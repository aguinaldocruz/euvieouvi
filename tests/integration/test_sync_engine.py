"""Integration tests for orchestration, persistence and safe resumption."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from flask import Flask
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from euvieouvi.connectors.dtos import (
    ConnectionInfo,
    ExternalIdentifier,
    ExternalLibrary,
    ExternalLibraryRef,
    ExternalLibraryType,
    ExternalMediaItem,
    ExternalMediaKind,
    ExternalWatchEvent,
    HistoryCheckpoint,
    Page,
    PageRequest,
)
from euvieouvi.connectors.errors import ConnectorConnectionError
from euvieouvi.database.enums import (
    ConnectorType,
    LibraryMediaType,
    MediaKind,
    SyncStatus,
    SyncTrigger,
)
from euvieouvi.database.models import (
    Genre,
    Library,
    MediaGenre,
    MediaIdentifier,
    MediaImage,
    MediaItem,
    Source,
    SourceMediaRef,
    SyncCheckpoint,
    SyncError,
    SyncRun,
    SyncRunLibrary,
    WatchEvent,
    WatchState,
)
from euvieouvi.extensions import db
from euvieouvi.sync.cancellation import CancellationToken
from euvieouvi.sync.discovery import LibraryDiscoveryService
from euvieouvi.sync.errors import SyncAlreadyRunningError
from euvieouvi.sync.orchestrator import SyncOrchestrator
from euvieouvi.sync.reconcile import reconcile_orphaned_runs

NOW = datetime(2026, 8, 4, 18, 0, tzinfo=UTC)


class FixtureConnector:
    def __init__(
        self,
        media: dict[str, tuple[ExternalMediaItem, ...]],
        history: dict[str, tuple[ExternalWatchEvent, ...]] | None = None,
        *,
        fail_at: tuple[str, int] | None = None,
        after_page: Callable[[str, int], None] | None = None,
    ) -> None:
        self.media = media
        self.history = history or {}
        self.fail_at = fail_at
        self.after_page = after_page
        self.media_calls: list[tuple[str, int]] = []
        self.history_calls: list[tuple[str, int]] = []

    def test_connection(self) -> ConnectionInfo:
        return ConnectionInfo("Fixture", "fixture-server", True)

    def list_libraries(self) -> list[ExternalLibrary]:
        return []

    def get_media_page(
        self,
        library: ExternalLibraryRef,
        media_kind: ExternalMediaKind,
        page: PageRequest,
    ) -> Page[ExternalMediaItem]:
        del media_kind
        self.media_calls.append((library.external_id, page.start))
        if self.fail_at == (library.external_id, page.start):
            raise ConnectorConnectionError("fixture page failure")
        result = _slice_page(self.media.get(library.external_id, ()), page)
        if self.after_page is not None:
            self.after_page(library.external_id, page.start)
        return result

    def get_history_page(
        self,
        library: ExternalLibraryRef,
        checkpoint: HistoryCheckpoint | None,
        page: PageRequest,
    ) -> Page[ExternalWatchEvent]:
        del checkpoint
        self.history_calls.append((library.external_id, page.start))
        return _slice_page(self.history.get(library.external_id, ()), page)

    def fetch_image(self, source_path: str, *, width: int, height: int) -> tuple[bytes, str]:
        del source_path, width, height
        return b"", "image/jpeg"


def _slice_page[ItemT](items: tuple[ItemT, ...], request: PageRequest) -> Page[ItemT]:
    selected = items[request.start : request.start + request.size]
    next_start = (
        request.start + len(selected) if request.start + len(selected) < len(items) else None
    )
    return Page(
        items=selected,
        start=request.start,
        size=len(selected),
        total_size=len(items),
        next_start=next_start,
    )


def seed_source(*, second_library: bool = False) -> tuple[int, list[int]]:
    source = Source(
        connector_type=ConnectorType.PLEX,
        name="Fixture Plex",
        base_url="http://plex.local:32400",
        secret="sanitized",
        enabled=True,
    )
    db.session.add(source)
    db.session.flush()
    libraries = [
        Library(
            source_id=source.id,
            external_id="movies",
            name="Movies",
            media_type=LibraryMediaType.MOVIE,
            enabled=True,
            available=True,
            discovered_at=NOW,
            last_seen_at=NOW,
        )
    ]
    if second_library:
        libraries.append(
            Library(
                source_id=source.id,
                external_id="shows",
                name="Shows",
                media_type=LibraryMediaType.SHOW,
                enabled=True,
                available=True,
                discovered_at=NOW,
                last_seen_at=NOW,
            )
        )
    db.session.add_all(libraries)
    db.session.commit()
    return source.id, [library.id for library in libraries]


def seed_music_source() -> tuple[int, int]:
    source = Source(
        connector_type=ConnectorType.PLEX,
        name="Music Plex",
        base_url="http://plex.local:32400",
        secret="sanitized",
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
        discovered_at=NOW,
        last_seen_at=NOW,
    )
    db.session.add(library)
    db.session.commit()
    return source.id, library.id


def movie(
    external_id: str,
    *,
    view_count: int | None = None,
    title: str | None = None,
    genres: tuple[str, ...] = (),
    thumb_path: str | None = None,
    identifiers: tuple[ExternalIdentifier, ...] = (),
) -> ExternalMediaItem:
    return ExternalMediaItem(
        external_id=external_id,
        library_external_id="movies",
        kind=ExternalMediaKind.MOVIE,
        title=title or f"Movie {external_id}",
        view_count=view_count,
        last_viewed_at=NOW if view_count else None,
        genres=genres,
        thumb_path=thumb_path,
        identifiers=identifiers,
    )


def episode(number: int, *, watched: bool) -> ExternalMediaItem:
    return ExternalMediaItem(
        external_id=f"episode-{number}",
        library_external_id="shows",
        kind=ExternalMediaKind.EPISODE,
        title=f"Episode {number}",
        show_external_id="futurama",
        show_title="Futurama",
        season_external_id="futurama-season-1",
        season_number=1,
        episode_number=number,
        view_count=1 if watched else 0,
        last_viewed_at=NOW if watched else None,
    )


def track(external_id: str = "track-1") -> ExternalMediaItem:
    return ExternalMediaItem(
        external_id=external_id,
        library_external_id="music",
        kind=ExternalMediaKind.TRACK,
        title="Come Together",
        artist_external_id="artist-1",
        artist_title="The Beatles",
        album_external_id="album-1",
        album_title="Abbey Road",
        disc_number=1,
        track_number=1,
        duration_ms=259000,
        thumb_path="/library/metadata/track-1/thumb",
        artist_thumb_path="/library/metadata/artist-1/thumb",
        album_thumb_path="/library/metadata/album-1/thumb",
        genres=("Rock",),
        view_count=3,
        last_viewed_at=NOW,
    )


def orchestrator(connector: FixtureConnector, *, page_size: int = 2) -> SyncOrchestrator:
    factory = sessionmaker(bind=db.engine, expire_on_commit=False)
    return SyncOrchestrator(factory, connector, page_size=page_size, clock=lambda: NOW)


def test_initial_sync_and_repetition_are_idempotent(app: Flask) -> None:
    with app.app_context():
        source_id, _ = seed_source()
        event = ExternalWatchEvent(
            source_event_id="event-1",
            media_external_id="m1",
            library_external_id="movies",
            watched_at=NOW,
            completed=True,
        )
        connector = FixtureConnector(
            {
                "movies": (
                    movie("m1", view_count=4, genres=("Drama", "Science Fiction")),
                    movie("m2"),
                    movie("m3"),
                )
            },
            {"movies": (event,)},
        )
        engine = orchestrator(connector)

        first = engine.run(source_id)
        second = engine.run(source_id)

        assert first.status is SyncStatus.SUCCEEDED
        assert second.status is SyncStatus.SUCCEEDED
        assert db.session.scalar(select(func.count()).select_from(MediaItem)) == 3
        assert db.session.scalar(select(func.count()).select_from(WatchEvent)) == 1
        state = db.session.scalar(select(WatchState).where(WatchState.view_count == 4))
        assert state is not None
        first_run = db.session.get(SyncRun, first.run_id)
        second_run = db.session.get(SyncRun, second.run_id)
        assert first_run is not None and first_run.items_inserted == 3
        assert second_run is not None and second_run.items_unchanged == 3
        assert db.session.scalar(select(func.count()).select_from(Genre)) == 2
        assert db.session.scalar(select(func.count()).select_from(MediaGenre)) == 2


def test_repeated_history_repairs_existing_event_completion(app: Flask) -> None:
    with app.app_context():
        source_id, _ = seed_source()
        partial = ExternalWatchEvent(
            source_event_id="event-repair",
            media_external_id="m1",
            library_external_id="movies",
            watched_at=NOW,
            completed=False,
            progress_ms=100,
            duration_ms=1000,
        )
        connector = FixtureConnector({"movies": (movie("m1"),)}, {"movies": (partial,)})
        engine = orchestrator(connector)
        assert engine.run(source_id).status is SyncStatus.SUCCEEDED

        connector.history["movies"] = (
            ExternalWatchEvent(
                source_event_id="event-repair",
                media_external_id="m1",
                library_external_id="movies",
                watched_at=NOW,
                completed=True,
                progress_ms=100,
                duration_ms=1000,
                view_number=1,
            ),
        )
        assert engine.run(source_id).status is SyncStatus.SUCCEEDED

        events = db.session.scalars(select(WatchEvent)).all()
        assert len(events) == 1
        assert events[0].completed is True
        assert events[0].view_number == 1


def test_catalog_watched_state_creates_one_known_completion_event(app: Flask) -> None:
    with app.app_context():
        source_id, _ = seed_source()
        connector = FixtureConnector(
            {"movies": (movie("m1", view_count=3),)},
            {"movies": ()},
        )
        engine = orchestrator(connector)

        first_run = engine.run(source_id)
        second_run = engine.run(source_id)
        assert first_run.status is SyncStatus.SUCCEEDED
        assert second_run.status is SyncStatus.SUCCEEDED
        first_stored = db.session.get(SyncRun, first_run.run_id)
        assert first_stored is not None
        assert first_stored.events_inserted == 1
        second_stored = db.session.get(SyncRun, second_run.run_id)
        assert second_stored is not None
        assert second_stored.events_inserted == 0

        events = db.session.scalars(select(WatchEvent)).all()
        assert len(events) == 1
        assert events[0].completed is True
        assert events[0].watched_at == NOW.replace(tzinfo=None)
        assert events[0].view_number == 3


def test_music_sync_persists_artist_album_track_and_history(app: Flask) -> None:
    with app.app_context():
        source_id, _ = seed_music_source()
        event = ExternalWatchEvent(
            source_event_id="listen-1",
            media_external_id="track-1",
            library_external_id="music",
            watched_at=NOW,
            completed=True,
            duration_ms=259000,
        )
        result = orchestrator(FixtureConnector({"music": (track(),)}, {"music": (event,)})).run(
            source_id
        )

        assert result.status is SyncStatus.SUCCEEDED
        items = db.session.scalars(select(MediaItem).order_by(MediaItem.id)).all()
        assert [item.kind for item in items] == [
            MediaKind.ARTIST,
            MediaKind.ALBUM,
            MediaKind.TRACK,
        ]
        assert items[1].parent_id == items[0].id
        assert items[2].parent_id == items[1].id
        assert (items[2].disc_number, items[2].track_number) == (1, 1)
        assert db.session.scalar(select(func.count()).select_from(WatchEvent)) == 1
        images = db.session.scalars(select(MediaImage).order_by(MediaImage.media_item_id)).all()
        assert [image.source_path for image in images] == [
            "/library/metadata/artist-1/thumb",
            "/library/metadata/album-1/thumb",
            "/library/metadata/track-1/thumb",
        ]
        refs = db.session.scalars(select(SourceMediaRef)).all()
        assert all(reference.available for reference in refs)
        assert db.session.scalar(select(func.count()).select_from(MediaGenre)) == 3


def test_music_sync_keeps_existing_album_parent_when_track_artist_varies(app: Flask) -> None:
    with app.app_context():
        source_id, _ = seed_music_source()
        compilation_track = replace(
            track("track-2"),
            artist_external_id="artist-2",
            artist_title="John Barry",
            title="Bobsled Chase",
        )
        result = orchestrator(FixtureConnector({"music": (track(), compilation_track)})).run(
            source_id
        )

        assert result.status is SyncStatus.SUCCEEDED
        album = db.session.scalar(select(MediaItem).where(MediaItem.kind == MediaKind.ALBUM))
        tracks = db.session.scalars(
            select(MediaItem).where(MediaItem.kind == MediaKind.TRACK).order_by(MediaItem.id)
        ).all()
        assert album is not None
        assert len(tracks) == 2
        assert all(item.parent_id == album.id for item in tracks)
        assert (
            db.session.scalar(
                select(func.count())
                .select_from(MediaItem)
                .where(MediaItem.kind == MediaKind.ARTIST)
            )
            == 1
        )


def test_plex_artwork_replaces_external_fallback(app: Flask) -> None:
    with app.app_context():
        source_id, _ = seed_source()
        engine = orchestrator(FixtureConnector({"movies": (movie("m1"),)}))
        assert engine.run(source_id).status is SyncStatus.SUCCEEDED
        media = db.session.scalar(select(MediaItem).where(MediaItem.title == "Movie m1"))
        assert media is not None
        db.session.add(
            MediaImage(
                media_item_id=media.id,
                source_id=None,
                image_type="poster",
                provider="tmdb",
                source_url="https://image.tmdb.org/t/p/w500/fallback.jpg",
                cache_status="pending",
            )
        )
        db.session.commit()
        refreshed = orchestrator(
            FixtureConnector({"movies": (movie("m1", thumb_path="/library/metadata/m1/thumb"),)})
        ).run(source_id)
        assert refreshed.status is SyncStatus.SUCCEEDED
        image = db.session.scalar(select(MediaImage))
        assert image is not None
        assert image.provider == "plex"
        assert image.source_id == source_id
        assert image.source_path == "/library/metadata/m1/thumb"
        assert image.source_url is None


def test_plex_sync_reuses_trakt_movie_and_adds_artwork(app: Flask) -> None:
    with app.app_context():
        source_id, _ = seed_source()
        historical = MediaItem(kind=MediaKind.MOVIE, title="Historical title", year=2016)
        db.session.add(historical)
        db.session.flush()
        db.session.add_all(
            [
                MediaIdentifier(
                    media_item_id=historical.id,
                    provider="tmdb",
                    external_id="329865",
                ),
                WatchEvent(
                    media_item_id=historical.id,
                    source_id=source_id,
                    source_event_id="trakt:1",
                    dedup_key="trakt-event-1",
                    watched_at=NOW,
                    completed=True,
                ),
            ]
        )
        db.session.commit()

        plex_item = movie(
            "plex-rating-key",
            title="Arrival",
            thumb_path="/library/metadata/plex-rating-key/thumb",
            identifiers=(ExternalIdentifier("tmdb", "329865"),),
        )
        result = orchestrator(FixtureConnector({"movies": (plex_item,)})).run(source_id)

        assert result.status is SyncStatus.SUCCEEDED
        assert db.session.scalar(select(func.count()).select_from(MediaItem)) == 1
        reference = db.session.scalar(select(SourceMediaRef))
        image = db.session.scalar(select(MediaImage))
        event = db.session.scalar(select(WatchEvent))
        assert reference is not None and reference.media_item_id == historical.id
        assert reference.external_id == "plex-rating-key"
        assert image is not None and image.media_item_id == historical.id
        assert image.source_path == "/library/metadata/plex-rating-key/thumb"
        assert event is not None and event.media_item_id == historical.id


def test_plex_sync_reuses_trakt_episode_hierarchy(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source(second_library=True)
        movie_library = db.session.get(Library, library_ids[0])
        assert movie_library is not None
        movie_library.enabled = False
        show = MediaItem(kind=MediaKind.SHOW, title="Futurama", year=1999)
        db.session.add(show)
        db.session.flush()
        season = MediaItem(
            kind=MediaKind.SEASON,
            title="Season 1",
            parent_id=show.id,
            season_number=1,
        )
        db.session.add(season)
        db.session.flush()
        historical = MediaItem(
            kind=MediaKind.EPISODE,
            title="Space Pilot 3000",
            parent_id=season.id,
            season_number=1,
            episode_number=1,
        )
        db.session.add(historical)
        db.session.flush()
        db.session.add(
            MediaIdentifier(
                media_item_id=historical.id,
                provider="tvdb",
                external_id="131091",
            )
        )
        db.session.commit()

        plex_item = ExternalMediaItem(
            external_id="episode-plex-1",
            library_external_id="shows",
            kind=ExternalMediaKind.EPISODE,
            title="Space Pilot 3000",
            show_external_id="show-plex",
            show_title="Futurama",
            season_external_id="season-plex-1",
            season_number=1,
            episode_number=1,
            thumb_path="/library/metadata/episode-plex-1/thumb",
            artist_thumb_path="/library/metadata/show-plex/thumb",
            album_thumb_path="/library/metadata/season-plex-1/thumb",
            identifiers=(ExternalIdentifier("tvdb", "131091"),),
        )
        previously_unseen_episode = ExternalMediaItem(
            external_id="episode-plex-2",
            library_external_id="shows",
            kind=ExternalMediaKind.EPISODE,
            title="The Series Has Landed",
            show_external_id="show-plex",
            show_title="Futurama",
            season_external_id="season-plex-1",
            season_number=1,
            episode_number=2,
        )
        result = orchestrator(
            FixtureConnector({"shows": (previously_unseen_episode, plex_item)})
        ).run(source_id)

        assert result.status is SyncStatus.SUCCEEDED
        assert db.session.scalar(select(func.count()).select_from(MediaItem)) == 4
        references = db.session.scalars(select(SourceMediaRef)).all()
        assert {reference.external_id for reference in references} == {
            "show-plex",
            "season-plex-1",
            "episode-plex-1",
            "episode-plex-2",
        }
        assert {reference.media_item_id for reference in references} == {
            show.id,
            season.id,
            historical.id,
            db.session.scalar(
                select(MediaItem.id).where(MediaItem.title == "The Series Has Landed")
            ),
        }
        images = db.session.scalars(select(MediaImage)).all()
        assert {image.media_item_id for image in images} == {
            show.id,
            season.id,
            historical.id,
        }


def test_same_source_duplicate_episode_identifier_keeps_both_hierarchies(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source(second_library=True)
        movie_library = db.session.get(Library, library_ids[0])
        assert movie_library is not None
        movie_library.enabled = False
        shared = (ExternalIdentifier("tvdb", "360134"),)
        first = ExternalMediaItem(
            external_id="special-sg1", library_external_id="shows",
            kind=ExternalMediaKind.EPISODE, title="Sci-Fi Lowdown",
            show_external_id="sg1", show_title="Stargate SG-1",
            season_external_id="sg1-specials", season_number=0, episode_number=2,
            identifiers=shared,
        )
        second = ExternalMediaItem(
            external_id="special-atlantis", library_external_id="shows",
            kind=ExternalMediaKind.EPISODE, title="Sci-Fi Lowdown",
            show_external_id="atlantis", show_title="Stargate Atlantis",
            season_external_id="atlantis-specials", season_number=0, episode_number=1,
            identifiers=shared,
        )
        result = orchestrator(FixtureConnector({"shows": (first, second)})).run(source_id)

        assert result.status is SyncStatus.SUCCEEDED
        episodes = db.session.scalars(
            select(MediaItem).where(MediaItem.kind == MediaKind.EPISODE)
        ).all()
        assert len(episodes) == 2
        assert len({episode.parent_id for episode in episodes}) == 2


def test_provider_match_with_incompatible_episode_season_keeps_both(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source(second_library=True)
        movie_library = db.session.get(Library, library_ids[0])
        assert movie_library is not None
        movie_library.enabled = False
        historical_show = MediaItem(kind=MediaKind.SHOW, title="V")
        db.session.add(historical_show)
        db.session.flush()
        historical_season = MediaItem(
            kind=MediaKind.SEASON, title="Season 1", parent_id=historical_show.id,
            season_number=1,
        )
        db.session.add(historical_season)
        db.session.flush()
        historical_episode = MediaItem(
            kind=MediaKind.EPISODE, title="Part 2", parent_id=historical_season.id,
            season_number=1, episode_number=2,
        )
        db.session.add(historical_episode)
        db.session.flush()
        db.session.add(
            MediaIdentifier(
                media_item_id=historical_episode.id, provider="tvdb", external_id="190756"
            )
        )
        db.session.commit()
        special = ExternalMediaItem(
            external_id="jf-part-2", library_external_id="shows",
            kind=ExternalMediaKind.EPISODE, title="The Original Miniseries (2)",
            show_external_id="jf-v", show_title="V: The Original Miniseries",
            season_external_id="jf-v-specials", season_number=0, episode_number=2,
            identifiers=(ExternalIdentifier("tvdb", "190756"),),
        )

        result = orchestrator(FixtureConnector({"shows": (special,)})).run(source_id)

        assert result.status is SyncStatus.SUCCEEDED
        episodes = db.session.scalars(
            select(MediaItem).where(MediaItem.kind == MediaKind.EPISODE)
        ).all()
        assert len(episodes) == 2
        assert {episode.season_number for episode in episodes} == {0, 1}


def test_partial_series_enumerates_all_episodes_and_preserves_144_watched(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source(second_library=True)
        movie_library = db.session.get(Library, library_ids[0])
        assert movie_library is not None
        movie_library.enabled = False
        db.session.commit()
        episodes = tuple(episode(number, watched=number <= 144) for number in range(1, 162))
        connector = FixtureConnector({"shows": episodes})
        engine = orchestrator(connector, page_size=37)

        first = engine.run(source_id)
        second = engine.run(source_id)

        assert first.status is SyncStatus.SUCCEEDED
        assert len(connector.media_calls) == 10
        assert (
            db.session.scalar(
                select(func.count())
                .select_from(MediaItem)
                .where(MediaItem.kind == MediaKind.EPISODE)
            )
            == 161
        )
        assert (
            db.session.scalar(
                select(func.count()).select_from(WatchState).where(WatchState.completed.is_(True))
            )
            == 144
        )
        second_run = db.session.get(SyncRun, second.run_id)
        assert second_run is not None and second_run.items_unchanged == 161
        containers = db.session.scalars(
            select(WatchState)
            .join(MediaItem, MediaItem.id == WatchState.media_item_id)
            .where(MediaItem.kind.in_([MediaKind.SEASON, MediaKind.SHOW]))
        ).all()
        assert len(containers) == 2
        assert all(state.completed is False and state.view_count == 0 for state in containers)


def test_fully_watched_series_derives_season_and_show_completion(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source(second_library=True)
        movie_library = db.session.get(Library, library_ids[0])
        assert movie_library is not None
        movie_library.enabled = False
        db.session.commit()

        result = orchestrator(
            FixtureConnector({"shows": (episode(1, watched=True), episode(2, watched=True))})
        ).run(source_id)

        assert result.status is SyncStatus.SUCCEEDED
        containers = db.session.execute(
            select(MediaItem.kind, WatchState)
            .join(WatchState, WatchState.media_item_id == MediaItem.id)
            .where(MediaItem.kind.in_([MediaKind.SEASON, MediaKind.SHOW]))
        ).all()
        assert {kind for kind, _ in containers} == {MediaKind.SEASON, MediaKind.SHOW}
        assert all(
            state.completed is True
            and state.view_count == 1
            and state.last_watched_at == NOW.replace(tzinfo=None)
            for _, state in containers
        )


def test_page_failure_keeps_last_confirmed_checkpoint_and_valid_items(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source()
        connector = FixtureConnector(
            {"movies": (movie("1"), movie("2"), movie("3"))},
            fail_at=("movies", 2),
        )

        result = orchestrator(connector).run(source_id)

        checkpoint = db.session.scalar(
            select(SyncCheckpoint).where(SyncCheckpoint.library_id == library_ids[0])
        )
        assert result.status is SyncStatus.FAILED
        assert checkpoint is not None and '"start": 2' in str(checkpoint.cursor)
        assert db.session.scalar(select(func.count()).select_from(SourceMediaRef)) == 2


def test_cancellation_is_observed_between_pages(app: Flask) -> None:
    with app.app_context():
        source_id, _ = seed_source()
        token = CancellationToken()
        connector = FixtureConnector(
            {"movies": (movie("1"), movie("2"), movie("3"))},
            after_page=lambda library, start: token.cancel(),
        )

        result = orchestrator(connector).run(source_id, cancellation=token)

        assert result.status is SyncStatus.INTERRUPTED
        stored_run = db.session.get(SyncRun, result.run_id)
        assert stored_run is not None and stored_run.finished_at == NOW.replace(tzinfo=None)
        assert db.session.scalar(select(func.count()).select_from(SourceMediaRef)) == 2


def test_active_run_blocks_a_second_execution(app: Flask) -> None:
    with app.app_context():
        source_id, _ = seed_source()
        db.session.add(
            SyncRun(
                source_id=source_id,
                trigger=SyncTrigger.MANUAL,
                status=SyncStatus.RUNNING,
                started_at=NOW,
            )
        )
        db.session.commit()

        with pytest.raises(SyncAlreadyRunningError):
            orchestrator(FixtureConnector({"movies": ()})).run(source_id)


def test_full_success_marks_missing_refs_unavailable_but_failure_does_not(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source()
        media = MediaItem(kind=MediaKind.MOVIE, title="Old")
        db.session.add(media)
        db.session.flush()
        old_ref = SourceMediaRef(
            source_id=source_id,
            library_id=library_ids[0],
            media_item_id=media.id,
            external_id="old",
            last_seen_at=NOW,
            available=True,
        )
        db.session.add(old_ref)
        db.session.commit()

        failure = orchestrator(FixtureConnector({"movies": ()}, fail_at=("movies", 0))).run(
            source_id
        )
        assert failure.status is SyncStatus.FAILED
        db.session.expire_all()
        stored_ref = db.session.get(SourceMediaRef, old_ref.id)
        assert stored_ref is not None and stored_ref.available is True

        success = orchestrator(FixtureConnector({"movies": ()})).run(source_id)
        assert success.status is SyncStatus.SUCCEEDED
        db.session.expire_all()
        stored_ref = db.session.get(SourceMediaRef, old_ref.id)
        assert stored_ref is not None and stored_ref.available is False
        assert stored_ref.unavailable_since is not None
        assert stored_ref.unavailable_since.replace(tzinfo=UTC) == NOW

        restored = orchestrator(FixtureConnector({"movies": (movie("old"),)})).run(source_id)
        assert restored.status is SyncStatus.SUCCEEDED
        db.session.expire_all()
        stored_ref = db.session.get(SourceMediaRef, old_ref.id)
        assert stored_ref is not None and stored_ref.available is True
        assert stored_ref.unavailable_since is None


def test_item_savepoint_preserves_valid_item_and_blocks_checkpoint(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source(second_library=True)
        conflict = movie("shared")
        first_run = orchestrator(FixtureConnector({"movies": (conflict,), "shows": ()})).run(
            source_id
        )
        assert first_run.status is SyncStatus.SUCCEEDED
        bad_episode = ExternalMediaItem(
            external_id="episode-bad",
            library_external_id="shows",
            kind=ExternalMediaKind.EPISODE,
            title="Bad hierarchy",
            show_external_id="shared",
            show_title="Conflict",
            season_number=1,
            episode_number=1,
        )
        connector = FixtureConnector({"movies": (conflict,), "shows": (bad_episode,)})

        result = orchestrator(connector).run(source_id)

        assert result.status is SyncStatus.FAILED
        errors = db.session.scalars(
            select(SyncError).where(
                SyncError.sync_run_id == result.run_id,
                SyncError.category == "item_persistence",
            )
        ).all()
        assert len(errors) == 1
        checkpoint = db.session.scalar(
            select(SyncCheckpoint).where(SyncCheckpoint.library_id == library_ids[1])
        )
        assert checkpoint is not None
        assert checkpoint.cursor is None
        assert checkpoint.last_successful_run_id == first_run.run_id


def test_snapshot_does_not_change_when_selection_changes_mid_run(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source(second_library=True)

        def disable_second(library: str, start: int) -> None:
            if library == "movies" and start == 0:
                second_library = db.session.get(Library, library_ids[1])
                assert second_library is not None
                second_library.enabled = False
                db.session.commit()

        connector = FixtureConnector(
            {"movies": (movie("1"),), "shows": (episode(1, watched=True),)},
            after_page=disable_second,
        )

        result = orchestrator(connector).run(source_id)

        assert result.status is SyncStatus.SUCCEEDED
        assert ("shows", 0) in connector.media_calls
        details = db.session.scalars(
            select(SyncRunLibrary).where(SyncRunLibrary.sync_run_id == result.run_id)
        ).all()
        assert len(details) == 2


def test_failed_run_resumes_from_confirmed_page(app: Flask) -> None:
    with app.app_context():
        source_id, _ = seed_source()
        media = (movie("1"), movie("2"), movie("3"))
        failed = FixtureConnector({"movies": media}, fail_at=("movies", 2))
        assert orchestrator(failed).run(source_id).status is SyncStatus.FAILED

        resumed = FixtureConnector({"movies": media})
        result = orchestrator(resumed).run(source_id)

        assert result.status is SyncStatus.SUCCEEDED
        assert resumed.media_calls[0] == ("movies", 2)
        assert db.session.scalar(select(func.count()).select_from(MediaItem)) == 3


def test_lower_view_count_is_not_applied_silently(app: Flask) -> None:
    with app.app_context():
        source_id, _ = seed_source()
        engine = orchestrator(FixtureConnector({"movies": (movie("1", view_count=4),)}))
        assert engine.run(source_id).status is SyncStatus.SUCCEEDED

        second = orchestrator(FixtureConnector({"movies": (movie("1", view_count=2),)})).run(
            source_id
        )

        state = db.session.scalar(select(WatchState))
        assert state is not None and state.view_count == 4
        warning = db.session.scalar(
            select(SyncError).where(
                SyncError.sync_run_id == second.run_id,
                SyncError.category == "view_count_regression",
            )
        )
        assert warning is not None


def test_startup_reconciliation_interrupts_run_and_snapshot(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source()
        run = SyncRun(
            source_id=source_id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.RUNNING,
            started_at=NOW,
        )
        db.session.add(run)
        db.session.flush()
        detail = SyncRunLibrary(
            sync_run_id=run.id,
            library_id=library_ids[0],
            status=SyncStatus.RUNNING,
            started_at=NOW,
        )
        db.session.add(detail)
        db.session.commit()

        assert reconcile_orphaned_runs() == 1

        db.session.expire_all()
        assert db.session.get(SyncRun, run.id).status is SyncStatus.INTERRUPTED  # type: ignore[union-attr]
        assert db.session.get(SyncRunLibrary, detail.id).status is SyncStatus.INTERRUPTED  # type: ignore[union-attr]


def test_discovery_preserves_selection_and_marks_absence_only_after_success(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source()
        existing = db.session.get(Library, library_ids[0])
        assert existing is not None and existing.enabled is True

        connector = FixtureConnector({})
        connector.list_libraries = lambda: [  # type: ignore[method-assign]
            ExternalLibrary("movies", "Renamed Movies", ExternalLibraryType.MOVIE),
            ExternalLibrary("shows", "Shows", ExternalLibraryType.SHOW),
        ]
        factory = sessionmaker(bind=db.engine, expire_on_commit=False)
        service = LibraryDiscoveryService(factory, connector, clock=lambda: NOW)

        assert service.discover(source_id) == 2

        db.session.expire_all()
        existing = db.session.get(Library, library_ids[0])
        added = db.session.scalar(select(Library).where(Library.external_id == "shows"))
        assert existing is not None and existing.enabled is True
        assert existing.name == "Renamed Movies"
        assert added is not None and added.enabled is False

        connector.list_libraries = lambda: []  # type: ignore[method-assign]
        service.discover(source_id)
        db.session.expire_all()
        assert db.session.get(Library, library_ids[0]).available is False  # type: ignore[union-attr]


def test_failed_discovery_does_not_mark_existing_library_unavailable(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source()
        connector = FixtureConnector({})

        def fail_discovery() -> list[ExternalLibrary]:
            raise ConnectorConnectionError("fixture discovery failure")

        connector.list_libraries = fail_discovery  # type: ignore[method-assign]
        factory = sessionmaker(bind=db.engine, expire_on_commit=False)

        with pytest.raises(ConnectorConnectionError):
            LibraryDiscoveryService(factory, connector, clock=lambda: NOW).discover(source_id)

        db.session.expire_all()
        library = db.session.get(Library, library_ids[0])
        source = db.session.get(Source, source_id)
        assert library is not None and library.available is True
        assert source is not None and source.last_connection_status == "failed"


def test_ambiguous_episode_identifier_keeps_source_specific_item(app: Flask) -> None:
    with app.app_context():
        source_id, library_ids = seed_source(second_library=True)
        movie_library = db.session.get(Library, library_ids[0])
        assert movie_library is not None
        movie_library.enabled = False
        for copy_index in range(2):
            show = MediaItem(kind=MediaKind.SHOW, title="Os Padrinhos Mágicos")
            db.session.add(show)
            db.session.flush()
            season = MediaItem(
                kind=MediaKind.SEASON,
                title="Specials",
                parent_id=show.id,
                season_number=0,
            )
            db.session.add(season)
            db.session.flush()
            if copy_index == 0:
                db.session.add_all(
                    [
                        SourceMediaRef(
                            source_id=source_id, library_id=library_ids[1],
                            media_item_id=show.id, external_id="jf-fairly-oddparents",
                            last_seen_at=NOW, available=True,
                        ),
                        SourceMediaRef(
                            source_id=source_id, library_id=library_ids[1],
                            media_item_id=season.id, external_id="jf-specials",
                            last_seen_at=NOW, available=True,
                        ),
                    ]
                )
            existing = MediaItem(
                kind=MediaKind.EPISODE,
                title="Merry Wishmas",
                parent_id=season.id,
                season_number=0,
                episode_number=11,
            )
            db.session.add(existing)
            db.session.flush()
            db.session.add(
                MediaIdentifier(
                    media_item_id=existing.id,
                    provider="tvdb",
                    external_id="1492111",
                )
            )
        db.session.commit()
        jellyfin_item = ExternalMediaItem(
            external_id="jf-wishmas",
            library_external_id="shows",
            kind=ExternalMediaKind.EPISODE,
            title="Feliz Desejo de Natal",
            show_external_id="jf-fairly-oddparents",
            show_title="Os Padrinhos Mágicos",
            season_external_id="jf-specials",
            season_number=0,
            episode_number=11,
            identifiers=(ExternalIdentifier("tvdb", "1492111"),),
        )

        result = orchestrator(FixtureConnector({"shows": (jellyfin_item,)})).run(source_id)

        assert result.status is SyncStatus.SUCCEEDED
        assert db.session.scalar(
            select(func.count()).select_from(MediaItem).where(MediaItem.kind == MediaKind.EPISODE)
        ) == 3
        assert db.session.scalar(
            select(SourceMediaRef).where(
                SourceMediaRef.source_id == source_id,
                SourceMediaRef.external_id == "jf-wishmas",
            )
        ) is not None
