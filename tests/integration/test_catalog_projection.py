"""Persistent catalog projection and canonical grouping."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy import func, select, text

from euvieouvi.database.enums import ConnectorType, LibraryMediaType, MediaKind
from euvieouvi.database.models import (
    CatalogEntry,
    CatalogEntryMember,
    CatalogProjectionDirtyItem,
    Library,
    MediaIdentifier,
    MediaItem,
    Setting,
    Source,
    SourceMediaRef,
    WatchState,
)
from euvieouvi.extensions import db
from euvieouvi.sync.catalog_projection import (
    PROJECTION_STATE_KEY,
    PROJECTION_VERSION,
    ensure_catalog_projection,
    rebuild_catalog_projection,
)
from euvieouvi.web.routes import _persist_and_enqueue_progress

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


@pytest.mark.parametrize("kind", [MediaKind.MOVIE, MediaKind.SHOW, MediaKind.ARTIST])
def test_sync_time_never_counts_as_playback_but_live_progress_does(
    app: Flask, kind: MediaKind
) -> None:
    with app.app_context():
        source, library = _source_with_library(ConnectorType.PLEX, "Plex", "plex")
        latest = MediaItem(kind=kind, title="Z latest playback")
        stale = MediaItem(kind=kind, title="A old progress")
        db.session.add_all([latest, stale])
        db.session.flush()
        leaves = [latest, stale]
        if kind in {MediaKind.SHOW, MediaKind.ARTIST}:
            containers = [
                MediaItem(
                    kind=MediaKind.SEASON if kind is MediaKind.SHOW else MediaKind.ALBUM,
                    title="Container",
                    parent_id=item.id,
                )
                for item in leaves
            ]
            db.session.add_all(containers)
            db.session.flush()
            leaves = [
                MediaItem(
                    kind=MediaKind.EPISODE if kind is MediaKind.SHOW else MediaKind.TRACK,
                    title="Leaf",
                    parent_id=item.id,
                )
                for item in containers
            ]
            db.session.add_all(leaves)
            db.session.flush()
        for item in {latest, stale, *leaves}:
            db.session.add(_reference(source, library, item, f"item-{item.id}"))
        stale_state = WatchState(
            media_item_id=leaves[1].id,
            source_id=source.id,
            completed=False,
            view_count=0,
            progress_ms=12345,
            observed_at=NOW + timedelta(days=1),
        )
        db.session.add_all(
            [
                WatchState(
                    media_item_id=leaves[0].id,
                    source_id=source.id,
                    completed=True,
                    view_count=1,
                    last_watched_at=NOW,
                    last_played_at=NOW,
                    observed_at=NOW + timedelta(days=1),
                ),
                stale_state,
            ]
        )
        db.session.commit()
        rebuild_catalog_projection(db.session())
        db.session.commit()
        latest_entry = db.session.get(CatalogEntry, latest.id)
        stale_entry = db.session.get(CatalogEntry, stale.id)
        assert latest_entry is not None and latest_entry.last_played_at == NOW.replace(tzinfo=None)
        assert stale_entry is not None and stale_entry.last_played_at is None
        assert db.session.get(CatalogEntry, leaves[1].id).progress_ms == 12345  # type: ignore[union-attr]
        stale_state.progress_ms = (stale_state.progress_ms or 0) + 1
        stale_state.observed_at = NOW + timedelta(days=2)
        db.session.commit()
        refreshed = ensure_catalog_projection(db.session())
        db.session.commit()
        assert refreshed is not None and refreshed.mode == "activity"
        assert stale_entry.last_played_at is None
        live_at = NOW + timedelta(days=3)
        _persist_and_enqueue_progress(source, f"item-{leaves[1].id}", 20000, live_at)
        db.session.commit()
        ensure_catalog_projection(db.session())
        db.session.commit()
        assert stale_entry.last_played_at == live_at.replace(tzinfo=None)
        _persist_and_enqueue_progress(source, f"item-{leaves[1].id}", 15000, NOW)
        db.session.commit()
        ensure_catalog_projection(db.session())
        db.session.commit()
        assert stale_entry.last_played_at == live_at.replace(tzinfo=None)


def test_unidentified_sort_is_persistent_and_uses_all_group_members(app: Flask) -> None:
    with app.app_context():
        source, library = _source_with_library(ConnectorType.PLEX, "Plex", "plex")
        identified = MediaItem(kind=MediaKind.SHOW, title="A identified")
        unidentified = MediaItem(kind=MediaKind.SHOW, title="Z unidentified")
        db.session.add_all([identified, unidentified])
        db.session.flush()
        db.session.add_all(
            [
                _reference(source, library, identified, "known"),
                _reference(source, library, unidentified, "unknown"),
                MediaIdentifier(media_item_id=identified.id, provider="tmdb", external_id="364"),
                MediaIdentifier(media_item_id=unidentified.id, provider="plex", external_id="guid"),
            ]
        )
        db.session.commit()
    client = app.test_client()
    for url in ("/catalog?kind=show&sort=unidentified&direction=desc", "/catalog"):
        response = client.get(url)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert html.index("Z unidentified") < html.index("A identified")
        assert 'value="unidentified" selected' in html
        assert "?v=" in html


def test_projection_groups_historical_and_current_same_source(app: Flask) -> None:
    with app.app_context():
        source, library = _source_with_library(ConnectorType.JELLYFIN, "Jellyfin", "jf")
        old = MediaItem(kind=MediaKind.SHOW, title="Historical")
        current = MediaItem(kind=MediaKind.SHOW, title="Current")
        db.session.add_all([old, current])
        db.session.flush()
        historical_ref = _reference(source, library, old, "old")
        historical_ref.available = False
        db.session.add_all(
            [
                historical_ref,
                _reference(source, library, current, "new"),
                MediaIdentifier(media_item_id=old.id, provider="tmdb", external_id="364"),
                MediaIdentifier(media_item_id=current.id, provider="tmdb", external_id="364"),
            ]
        )
        db.session.commit()
        result = rebuild_catalog_projection(db.session())
        assert result.entries == 1
        assert result.members == 2


def test_projection_groups_sources_prefers_plex_and_searches_all_members(app: Flask) -> None:
    with app.app_context():
        jellyfin, jellyfin_library = _source_with_library(
            ConnectorType.JELLYFIN, "Jellyfin", "jellyfin"
        )
        plex, plex_library = _source_with_library(ConnectorType.PLEX, "Plex", "plex")
        localized = MediaItem(kind=MediaKind.MOVIE, title="A Chegada", year=2016)
        canonical = MediaItem(kind=MediaKind.MOVIE, title="Arrival", year=2016)
        db.session.add_all([localized, canonical])
        db.session.flush()
        db.session.add_all(
            [
                MediaIdentifier(media_item_id=localized.id, provider="tmdb", external_id="329865"),
                MediaIdentifier(media_item_id=canonical.id, provider="tmdb", external_id="329865"),
                _reference(jellyfin, jellyfin_library, localized, "jf-arrival"),
                _reference(plex, plex_library, canonical, "plex-arrival"),
            ]
        )
        db.session.commit()

        result = rebuild_catalog_projection(db.session())
        db.session.commit()

        assert result.entries == 1
        assert result.members == 2
        assert result.duplicate_groups == 1
        entry = db.session.scalar(select(CatalogEntry))
        assert entry is not None
        assert entry.media_item_id == canonical.id
        assert entry.available_plex is True
        assert entry.available_jellyfin is True
        assert db.session.scalar(select(func.count(CatalogEntryMember.media_item_id))) == 2

    page = app.test_client().get("/catalog?kind=movie&query=Chegada").get_data(as_text=True)
    assert "Arrival" in page
    assert "A Chegada" not in page


def test_projection_rolls_activity_up_hierarchy_and_rebuilds_when_dirty(app: Flask) -> None:
    with app.app_context():
        plex, library = _source_with_library(ConnectorType.PLEX, "Plex", "plex")
        show = MediaItem(kind=MediaKind.SHOW, title="Example Show")
        db.session.add(show)
        db.session.flush()
        season = MediaItem(kind=MediaKind.SEASON, title="Season 1", parent_id=show.id)
        db.session.add(season)
        db.session.flush()
        episode = MediaItem(kind=MediaKind.EPISODE, title="Pilot", parent_id=season.id)
        db.session.add(episode)
        db.session.flush()
        db.session.add_all(
            [
                _reference(plex, library, show, "show"),
                _reference(plex, library, episode, "episode"),
                WatchState(
                    media_item_id=episode.id,
                    source_id=plex.id,
                    view_count=1,
                    last_watched_at=NOW,
                    completed=True,
                    observed_at=NOW,
                ),
            ]
        )
        db.session.commit()

        rebuilt = ensure_catalog_projection(db.session())
        db.session.commit()
        assert rebuilt is not None
        show_entry = db.session.get(CatalogEntry, show.id)
        assert show_entry is not None
        assert show_entry.last_played_at == NOW.replace(tzinfo=None)
        assert show_entry.child_count == 1
        assert show_entry.completed_child_count == 1
        assert db.session.get(Setting, PROJECTION_STATE_KEY).value == f"clean:v{PROJECTION_VERSION}"  # type: ignore[union-attr]

        state = db.session.scalar(select(WatchState).where(WatchState.media_item_id == episode.id))
        assert state is not None
        state.last_watched_at = NOW + timedelta(days=1)
        state.observed_at = NOW + timedelta(days=1)
        db.session.commit()
        assert db.session.get(Setting, PROJECTION_STATE_KEY).value == "activity"  # type: ignore[union-attr]
        assert db.session.scalar(select(func.count(CatalogProjectionDirtyItem.media_item_id))) == 1

        refreshed = ensure_catalog_projection(db.session())
        db.session.commit()
        assert refreshed is not None
        assert refreshed.mode == "activity"
        assert refreshed.entries == 2
        assert db.session.scalar(select(func.count(CatalogProjectionDirtyItem.media_item_id))) == 0
        show_entry = db.session.get(CatalogEntry, show.id)
        assert show_entry is not None
        assert show_entry.last_played_at == (NOW + timedelta(days=1)).replace(tzinfo=None)


def test_projection_does_not_hide_ambiguous_items_owned_by_the_same_source(app: Flask) -> None:
    with app.app_context():
        plex, library = _source_with_library(ConnectorType.PLEX, "Plex", "plex")
        first = MediaItem(kind=MediaKind.MOVIE, title="Same Provider ID A")
        second = MediaItem(kind=MediaKind.MOVIE, title="Same Provider ID B")
        db.session.add_all([first, second])
        db.session.flush()
        db.session.add_all(
            [
                MediaIdentifier(media_item_id=first.id, provider="tmdb", external_id="42"),
                MediaIdentifier(media_item_id=second.id, provider="tmdb", external_id="42"),
                _reference(plex, library, first, "first"),
                _reference(plex, library, second, "second"),
            ]
        )
        db.session.commit()

        result = rebuild_catalog_projection(db.session())
        db.session.commit()

        assert result.entries == 2
        assert result.members == 2
        assert result.duplicate_groups == 0


def test_projection_dirty_triggers_are_installed(app: Flask) -> None:
    with app.app_context():
        triggers = set(
            db.session.scalars(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'trg_catalog_dirty_%'"
                )
            )
        )

    assert "trg_catalog_dirty_media_items_insert" in triggers
    assert "trg_catalog_dirty_watch_states_update" in triggers


def test_activity_refresh_does_not_expand_descendants_into_sql_variables(app: Flask) -> None:
    with app.app_context():
        plex, library = _source_with_library(ConnectorType.PLEX, "Plex", "plex")
        artist = MediaItem(kind=MediaKind.ARTIST, title="Large Artist")
        db.session.add(artist)
        db.session.flush()
        album = MediaItem(kind=MediaKind.ALBUM, title="Large Album", parent_id=artist.id)
        db.session.add(album)
        db.session.flush()
        tracks = [
            MediaItem(kind=MediaKind.TRACK, title=f"Track {index}", parent_id=album.id)
            for index in range(15)
        ]
        db.session.add_all(tracks)
        db.session.flush()
        db.session.add_all(
            [
                _reference(plex, library, item, f"item-{item.id}")
                for item in (artist, album, *tracks)
            ]
        )
        db.session.commit()
        rebuild_catalog_projection(db.session())
        db.session.commit()

        db.session.add(
            WatchState(
                media_item_id=artist.id,
                source_id=plex.id,
                view_count=0,
                completed=False,
                progress_ms=1,
                observed_at=NOW,
            )
        )
        db.session.commit()

        driver = db.session.connection().connection.driver_connection
        assert driver is not None
        previous_limit = driver.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 10)
        try:
            refreshed = ensure_catalog_projection(db.session())
            db.session.commit()
        finally:
            driver.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous_limit)

        assert refreshed is not None
        assert refreshed.mode == "activity"
        assert db.session.scalar(select(func.count(CatalogProjectionDirtyItem.media_item_id))) == 0


def _source_with_library(
    connector_type: ConnectorType, name: str, external_id: str
) -> tuple[Source, Library]:
    source = Source(
        connector_type=connector_type,
        name=name,
        base_url=f"http://{external_id}",
        secret="{}",
        enabled=True,
    )
    db.session.add(source)
    db.session.flush()
    library = Library(
        source_id=source.id,
        external_id=external_id,
        name=name,
        media_type=LibraryMediaType.MOVIE,
        enabled=True,
        available=True,
        discovered_at=NOW,
        last_seen_at=NOW,
    )
    db.session.add(library)
    db.session.flush()
    return source, library


def _reference(
    source: Source, library: Library, item: MediaItem, external_id: str
) -> SourceMediaRef:
    return SourceMediaRef(
        source_id=source.id,
        library_id=library.id,
        media_item_id=item.id,
        external_id=external_id,
        last_seen_at=NOW,
        available=True,
    )
