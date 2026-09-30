"""Safe duplicate catalog reconciliation."""

from datetime import UTC, datetime

import pytest
from flask import Flask
from sqlalchemy import select

from euvieouvi.database.enums import ConnectorType, LibraryMediaType, MediaKind
from euvieouvi.database.models import (
    Library,
    MediaIdentifier,
    MediaImage,
    MediaItem,
    Setting,
    Source,
    SourceMediaRef,
    WatchEvent,
    WatchState,
)
from euvieouvi.extensions import db
from euvieouvi.sync.catalog_reconcile import reconcile_catalog

NOW = datetime(2026, 8, 14, tzinfo=UTC)


@pytest.mark.parametrize("episode_has_identifier", [True, False])
def test_reconcile_dry_run_then_merges_show_hierarchy_and_history(
    app: Flask, episode_has_identifier: bool
) -> None:
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
        show_b = MediaItem(kind=MediaKind.SHOW, title="Exemplo [tmdb-42]")
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
                MediaIdentifier(media_item_id=episode_a.id, provider="tvdb", external_id="99"),
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
        if episode_has_identifier:
            db.session.add(
                MediaIdentifier(media_item_id=episode_b.id, provider="tvdb", external_id="99")
            )
        db.session.commit()

        preview = reconcile_catalog(db.session(), dry_run=True)
        assert preview.groups_found == 1
        assert preview.items_merged == 1
        assert preview.identifiers_recovered == 1
        assert preview.titles_normalized == 1
        assert db.session.get(MediaItem, show_b.id) is not None
        assert db.session.get(MediaItem, show_b.id).title == "Exemplo [tmdb-42]"  # type: ignore[union-attr]

        result = reconcile_catalog(db.session(), dry_run=False)
        assert result.items_merged == 1
        assert result.hierarchy_merged == 2
        assert result.identifiers_recovered == 1
        projection_state = db.session.get(Setting, "catalog.projection.state")
        assert projection_state is not None and projection_state.value == "full"
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


def test_reconcile_merges_orphan_and_inactive_predecessor_but_not_two_live_editions(
    app: Flask,
) -> None:
    with app.app_context():
        plex = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex",
            secret="x",
            enabled=True,
        )
        db.session.add(plex)
        db.session.flush()
        library = Library(
            source_id=plex.id,
            external_id="movies",
            name="Movies",
            media_type=LibraryMediaType.MOVIE,
            enabled=True,
            available=True,
            discovered_at=NOW,
            last_seen_at=NOW,
        )
        merge_items = [
            MediaItem(kind=MediaKind.MOVIE, title=value)
            for value in ("Previous", "Current", "Identifier-only")
        ]
        live_editions = [
            MediaItem(kind=MediaKind.MOVIE, title=value) for value in ("Edition A", "Edition B")
        ]
        db.session.add_all([library, *merge_items, *live_editions])
        db.session.flush()
        db.session.add_all(
            [
                SourceMediaRef(
                    source_id=plex.id,
                    library_id=library.id,
                    media_item_id=merge_items[1].id,
                    external_id="current",
                    last_seen_at=NOW,
                    available=True,
                ),
                SourceMediaRef(
                    source_id=plex.id,
                    library_id=library.id,
                    media_item_id=merge_items[0].id,
                    external_id="previous",
                    last_seen_at=NOW,
                    available=False,
                ),
                *[
                    SourceMediaRef(
                        source_id=plex.id,
                        library_id=library.id,
                        media_item_id=item.id,
                        external_id=f"edition-{index}",
                        last_seen_at=NOW,
                        available=True,
                    )
                    for index, item in enumerate(live_editions)
                ],
                *[
                    MediaIdentifier(
                        media_item_id=item.id,
                        provider="tmdb",
                        external_id="merge-1",
                    )
                    for item in merge_items
                ],
                *[
                    MediaIdentifier(
                        media_item_id=item.id,
                        provider="tmdb",
                        external_id="editions-2",
                    )
                    for item in live_editions
                ],
            ]
        )
        db.session.commit()
        winner_id = merge_items[1].id
        removed_ids = {merge_items[0].id, merge_items[2].id}
        edition_ids = {item.id for item in live_editions}

        result = reconcile_catalog(db.session(), dry_run=False)

        assert result.groups_found == 2
        assert result.items_merged == 2
        assert result.skipped == 1
        assert db.session.get(MediaItem, winner_id) is not None
        assert all(db.session.get(MediaItem, media_id) is None for media_id in removed_ids)
        assert all(db.session.get(MediaItem, media_id) is not None for media_id in edition_ids)


def test_embedded_backfill_preserves_conflicting_structured_identity(app: Flask) -> None:
    with app.app_context():
        movie = MediaItem(kind=MediaKind.MOVIE, title="Movie [tmdb-999]")
        db.session.add(movie)
        db.session.flush()
        db.session.add(MediaIdentifier(media_item_id=movie.id, provider="tmdb", external_id="42"))
        db.session.commit()
        movie_id = movie.id
        result = reconcile_catalog(db.session(), dry_run=False)
        assert result.identifier_conflicts == 1
        assert result.identifiers_recovered == 0
        loaded = db.session.get(MediaItem, movie_id)
        assert loaded is not None and loaded.title == "Movie [tmdb-999]"
        values = db.session.scalars(
            select(MediaIdentifier.external_id).where(MediaIdentifier.media_item_id == movie_id)
        ).all()
        assert values == ["42"]
