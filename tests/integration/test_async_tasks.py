"""Durability guarantees for instant asynchronous updates."""

from datetime import UTC, datetime, timedelta

from flask import Flask
from pytest import MonkeyPatch
from sqlalchemy import select

from euvieouvi.connectors.dtos import ExternalMediaItem, ExternalMediaKind
from euvieouvi.connectors.errors import ConnectorNotFoundError
from euvieouvi.database.enums import ConnectorType, LibraryMediaType, MediaKind
from euvieouvi.database.models import (
    AsyncTask,
    Library,
    MediaItem,
    Source,
    SourceMediaRef,
    WatchState,
)
from euvieouvi.extensions import db
from euvieouvi.sync.async_tasks import AsyncTaskExecutor, enqueue_watch_update, recover_async_tasks


class _RecordingWatchConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | datetime | None]] = []
        self.closed = 0

    def set_progress(self, external_id: str, progress_ms: int) -> None:
        self.calls.append(("progress", external_id, progress_ms))

    def mark_watched(self, external_id: str, *, watched_at: datetime | None = None) -> None:
        self.calls.append(("watched", external_id, watched_at))

    def close(self) -> None:
        self.closed += 1


class _StaleAwareConnector(_RecordingWatchConnector):
    def mark_watched(self, external_id: str, *, watched_at: datetime | None = None) -> None:
        self.calls.append(("watched", external_id, watched_at))
        if external_id == "jellyfin-stale":
            raise ConnectorNotFoundError("missing")


class _LookupConnector:
    def __init__(self, item: ExternalMediaItem) -> None:
        self.item = item
        self.closed = 0

    def get_media_item(self, external_id: str, library_external_id: str) -> ExternalMediaItem:
        assert external_id == self.item.external_id
        assert library_external_id == self.item.library_external_id
        return self.item

    def close(self) -> None:
        self.closed += 1


def _seed_cross_server_episode(observed_at: datetime) -> tuple[int, int, int]:
    plex = Source(
        connector_type=ConnectorType.PLEX,
        name="Plex",
        base_url="http://plex",
        secret="token",
        enabled=True,
    )
    jellyfin = Source(
        connector_type=ConnectorType.JELLYFIN,
        name="Jellyfin",
        base_url="http://jellyfin",
        secret='{"api_key":"key","user_id":"user"}',
        enabled=True,
    )
    db.session.add_all([plex, jellyfin])
    db.session.flush()
    plex_library = Library(
        source_id=plex.id,
        external_id="plex-shows",
        name="Plex Shows",
        media_type=LibraryMediaType.SHOW,
        enabled=True,
        available=True,
        discovered_at=observed_at,
        last_seen_at=observed_at,
    )
    jellyfin_library = Library(
        source_id=jellyfin.id,
        external_id="jellyfin-shows",
        name="Jellyfin Shows",
        media_type=LibraryMediaType.SHOW,
        enabled=True,
        available=True,
        discovered_at=observed_at,
        last_seen_at=observed_at,
    )
    episode = MediaItem(
        kind=MediaKind.EPISODE,
        title="Dutton Ranch",
        season_number=1,
        episode_number=3,
        duration_ms=3_600_000,
    )
    db.session.add_all([plex_library, jellyfin_library, episode])
    db.session.flush()
    db.session.add_all(
        [
            SourceMediaRef(
                source_id=plex.id,
                library_id=plex_library.id,
                media_item_id=episode.id,
                external_id="plex-episode",
                last_seen_at=observed_at,
                available=True,
            ),
            SourceMediaRef(
                source_id=jellyfin.id,
                library_id=jellyfin_library.id,
                media_item_id=episode.id,
                external_id="jellyfin-episode",
                last_seen_at=observed_at,
                available=True,
            ),
        ]
    )
    db.session.commit()
    return plex.id, jellyfin.id, episode.id


def test_watch_update_enqueue_is_durable_and_idempotent(app: Flask) -> None:
    watched_at = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)
    with app.app_context():
        enqueue_watch_update(
            db.session(), source_id=1, external_id="movie-1", watched_at=watched_at
        )
        enqueue_watch_update(
            db.session(), source_id=1, external_id="movie-1", watched_at=watched_at
        )
        db.session.commit()

        task = db.session.query(AsyncTask).one()
        assert task.status == "pending" and task.attempts == 0
        task.status = "processing"
        db.session.commit()
        assert recover_async_tasks() == 1
        assert db.session.get(AsyncTask, task.id).status == "pending"  # type: ignore[union-attr]


def test_progress_arriving_during_processing_is_preserved_as_successor(app: Flask) -> None:
    watched_at = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)
    with app.app_context():
        enqueue_watch_update(
            db.session(),
            source_id=1,
            external_id="episode-1",
            watched_at=watched_at,
            progress_ms=100,
            completed=False,
        )
        db.session.commit()
        task = db.session.query(AsyncTask).one()
        task.status = "processing"
        db.session.commit()

        enqueue_watch_update(
            db.session(),
            source_id=1,
            external_id="episode-1",
            watched_at=watched_at,
            progress_ms=200,
            completed=False,
        )
        db.session.commit()

        task = db.session.query(AsyncTask).one()
        assert task.status == "processing"
        assert task.pending_payload is not None
        assert '"progress_ms":200' in task.pending_payload

        assert recover_async_tasks() == 1
        task = db.session.query(AsyncTask).one()
        assert task.status == "pending"
        assert task.pending_payload is None
        assert '"progress_ms":200' in task.payload


def test_watch_update_without_cross_server_match_is_completed_as_noop(app: Flask) -> None:
    watched_at = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    with app.app_context():
        source = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex",
            secret="token",
            enabled=True,
        )
        db.session.add(source)
        db.session.flush()
        library = Library(
            source_id=source.id,
            external_id="music",
            name="Music",
            media_type=LibraryMediaType.ARTIST,
            enabled=True,
            available=True,
            discovered_at=watched_at,
            last_seen_at=watched_at,
        )
        item = MediaItem(kind=MediaKind.TRACK, title="Unmatched track")
        db.session.add_all([library, item])
        db.session.flush()
        db.session.add(
            SourceMediaRef(
                source_id=source.id,
                library_id=library.id,
                media_item_id=item.id,
                external_id="track-1",
                last_seen_at=watched_at,
                available=True,
            )
        )
        enqueue_watch_update(
            db.session(), source_id=source.id, external_id="track-1", watched_at=watched_at
        )
        db.session.commit()
        task_id = db.session.query(AsyncTask.id).scalar()

        succeeded, changed, failure = AsyncTaskExecutor(app)._run_one(task_id)

        assert (succeeded, changed, failure) == (True, False, None)
        assert db.session.get(AsyncTask, task_id) is None


def test_async_failure_detail_identifies_task_and_media(app: Flask) -> None:
    watched_at = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    with app.app_context():
        enqueue_watch_update(
            db.session(), source_id=999, external_id="missing-1", watched_at=watched_at
        )
        db.session.commit()
        task_id = db.session.query(AsyncTask.id).scalar()

        succeeded, changed, failure = AsyncTaskExecutor(app)._run_one(task_id)

        assert succeeded is False and changed is False
        assert failure is not None
        assert f"task={task_id}" in failure
        assert "origem=999" in failure and "mídia=missing-1" in failure
        assert "LookupError: watch update source is not available" in failure


def test_async_executor_propagates_partial_then_completed_watch_state(
    app: Flask, monkeypatch: MonkeyPatch
) -> None:
    observed_at = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    connector = _RecordingWatchConnector()
    monkeypatch.setattr("euvieouvi.api.runtime.connector_for", lambda _source: connector)

    with app.app_context():
        plex_id, jellyfin_id, media_id = _seed_cross_server_episode(observed_at)
        enqueue_watch_update(
            db.session(),
            source_id=plex_id,
            external_id="plex-episode",
            watched_at=observed_at,
            progress_ms=1_800_000,
            completed=False,
        )
        db.session.commit()

        executor = AsyncTaskExecutor(app)
        executor._execute()

        state = db.session.scalar(
            select(WatchState).where(
                WatchState.media_item_id == media_id,
                WatchState.source_id == jellyfin_id,
            )
        )
        assert state is not None
        assert state.completed is False and state.progress_ms == 1_800_000
        assert state.last_played_at == observed_at.replace(tzinfo=None)
        assert connector.calls == [("progress", "jellyfin-episode", 1_800_000)]
        assert connector.closed == 1
        assert db.session.query(AsyncTask).count() == 0
        assert executor.snapshot["processed"] == 1
        assert executor.snapshot["updated"] == 1
        assert executor.snapshot["failed"] == 0

        watched_at = observed_at + timedelta(minutes=30)
        enqueue_watch_update(
            db.session(),
            source_id=plex_id,
            external_id="plex-episode",
            watched_at=watched_at,
        )
        db.session.commit()
        executor._execute()

        completed_state = db.session.scalar(
            select(WatchState).where(
                WatchState.media_item_id == media_id,
                WatchState.source_id == jellyfin_id,
            )
        )
        assert completed_state is not None
        assert completed_state.completed is True and completed_state.progress_ms is None
        assert completed_state.view_count == 1
        assert connector.calls[-1] == ("watched", "jellyfin-episode", watched_at)
        assert connector.closed == 2


def test_completed_event_supersedes_queued_partial_progress(
    app: Flask, monkeypatch: MonkeyPatch
) -> None:
    observed_at = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    connector = _RecordingWatchConnector()
    monkeypatch.setattr("euvieouvi.api.runtime.connector_for", lambda _source: connector)

    with app.app_context():
        plex_id, _jellyfin_id, _media_id = _seed_cross_server_episode(observed_at)
        enqueue_watch_update(
            db.session(),
            source_id=plex_id,
            external_id="plex-episode",
            watched_at=observed_at,
            progress_ms=1_800_000,
            completed=False,
        )
        enqueue_watch_update(
            db.session(),
            source_id=plex_id,
            external_id="plex-episode",
            watched_at=observed_at + timedelta(minutes=10),
        )
        db.session.commit()

        AsyncTaskExecutor(app)._execute()

        assert connector.calls == [
            ("watched", "jellyfin-episode", observed_at + timedelta(minutes=10))
        ]
        assert db.session.query(AsyncTask).count() == 0


def test_stale_duplicate_target_reference_is_disabled_and_valid_one_is_updated(
    app: Flask, monkeypatch: MonkeyPatch
) -> None:
    observed_at = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    connector = _StaleAwareConnector()
    monkeypatch.setattr("euvieouvi.api.runtime.connector_for", lambda _source: connector)

    with app.app_context():
        plex_id, jellyfin_id, media_id = _seed_cross_server_episode(observed_at)
        jellyfin_library_id = db.session.scalar(
            select(Library.id).where(Library.source_id == jellyfin_id)
        )
        stale = SourceMediaRef(
            source_id=jellyfin_id,
            library_id=jellyfin_library_id,
            media_item_id=media_id,
            external_id="jellyfin-stale",
            last_seen_at=observed_at - timedelta(days=10),
            available=True,
        )
        db.session.add(stale)
        enqueue_watch_update(
            db.session(),
            source_id=plex_id,
            external_id="plex-episode",
            watched_at=observed_at,
        )
        db.session.commit()
        stale_id = stale.id

        executor = AsyncTaskExecutor(app)
        executor._execute()

        assert connector.calls == [
            ("watched", "jellyfin-episode", observed_at),
            ("watched", "jellyfin-stale", observed_at),
        ]
        db.session.expire_all()
        repaired = db.session.get(SourceMediaRef, stale_id)
        assert repaired is not None and repaired.available is False
        assert repaired.unavailable_since is not None
        assert db.session.query(AsyncTask).count() == 0
        assert "AUTO-REPARO" in str(executor.snapshot["failure_details"])


def test_missing_origin_reference_is_recovered_without_full_source_scan(
    app: Flask, monkeypatch: MonkeyPatch
) -> None:
    observed_at = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    with app.app_context():
        plex = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex",
            secret="token",
            enabled=True,
        )
        jellyfin = Source(
            connector_type=ConnectorType.JELLYFIN,
            name="Jellyfin",
            base_url="http://jellyfin",
            secret='{"api_key":"key","user_id":"user"}',
            enabled=True,
        )
        db.session.add_all([plex, jellyfin])
        db.session.flush()
        plex_library = Library(
            source_id=plex.id,
            external_id="plex-shows",
            name="Plex Shows",
            media_type=LibraryMediaType.SHOW,
            enabled=True,
            available=True,
            discovered_at=observed_at,
            last_seen_at=observed_at,
        )
        jellyfin_library = Library(
            source_id=jellyfin.id,
            external_id="jellyfin-shows",
            name="Jellyfin Shows",
            media_type=LibraryMediaType.SHOW,
            enabled=True,
            available=True,
            discovered_at=observed_at,
            last_seen_at=observed_at,
        )
        old_show = MediaItem(kind=MediaKind.SHOW, title="Below Deck Mediterranean")
        current_show = MediaItem(kind=MediaKind.SHOW, title="Below Deck Mediterranean")
        db.session.add_all([plex_library, jellyfin_library, old_show, current_show])
        db.session.flush()
        old_season = MediaItem(
            kind=MediaKind.SEASON,
            title="Season 11",
            parent_id=old_show.id,
            season_number=11,
        )
        current_season = MediaItem(
            kind=MediaKind.SEASON,
            title="Season 11",
            parent_id=current_show.id,
            season_number=11,
        )
        db.session.add_all([old_season, current_season])
        db.session.flush()
        episode = MediaItem(
            kind=MediaKind.EPISODE,
            title="Chips on the Shoulders",
            parent_id=current_season.id,
            season_number=11,
            episode_number=15,
        )
        db.session.add(episode)
        db.session.flush()
        db.session.add_all(
            [
                SourceMediaRef(
                    source_id=plex.id,
                    library_id=plex_library.id,
                    media_item_id=old_show.id,
                    external_id="old-plex-show",
                    last_seen_at=observed_at - timedelta(days=30),
                    available=False,
                ),
                SourceMediaRef(
                    source_id=plex.id,
                    library_id=plex_library.id,
                    media_item_id=old_season.id,
                    external_id="old-plex-season",
                    last_seen_at=observed_at - timedelta(days=30),
                    available=False,
                ),
                SourceMediaRef(
                    source_id=jellyfin.id,
                    library_id=jellyfin_library.id,
                    media_item_id=current_show.id,
                    external_id="jf-show",
                    last_seen_at=observed_at,
                    available=True,
                ),
                SourceMediaRef(
                    source_id=jellyfin.id,
                    library_id=jellyfin_library.id,
                    media_item_id=current_season.id,
                    external_id="jf-season",
                    last_seen_at=observed_at,
                    available=True,
                ),
                SourceMediaRef(
                    source_id=jellyfin.id,
                    library_id=jellyfin_library.id,
                    media_item_id=episode.id,
                    external_id="jf-episode-15",
                    last_seen_at=observed_at,
                    available=True,
                ),
            ]
        )
        queued_item = ExternalMediaItem(
            external_id="new-plex-episode-15",
            library_external_id="plex-shows",
            kind=ExternalMediaKind.EPISODE,
            title="Chips on the Shoulders",
            show_external_id="new-plex-show",
            show_title="Below Deck Mediterranean",
            season_external_id="new-plex-season",
            season_number=11,
            episode_number=15,
        )
        enqueue_watch_update(
            db.session(),
            source_id=plex.id,
            external_id=queued_item.external_id,
            watched_at=observed_at,
            library_external_id=plex_library.external_id,
        )
        db.session.commit()
        plex_id = plex.id
        jellyfin_id = jellyfin.id
        episode_id = episode.id

        lookup = _LookupConnector(queued_item)
        target = _RecordingWatchConnector()
        monkeypatch.setattr(
            "euvieouvi.api.runtime.connector_for",
            lambda source: lookup if source.id == plex_id else target,
        )
        executor = AsyncTaskExecutor(app)
        executor._execute()

        repaired_ref = db.session.scalar(
            select(SourceMediaRef).where(
                SourceMediaRef.source_id == plex_id,
                SourceMediaRef.external_id == queued_item.external_id,
            )
        )
        assert repaired_ref is not None and repaired_ref.media_item_id == episode_id
        state = db.session.scalar(
            select(WatchState).where(
                WatchState.media_item_id == episode_id,
                WatchState.source_id == jellyfin_id,
            )
        )
        assert state is not None and state.completed is True
        assert target.calls == [("watched", "jf-episode-15", observed_at)]
        assert db.session.query(AsyncTask).count() == 0
        assert "referência recriada" in str(executor.snapshot["failure_details"])


def test_async_executor_reports_fatal_drain_error(app: Flask, monkeypatch: MonkeyPatch) -> None:
    executor = AsyncTaskExecutor(app)

    def fail_claim() -> int | None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(executor, "_claim_due", fail_claim)
    executor._execute()

    assert executor.snapshot["active"] is False
    assert executor.snapshot["failed"] == 1
    assert "RuntimeError: database unavailable" in str(executor.snapshot["summary"])
    assert "operação=processar fila" in str(executor.snapshot["failure_details"])


def test_retry_all_makes_pending_tasks_due_and_forces_submit(
    app: Flask, monkeypatch: MonkeyPatch
) -> None:
    future = datetime.now(UTC) + timedelta(days=1)
    with app.app_context():
        task = AsyncTask(
            task_type="watch_update",
            dedup_key="future-task",
            payload="{}",
            status="blocked",
            attempts=12,
            next_attempt_at=future,
        )
        db.session.add(task)
        db.session.commit()
        task_id = task.id

    executor = AsyncTaskExecutor(app)
    submitted: list[bool] = []

    def submit(*, force: bool = False) -> bool:
        submitted.append(force)
        return True

    monkeypatch.setattr(executor, "submit", submit)
    assert executor.retry_all() is True
    assert submitted == [True]
    with app.app_context():
        refreshed = db.session.get(AsyncTask, task_id)
        assert refreshed is not None
        assert refreshed.status == "pending" and refreshed.attempts == 0
        next_attempt_at = refreshed.next_attempt_at.replace(tzinfo=UTC)
        assert next_attempt_at < future


def test_event_is_retained_when_only_known_target_reference_is_stale(app: Flask) -> None:
    observed_at = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    with app.app_context():
        plex_id, jellyfin_id, media_id = _seed_cross_server_episode(observed_at)
        target = db.session.scalar(
            select(SourceMediaRef).where(
                SourceMediaRef.source_id == jellyfin_id,
                SourceMediaRef.media_item_id == media_id,
            )
        )
        assert target is not None
        target.available = False
        target.unavailable_since = observed_at
        enqueue_watch_update(
            db.session(),
            source_id=plex_id,
            external_id="plex-episode",
            watched_at=observed_at,
        )
        db.session.commit()
        task_id = db.session.scalar(select(AsyncTask.id))
        assert task_id is not None

        succeeded, changed, failure = AsyncTaskExecutor(app)._run_one(task_id)

        assert succeeded is False and changed is False
        assert failure is not None and "references are stale" in failure
        retained = db.session.get(AsyncTask, task_id)
        assert retained is not None and retained.status == "pending"
