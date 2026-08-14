"""Safe duplicate catalog reconciliation."""

from datetime import UTC, datetime

from flask import Flask
from sqlalchemy import select

from euvieouvi.database.enums import ConnectorType, LibraryMediaType, MediaKind
from euvieouvi.database.models import (
    Library,
    MediaIdentifier,
    MediaImage,
    MediaItem,
    Source,
    SourceMediaRef,
    WatchEvent,
    WatchState,
)
from euvieouvi.extensions import db
from euvieouvi.sync.catalog_reconcile import reconcile_catalog

NOW = datetime(2026, 8, 14, tzinfo=UTC)


def test_reconcile_dry_run_then_merges_show_hierarchy_and_history(app: Flask) -> None:
    with app.app_context():
        plex = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex",
            secret="x",
            enabled=True,
        )
        jellyfin = Source(
            connector_type=ConnectorType.JELLYFIN,
            name="Jellyfin",
            base_url="http://jellyfin",
            secret='{"api_key":"x","user_id":"u"}',
            enabled=True,
        )
        db.session.add_all([plex, jellyfin])
        db.session.flush()
        libraries = [
            Library(
                source_id=source.id,
                external_id=str(source.id),
                name=source.name,
                media_type=LibraryMediaType.SHOW,
                enabled=True,
                available=True,
                discovered_at=NOW,
                last_seen_at=NOW,
            )
            for source in (plex, jellyfin)
        ]
        db.session.add_all(libraries)
        db.session.flush()
        show_a = MediaItem(kind=MediaKind.SHOW, title="Example")
        show_b = MediaItem(kind=MediaKind.SHOW, title="Exemplo")
        db.session.add_all([show_a, show_b])
        db.session.flush()
        season_a = MediaItem(
            kind=MediaKind.SEASON, title="Season 1", parent_id=show_a.id, season_number=1
        )
        season_b = MediaItem(
            kind=MediaKind.SEASON, title="Temporada 1", parent_id=show_b.id, season_number=1
        )
        db.session.add_all([season_a, season_b])
        db.session.flush()
        episode_a = MediaItem(
            kind=MediaKind.EPISODE, title="Pilot", parent_id=season_a.id, episode_number=1
        )
        episode_b = MediaItem(
            kind=MediaKind.EPISODE, title="Piloto", parent_id=season_b.id, episode_number=1
        )
        db.session.add_all([episode_a, episode_b])
        db.session.flush()
        for media, source, library, external in (
            (show_a, plex, libraries[0], "show-p"),
            (season_a, plex, libraries[0], "season-p"),
            (episode_a, plex, libraries[0], "episode-p"),
            (show_b, jellyfin, libraries[1], "show-j"),
            (season_b, jellyfin, libraries[1], "season-j"),
            (episode_b, jellyfin, libraries[1], "episode-j"),
        ):
            db.session.add(
                SourceMediaRef(
                    source_id=source.id,
                    library_id=library.id,
                    media_item_id=media.id,
                    external_id=external,
                    last_seen_at=NOW,
                    available=True,
                )
            )
        db.session.add_all(
            [
                MediaIdentifier(media_item_id=show_a.id, provider="tmdb", external_id="42"),
                MediaIdentifier(media_item_id=show_b.id, provider="tmdb", external_id="42"),
                MediaIdentifier(media_item_id=episode_a.id, provider="tvdb", external_id="99"),
                MediaIdentifier(media_item_id=episode_b.id, provider="tvdb", external_id="99"),
                MediaImage(media_item_id=show_b.id, image_type="poster", provider="jellyfin"),
                WatchEvent(
                    media_item_id=episode_b.id,
                    source_id=jellyfin.id,
                    source_event_id="event",
                    dedup_key="event",
                    watched_at=NOW,
                    completed=True,
                    origin="synchronization",
                ),
                WatchState(
                    media_item_id=episode_b.id,
                    source_id=jellyfin.id,
                    view_count=1,
                    last_watched_at=NOW,
                    completed=True,
                    observed_at=NOW,
                ),
            ]
        )
        db.session.commit()

        preview = reconcile_catalog(db.session(), dry_run=True)
        assert preview.groups_found == 1
        assert preview.items_merged == 1
        assert db.session.get(MediaItem, show_b.id) is not None

        result = reconcile_catalog(db.session(), dry_run=False)
        assert result.items_merged == 1
        assert result.hierarchy_merged == 2
        assert db.session.get(MediaItem, show_b.id) is None
        assert db.session.get(MediaItem, season_b.id) is None
        assert db.session.get(MediaItem, episode_b.id) is None
        assert (
            len(
                db.session.scalars(
                    select(SourceMediaRef).where(SourceMediaRef.media_item_id == show_a.id)
                ).all()
            )
            == 2
        )
        event = db.session.scalar(select(WatchEvent).where(WatchEvent.source_event_id == "event"))
        assert event is not None and event.media_item_id == episode_a.id
