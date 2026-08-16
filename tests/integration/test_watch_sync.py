"""Cross-source watched-state propagation tests."""

from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask

from euvieouvi.database.enums import (
    ConnectorType,
    LibraryMediaType,
    MediaKind,
    SyncStatus,
    SyncTrigger,
)
from euvieouvi.database.models import (
    Library,
    MediaIdentifier,
    MediaItem,
    Setting,
    Source,
    SourceMediaRef,
    SyncRun,
    WatchEvent,
    WatchState,
)
from euvieouvi.extensions import db
from euvieouvi.sync.watch_sync import WatchSyncService, _matching_item_groups

NOW = datetime(2026, 8, 11, 19, tzinfo=UTC)


class FailingConnector:
    def mark_watched(
        self, external_id: str, *, watched_at: datetime | None = None
    ) -> None:
        del watched_at
        raise RuntimeError(external_id)

    def close(self) -> None:
        pass


class RecordingConnector:
    def __init__(self, source: Source, calls: list[tuple[ConnectorType, str]]) -> None:
        self.source = source
        self.calls = calls

    def mark_watched(
        self, external_id: str, *, watched_at: datetime | None = None
    ) -> None:
        assert watched_at == NOW
        self.calls.append((self.source.connector_type, external_id))

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("watched_source", "target_source", "target_external_id"),
    [
        (ConnectorType.PLEX, ConnectorType.JELLYFIN, "jf-202"),
        (ConnectorType.JELLYFIN, ConnectorType.PLEX, "plex-101"),
    ],
)
def test_completed_state_is_propagated_only_to_unwatched_matching_source(
    app: Flask,
    watched_source: ConnectorType,
    target_source: ConnectorType,
    target_external_id: str,
) -> None:
    calls: list[tuple[ConnectorType, str]] = []
    with app.app_context():
        plex = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex.local",
            secret="plex-token",
            enabled=True,
        )
        jellyfin = Source(
            connector_type=ConnectorType.JELLYFIN,
            name="Jellyfin",
            base_url="http://jellyfin.local",
            secret='{"api_key":"key","user_id":"user"}',
            enabled=True,
        )
        db.session.add_all([plex, jellyfin, Setting(key="plex.user_id", value="plex-user")])
        db.session.flush()
        source_by_type = {ConnectorType.PLEX: plex, ConnectorType.JELLYFIN: jellyfin}
        plex_library = Library(
            source_id=plex.id,
            external_id="plex-movies",
            name="Movies",
            media_type=LibraryMediaType.MOVIE,
            enabled=True,
            available=True,
            discovered_at=NOW,
            last_seen_at=NOW,
        )
        jellyfin_library = Library(
            source_id=jellyfin.id,
            external_id="jf-movies",
            name="Movies",
            media_type=LibraryMediaType.MOVIE,
            enabled=True,
            available=True,
            discovered_at=NOW,
            last_seen_at=NOW,
        )
        movie = MediaItem(kind=MediaKind.MOVIE, title="Arrival")
        db.session.add_all([plex_library, jellyfin_library, movie])
        db.session.flush()
        db.session.add_all(
            [
                SourceMediaRef(
                    source_id=plex.id,
                    library_id=plex_library.id,
                    media_item_id=movie.id,
                    external_id="plex-101",
                    last_seen_at=NOW,
                    available=True,
                ),
                SourceMediaRef(
                    source_id=jellyfin.id,
                    library_id=jellyfin_library.id,
                    media_item_id=movie.id,
                    external_id="jf-202",
                    last_seen_at=NOW,
                    available=True,
                ),
                WatchState(
                    media_item_id=movie.id,
                    source_id=source_by_type[watched_source].id,
                    view_count=1,
                    completed=True,
                    observed_at=NOW,
                ),
                WatchEvent(
                    media_item_id=movie.id,
                    source_id=source_by_type[watched_source].id,
                    source_event_id="webhook:completed",
                    dedup_key="webhook:completed",
                    watched_at=NOW,
                    completed=True,
                    playback_user=(
                        "plex-user" if watched_source is ConnectorType.PLEX else "user"
                    ),
                    origin="webhook",
                ),
            ]
        )
        run = SyncRun(
            source_id=plex.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.SUCCEEDED,
            started_at=NOW,
            finished_at=NOW,
        )
        db.session.add(run)
        db.session.commit()
        if watched_source is ConnectorType.PLEX:
            watched_state = db.session.scalar(
                db.select(WatchState).where(
                    WatchState.media_item_id == movie.id,
                    WatchState.source_id == plex.id,
                )
            )
            db.session.delete(watched_state)
            db.session.commit()
        run_id = run.id
        movie_id = movie.id
        target_source_id = source_by_type[target_source].id

        result = WatchSyncService(
            lambda: db.session(), lambda source: RecordingConnector(source, calls)
        ).run(run_id)

        assert result.scanned == 1
        assert result.updated == 1
        assert result.skipped == 0
        assert result.failed == 0
        assert calls == [(target_source, target_external_id)]
        target_state = (
            db.session.query(WatchState)
            .filter_by(media_item_id=movie_id, source_id=target_source_id)
            .one()
        )
        assert target_state.completed is True
        assert target_state.last_watched_at == NOW.replace(tzinfo=None)
        persisted_run = db.session.get(SyncRun, run_id)
        assert persisted_run is not None
        assert persisted_run.watch_sync_status is SyncStatus.SUCCEEDED
        assert persisted_run.watch_sync_updated == 1
        assert "1 atualizados" in (persisted_run.watch_sync_summary or "")

        calls.clear()
        aligned_result = WatchSyncService(
            lambda: db.session(), lambda source: RecordingConnector(source, calls)
        ).run(None)
        assert aligned_result.updated == 0
        assert calls == []

        target_state = (
            db.session.query(WatchState)
            .filter_by(media_item_id=movie_id, source_id=target_source_id)
            .one()
        )
        target_state.last_watched_at = NOW + timedelta(days=1)
        db.session.commit()
        conflict_result = WatchSyncService(
            lambda: db.session(), lambda source: RecordingConnector(source, calls)
        ).run(None, source_type=watched_source)
        assert conflict_result.updated == 0
        assert calls == []

        target_state = (
            db.session.query(WatchState)
            .filter_by(media_item_id=movie_id, source_id=target_source_id)
            .one()
        )
        target_state.completed = False
        db.session.add(Setting(key="watch_sync.pending", value="true"))
        db.session.commit()

        calls.clear()
        pending_result = WatchSyncService(
            lambda: db.session(), lambda source: RecordingConnector(source, calls)
        ).run(None)

        assert pending_result.updated == 1
        assert calls == [(target_source, target_external_id)]
        pending = db.session.get(Setting, "watch_sync.pending")
        assert pending is not None and pending.value == "false"

        target_state = (
            db.session.query(WatchState)
            .filter_by(media_item_id=movie_id, source_id=target_source_id)
            .one()
        )
        target_state.completed = False
        pending.value = "true"
        db.session.commit()
        failed_result = WatchSyncService(
            lambda: db.session(), lambda source: FailingConnector()
        ).run(None)

        assert failed_result.failed == 1
        persisted_pending = db.session.get(Setting, "watch_sync.pending")
        assert persisted_pending is not None and persisted_pending.value == "true"


def test_already_aligned_media_does_not_call_remote_server(app: Flask) -> None:
    calls: list[tuple[ConnectorType, str]] = []
    with app.app_context():
        plex = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex.local",
            secret="token",
            enabled=True,
        )
        jellyfin = Source(
            connector_type=ConnectorType.JELLYFIN,
            name="Jellyfin",
            base_url="http://jellyfin.local",
            secret='{"api_key":"key","user_id":"user"}',
            enabled=True,
        )
        db.session.add_all([plex, jellyfin])
        db.session.flush()
        run = SyncRun(
            source_id=plex.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.SUCCEEDED,
            started_at=NOW,
            finished_at=NOW,
        )
        db.session.add(run)
        db.session.commit()
        result = WatchSyncService(
            lambda: db.session(), lambda source: RecordingConnector(source, calls)
        ).run(run.id)

        assert result.updated == 0
        assert calls == []


def test_separate_rows_are_grouped_by_provider_identity(app: Flask) -> None:
    with app.app_context():
        plex_item = MediaItem(kind=MediaKind.EPISODE, title="Second Contact")
        jellyfin_item = MediaItem(kind=MediaKind.EPISODE, title="Second Contact")
        db.session.add_all([plex_item, jellyfin_item])
        db.session.flush()
        db.session.add_all(
            [
                MediaIdentifier(media_item_id=plex_item.id, provider="tvdb", external_id="7820679"),
                MediaIdentifier(
                    media_item_id=jellyfin_item.id, provider="tvdb", external_id="7820679"
                ),
            ]
        )
        db.session.flush()
        item_ids = {plex_item.id, jellyfin_item.id}
        groups = _matching_item_groups(
            db.session, item_ids, {media_id: MediaKind.EPISODE for media_id in item_ids}
        )
        assert groups == (frozenset(item_ids),)
