"""Physical evidence, manual identity locks and monotonic completion."""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy import func, select

from euvieouvi.connectors.dtos import ExternalIdentifier, ExternalMediaItem, ExternalMediaKind
from euvieouvi.database.enums import ConnectorType, LibraryMediaType, MediaKind
from euvieouvi.database.models import (
    EnrichmentRecord,
    Library,
    MediaFilePath,
    MediaIdentifier,
    MediaImage,
    MediaItem,
    Setting,
    Source,
    SourceMediaRef,
    WatchEvent,
    WatchState,
)
from euvieouvi.database.unit_of_work import UnitOfWork
from euvieouvi.enrichment.providers import EnrichedMetadata, TmdbClient, TmdbMatch
from euvieouvi.extensions import db
from euvieouvi.sync.catalog_projection import rebuild_catalog_projection
from euvieouvi.sync.catalog_reconcile import reconcile_catalog
from euvieouvi.sync.manual_match import apply_manual_match
from euvieouvi.sync.persistence import MediaPersistenceService
from euvieouvi.sync.physical_identity import ROOTS_KEY, canonical_path, normalize_path
from euvieouvi.sync.watch_sync import WatchSyncService, _matching_item_groups
from euvieouvi.web import routes

NOW = datetime(2026, 9, 29, 20, tzinfo=UTC)


def test_undated_completion_propagates_without_inventing_playback_time(app: Flask) -> None:
    class Connector:
        def mark_watched(self, external_id: str, *, watched_at: datetime | None = None) -> None:
            assert external_id == "j" and watched_at is None

        def set_progress(self, external_id: str, progress_ms: int) -> None:
            raise AssertionError("Undated completion must not become partial progress")

        def close(self) -> None:
            pass

    with app.app_context():
        plex, jellyfin = service(ConnectorType.PLEX), service(ConnectorType.JELLYFIN)
        plex_source = db.session.get(Source, plex.source_id)
        jellyfin_source = db.session.get(Source, jellyfin.source_id)
        assert plex_source is not None and jellyfin_source is not None
        plex_source.enabled = jellyfin_source.enabled = True
        jellyfin_source.secret = '{"api_key":"fixture","user_id":"user"}'
        db.session.add(Setting(key="plex.user_id", value="user"))
        result = plex.persist_media(
            replace(movie("p", "Undated", "/same.mkv", "42"), completed=True, view_count=1), NOW
        )
        jellyfin.persist_media(
            replace(movie("j", "Undated", "/same.mkv", "42"), completed=False, view_count=0),
            NOW + timedelta(days=1),
        )
        db.session.commit()
        sync = WatchSyncService(lambda: db.session(), lambda _source: Connector())
        assert sync.run(None, source_type=ConnectorType.PLEX).updated == 1
        state = db.session.scalar(
            select(WatchState).where(
                WatchState.media_item_id == result.media_item_id,
                WatchState.source_id == jellyfin.source_id,
            )
        )
        assert state is not None and state.completed
        assert state.last_watched_at is None and state.last_played_at is None


def test_repeated_sync_preserves_real_playback_time_without_inventing_one(app: Flask) -> None:
    with app.app_context():
        plex = service(ConnectorType.PLEX)
        dto = replace(
            movie("p", "Partial", "/partial.mkv", "42"),
            view_count=0,
            completed=False,
            view_offset_ms=1000,
        )
        result = plex.persist_media(dto, NOW)
        db.session.commit()
        state = db.session.scalar(
            select(WatchState).where(WatchState.media_item_id == result.media_item_id)
        )
        assert state is not None and state.last_played_at is None
        plex.persist_media(dto, NOW + timedelta(days=1))
        db.session.commit()
        assert state.last_played_at is None
        source = db.session.get(Source, plex.source_id)
        assert source is not None
        routes._persist_and_enqueue_progress(source, "p", 2000, NOW)
        db.session.commit()
        assert state.last_played_at == NOW.replace(tzinfo=None)
        plex.persist_media(dto, NOW + timedelta(days=2))
        db.session.commit()
        assert state.last_played_at == NOW.replace(tzinfo=None)
        plex.persist_media(
            replace(dto, last_viewed_at=NOW + timedelta(hours=1)), NOW + timedelta(days=3)
        )
        db.session.commit()
        assert state.last_played_at == (NOW + timedelta(hours=1)).replace(tzinfo=None)


def test_manual_match_reconciles_historical_duplicate_and_keeps_selected_item(app: Flask) -> None:
    with app.app_context():
        plex, jellyfin = service(ConnectorType.PLEX), service(ConnectorType.JELLYFIN)
        active = plex.persist_media(movie("p", "Current", "/current.mkv", "42"), NOW)
        historical = jellyfin.persist_media(
            replace(
                movie("j", "Historical", "/old.mkv", "999"),
                completed=True,
                view_count=1,
                last_viewed_at=NOW,
            ),
            NOW,
        )
        db.session.commit()
        reference = db.session.scalar(
            select(SourceMediaRef).where(SourceMediaRef.media_item_id == historical.media_item_id)
        )
        assert reference is not None
        reference.available = False
        item = db.session.get(MediaItem, historical.media_item_id)
        assert item is not None
        match = TmdbMatch("42", "Correct", None, 1972, EnrichedMetadata(), None, (("tmdb", "42"),))
        assert apply_manual_match(db.session(), item, match) == historical.media_item_id
        db.session.commit()
        assert db.session.get(MediaItem, active.media_item_id) is None
        assert db.session.scalar(select(func.count()).select_from(SourceMediaRef)) == 2
        assert db.session.scalar(select(func.count()).select_from(WatchEvent)) == 1
        assert item.title == "Correct"


def test_plex_parent_metadata_updates_show_but_respects_manual_match(app: Flask) -> None:
    with app.app_context():
        plex = service(ConnectorType.PLEX)
        parent = ExternalMediaItem(
            external_id="show",
            library_external_id="library",
            kind=ExternalMediaKind.SHOW,
            title="SeaLab 2020",
            year=1972,
            thumb_path="/show/thumb",
            art_path="/show/art",
            summary="Plex summary",
        )
        episode = ExternalMediaItem(
            external_id="episode",
            library_external_id="library",
            kind=ExternalMediaKind.EPISODE,
            title="Pilot",
            show_external_id="show",
            show_title="SeaLab 2020",
            season_number=1,
            episode_number=1,
            show_metadata=parent,
        )
        result = plex.persist_media(episode, NOW)
        db.session.commit()
        leaf = db.session.get(MediaItem, result.media_item_id)
        assert leaf is not None
        season = db.session.get(MediaItem, leaf.parent_id)
        assert season is not None
        show = db.session.get(MediaItem, season.parent_id)
        assert show is not None and show.year == 1972 and show.summary == "Plex summary"
        assert db.session.scalar(select(func.count()).select_from(MediaImage)) == 2
        show.manual_match_tmdb_id = "364"
        show.title = "Manual title"
        db.session.commit()
        fresh = MediaPersistenceService(
            UnitOfWork(db.session()), source_id=plex.source_id, library_id=plex.library_id
        )
        fresh.persist_media(replace(episode, show_metadata=replace(parent, year=2000)), NOW)
        db.session.commit()
        assert show.title == "Manual title" and show.year == 1972


def test_artwork_version_changes_when_missing_cover_becomes_available(app: Flask) -> None:
    with app.app_context():
        item = MediaItem(kind=MediaKind.MOVIE, title="Cover test")
        db.session.add(item)
        db.session.commit()
        missing_version = routes._artwork_versions([item.id])[item.id]
        image = MediaImage(
            media_item_id=item.id,
            image_type="poster",
            provider="tmdb",
            source_url="https://image.tmdb.org/t/p/w500/test.jpg",
            cache_status="pending",
        )
        db.session.add(image)
        db.session.commit()
        pending_version = routes._artwork_versions([item.id])[item.id]
        assert pending_version != missing_version
        image.cache_status = "cached"
        image.local_filename = "test.jpg"
        image.updated_at = NOW + timedelta(days=10)
        db.session.commit()
        assert routes._artwork_versions([item.id])[item.id] != pending_version


@pytest.mark.parametrize(
    "kind,provider",
    [
        (MediaKind.SHOW, "tvdb"),
        (MediaKind.MOVIE, "tmdb"),
        (MediaKind.ALBUM, "mbid-release"),
        (MediaKind.TRACK, "mbid"),
    ],
)
def test_missing_artwork_queues_recovery_but_does_not_repeat_not_found(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    kind: MediaKind,
    provider: str,
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(routes, "submit_job", lambda _app, job, **_kw: queued.append(job))
    with app.app_context():
        item = MediaItem(kind=kind, title="Missing poster")
        db.session.add(item)
        db.session.flush()
        media_id = item.id
        db.session.add_all(
            [
                Setting(key="metadata.tmdb.enabled", value="true"),
                Setting(key="metadata.tmdb.token", value="fixture"),
                Setting(key="metadata.musicbrainz.enabled", value="true"),
                MediaIdentifier(media_item_id=media_id, provider=provider, external_id="42"),
            ]
        )
        db.session.commit()
        assert routes._artwork_versions([]) == {}
    client = app.test_client()
    response = client.get(f"/media/{media_id}/image")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert queued == ["catalog_images"]
    with app.app_context():
        db.session.add(
            EnrichmentRecord(
                media_item_id=media_id, provider=f"artwork:{provider}", status="not_found"
            )
        )
        db.session.commit()
    assert client.get(f"/media/{media_id}/image").status_code == 200
    assert queued == ["catalog_images"]


def test_match_lookup_failure_preserves_identity(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        item = MediaItem(kind=MediaKind.SHOW, title="Unidentified")
        db.session.add(item)
        db.session.add(Setting(key="metadata.tmdb.token", value="fixture"))
        db.session.commit()
        media_id = item.id

    def fail(*_args: object, **_kwargs: object) -> tuple[TmdbMatch, ...]:
        raise ValueError("Provider unavailable")

    monkeypatch.setattr(TmdbClient, "search", fail)
    response = app.test_client().get(f"/media/{media_id}/match?q=Unidentified")
    assert response.status_code == 200
    with app.app_context():
        saved = db.session.get(MediaItem, media_id)
        assert saved is not None and saved.title == "Unidentified"
        assert saved.manual_match_tmdb_id is None


def service(kind: ConnectorType) -> MediaPersistenceService:
    source = Source(name=kind.value, connector_type=kind, base_url="http://local", secret="test")
    db.session.add(source)
    db.session.flush()
    library = Library(
        source_id=source.id,
        external_id="library",
        name=kind.value,
        media_type=LibraryMediaType.SHOW,
        enabled=True,
        available=True,
        discovered_at=NOW,
        last_seen_at=NOW,
    )
    db.session.add(library)
    db.session.flush()
    return MediaPersistenceService(
        UnitOfWork(db.session()), source_id=source.id, library_id=library.id
    )


def movie(external_id: str, title: str, path: str, tmdb: str) -> ExternalMediaItem:
    return ExternalMediaItem(
        external_id=external_id,
        library_external_id="library",
        kind=ExternalMediaKind.MOVIE,
        title=title,
        file_paths=(path,),
        identifiers=(ExternalIdentifier("tmdb", tmdb),),
    )


@pytest.mark.parametrize("first", [ConnectorType.PLEX, ConnectorType.JELLYFIN])
def test_physical_file_wins_over_wrong_metadata_and_title(app: Flask, first: ConnectorType) -> None:
    with app.app_context():
        other = ConnectorType.JELLYFIN if first is ConnectorType.PLEX else ConnectorType.PLEX
        services = {first: service(first), other: service(other)}
        plex = movie("plex", "Correct", "/shared/movie.mkv", "1")
        jellyfin = movie("jellyfin", "Wrong", "/shared/movie.mkv", "2")
        items = {ConnectorType.PLEX: plex, ConnectorType.JELLYFIN: jellyfin}
        one = services[first].persist_media(items[first], NOW)
        two = services[other].persist_media(items[other], NOW)
        db.session.commit()
        assert one.media_item_id == two.media_item_id
        item = db.session.get(MediaItem, one.media_item_id)
        assert item is not None and item.title == "Correct"
        assert db.session.scalars(select(MediaIdentifier.external_id)).all() == ["1"]
        assert db.session.scalar(select(func.count()).select_from(MediaFilePath)) == 2


def test_root_mapping_and_never_basename_matching(app: Flask) -> None:
    with app.app_context():
        plex, jellyfin = service(ConnectorType.PLEX), service(ConnectorType.JELLYFIN)
        db.session.add(
            Setting(
                key=ROOTS_KEY,
                value=json.dumps(
                    {str(jellyfin.source_id): [{"source": "/jellyfin", "canonical": "/plex"}]}
                ),
            )
        )
        jellyfin = MediaPersistenceService(
            jellyfin.work, source_id=jellyfin.source_id, library_id=jellyfin.library_id
        )
        one = plex.persist_media(movie("p", "A", "/plex/A/file.mkv", "1"), NOW)
        two = jellyfin.persist_media(movie("j", "B", "/jellyfin/A/file.mkv", "2"), NOW)
        three = jellyfin.persist_media(movie("j-other", "C", "/jellyfin/B/file.mkv", "3"), NOW)
        assert one.media_item_id == two.media_item_id != three.media_item_id
        assert (
            canonical_path("/mnt/tv2/file.mkv", [{"source": "/mnt/tv", "canonical": "/tv"}])
            == "/mnt/tv2/file.mkv"
        )
        assert normalize_path("http://server/file.mkv") is None
        assert normalize_path("relative/file.mkv") is None


def test_existing_show_duplicates_reconcile_from_episode_file(app: Flask) -> None:
    with app.app_context():
        plex, jellyfin = service(ConnectorType.PLEX), service(ConnectorType.JELLYFIN)
        dto = ExternalMediaItem(
            external_id="p-episode",
            library_external_id="library",
            kind=ExternalMediaKind.EPISODE,
            title="Plex episode",
            show_external_id="p-show",
            show_title="Plex show",
            season_number=1,
            episode_number=1,
            identifiers=(ExternalIdentifier("tmdb", "1001"),),
            show_identifiers=(ExternalIdentifier("tmdb", "100"),),
        )
        wrong = replace(
            dto,
            external_id="j-episode",
            show_external_id="j-show",
            show_title="Wrong show",
            identifiers=(ExternalIdentifier("tmdb", "2001"),),
            show_identifiers=(ExternalIdentifier("tmdb", "200"),),
        )
        plex.persist_media(dto, NOW)
        jellyfin.persist_media(wrong, NOW)
        plex.persist_media(replace(dto, file_paths=("/shared/episode.mkv",)), NOW)
        jellyfin.persist_media(replace(wrong, file_paths=("/shared/episode.mkv",)), NOW)
        result = reconcile_catalog(db.session(), dry_run=False)
        assert result.items_merged == 1
        assert result.hierarchy_merged == 2
        assert (
            db.session.scalar(
                select(func.count()).select_from(MediaItem).where(MediaItem.kind == MediaKind.SHOW)
            )
            == 1
        )
        assert (
            db.session.scalar(
                select(func.count())
                .select_from(MediaItem)
                .where(MediaItem.kind == MediaKind.EPISODE)
            )
            == 1
        )


def test_manual_match_preserves_history_and_survives_sync(app: Flask) -> None:
    with app.app_context():
        plex = service(ConnectorType.PLEX)
        dto = replace(
            movie("p", "Wrong", "/shared/file.mkv", "999"),
            view_count=1,
            completed=True,
            last_viewed_at=NOW,
        )
        result = plex.persist_media(dto, NOW)
        db.session.commit()
        item = db.session.get(MediaItem, result.media_item_id)
        assert item is not None
        match = TmdbMatch(
            "42",
            "Correct show",
            "Original",
            1980,
            EnrichedMetadata(
                summary="Summary",
                genres=("Drama",),
                poster_url="https://image.tmdb.org/t/p/w500/a.jpg",
            ),
            "https://image.tmdb.org/t/p/w500/b.jpg",
            (("tmdb", "42"),),
        )
        apply_manual_match(db.session(), item, match)
        db.session.commit()
        plex.persist_media(
            replace(
                dto,
                title="Wrong again",
                thumb_path="/wrong/poster",
                view_count=0,
                completed=False,
                last_viewed_at=NOW - timedelta(days=1),
            ),
            NOW,
        )
        db.session.commit()
        assert item.title == "Correct show" and item.manual_match_tmdb_id == "42"
        assert db.session.scalars(select(MediaIdentifier.external_id)).all() == ["42"]
        assert {image.provider for image in db.session.scalars(select(MediaImage))} == {"tmdb"}
        assert db.session.scalar(select(func.count()).select_from(WatchEvent)) == 1
        state = db.session.scalar(select(WatchState))
        assert state is not None and state.completed
        assert state.last_watched_at == NOW.replace(tzinfo=None)


@pytest.mark.parametrize("anchored", [True, False])
def test_split_jellyfin_show_requires_complete_independent_file_anchor(
    app: Flask, anchored: bool
) -> None:
    with app.app_context():
        plex, jellyfin = service(ConnectorType.PLEX), service(ConnectorType.JELLYFIN)
        first = ExternalMediaItem(
            external_id="p1",
            library_external_id="library",
            kind=ExternalMediaKind.EPISODE,
            title="Episode 1",
            show_external_id="p-show",
            show_title="Plex show",
            season_number=1,
            episode_number=1,
            identifiers=(ExternalIdentifier("tmdb", "501"),),
            show_identifiers=(ExternalIdentifier("tmdb", "100"),),
        )
        second = replace(
            first,
            external_id="p2",
            episode_number=2,
            identifiers=(ExternalIdentifier("tmdb", "502"),),
        )
        jf_first = replace(
            first,
            external_id="j1",
            show_external_id="j-show-a",
            identifiers=(ExternalIdentifier("tmdb", "601"),),
        )
        jf_second = replace(
            second,
            external_id="j2",
            show_external_id="j-show-b",
            show_title="Wrong split show",
            show_identifiers=(ExternalIdentifier("tmdb", "200"),),
            identifiers=(ExternalIdentifier("tmdb", "602"),),
        )
        for svc, dto in (
            (plex, first),
            (plex, second),
            (jellyfin, jf_first),
            (jellyfin, jf_second),
        ):
            svc.persist_media(dto, NOW)
        for svc, dto, path in (
            (plex, first, "/shared/1.mkv"),
            (plex, second, "/shared/2.mkv"),
            (jellyfin, jf_first, "/shared/1.mkv" if anchored else "/shared/unrelated.mkv"),
            (jellyfin, jf_second, "/shared/2.mkv"),
        ):
            svc.persist_media(replace(dto, file_paths=(path,)), NOW)
        result = reconcile_catalog(db.session(), dry_run=False)
        assert result.items_merged == int(anchored)
        assert db.session.scalar(
            select(func.count()).select_from(MediaItem).where(MediaItem.kind == MediaKind.SHOW)
        ) == (1 if anchored else 2)


def test_watch_propagation_uses_files_not_conflicting_provider_ids(app: Flask) -> None:
    with app.app_context():
        plex, jellyfin = service(ConnectorType.PLEX), service(ConnectorType.JELLYFIN)
        one = plex.persist_media(movie("p", "A", "/shared/a.mkv", "1"), NOW)
        # Deliberately keep separate catalog rows to exercise propagation grouping.
        two = jellyfin.persist_media(movie("j", "B", "/shared/b.mkv", "2"), NOW)
        db.session.add(
            MediaIdentifier(media_item_id=two.media_item_id, provider="tmdb", external_id="1")
        )
        db.session.flush()
        ids = {one.media_item_id, two.media_item_id}
        groups = _matching_item_groups(db.session(), ids, dict.fromkeys(ids, MediaKind.MOVIE))
        assert len(groups) == 2
        result = reconcile_catalog(db.session(), dry_run=False)
        assert result.items_merged == 0
        rebuild_catalog_projection(db.session())
        from euvieouvi.database.models import CatalogEntry

        assert db.session.scalar(select(func.count()).select_from(CatalogEntry)) == 2


def test_reused_identifier_cannot_override_different_known_files(app: Flask) -> None:
    with app.app_context():
        plex, jellyfin = service(ConnectorType.PLEX), service(ConnectorType.JELLYFIN)
        one = plex.persist_media(movie("p", "A", "/shared/a.mkv", "1"), NOW)
        two = jellyfin.persist_media(movie("j", "B", "/shared/b.mkv", "1"), NOW)
        assert one.media_item_id != two.media_item_id


def test_mount_settings_reindex_existing_paths(app: Flask) -> None:
    with app.app_context():
        jellyfin = service(ConnectorType.JELLYFIN)
        jellyfin.persist_media(movie("j", "A", "/media/a.mkv", "1"), NOW)
        source_id = jellyfin.source_id
        db.session.commit()
    client = app.test_client()
    page = client.get("/settings/media-identity")
    csrf_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
    assert csrf_match is not None
    token = csrf_match.group(1)
    response = client.post(
        "/settings/media-identity",
        data={"csrf_token": token, f"roots_{source_id}": "/media => /shared"},
    )
    assert response.status_code == 302
    with app.app_context():
        path = db.session.scalar(select(MediaFilePath))
        assert path is not None and path.canonical_path == "/shared/a.mkv"
    response = client.post(
        "/settings/media-identity",
        data={"csrf_token": token, f"roots_{source_id}": "relative => /shared"},
    )
    assert response.status_code == 302
    with app.app_context():
        path = db.session.scalar(select(MediaFilePath))
        assert path is not None and path.canonical_path == "/shared/a.mkv"


def test_match_ui_and_csrf(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    with app.app_context():
        item = MediaItem(kind=MediaKind.SHOW, title="History-only show")
        db.session.add(item)
        db.session.add(Setting(key="metadata.tmdb.token", value="fixture"))
        db.session.commit()
        media_id = item.id
    match = TmdbMatch(
        "42", "Correct", None, 1980, EnrichedMetadata(), identifiers=(("tmdb", "42"),)
    )
    monkeypatch.setattr(TmdbClient, "search", lambda *_a, **_kw: (match,))
    monkeypatch.setattr(TmdbClient, "match_details", lambda *_a, **_kw: match)
    monkeypatch.setattr(routes, "submit_job", lambda *_a, **_kw: True)
    client = app.test_client()
    response = client.get(f"/media/{media_id}/match?q=History")
    assert response.status_code == 200 and b"Correct" in response.data
    csrf_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', response.text)
    assert csrf_match is not None
    token = csrf_match.group(1)
    assert client.post(f"/media/{media_id}/match", data={"tmdb_id": "42"}).status_code == 400
    response = client.post(f"/media/{media_id}/match", data={"csrf_token": token, "tmdb_id": "42"})
    assert response.status_code == 302
    with app.app_context():
        saved_item = db.session.get(MediaItem, media_id)
        assert saved_item is not None and saved_item.manual_match_tmdb_id == "42"
