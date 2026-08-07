"""Map neutral connector facts into the approved persistence model."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select

from euvieouvi.connectors.dtos import ExternalMediaItem, ExternalMediaKind, ExternalWatchEvent
from euvieouvi.database.enums import MediaKind
from euvieouvi.database.models import (
    Genre,
    MediaGenre,
    MediaIdentifier,
    MediaImage,
    MediaItem,
    SourceMediaRef,
    WatchEvent,
    WatchState,
)
from euvieouvi.database.unit_of_work import UnitOfWork


class ItemClassification(StrEnum):
    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class PersistResult:
    classification: ItemClassification
    media_item_id: int
    view_count_regression: bool = False
    event_inserted: bool = False


class MediaPersistenceService:
    """Persist one item or event using repositories from an existing transaction."""

    def __init__(self, work: UnitOfWork, *, source_id: int, library_id: int) -> None:
        self.work = work
        self.source_id = source_id
        self.library_id = library_id

    def persist_media(self, item: ExternalMediaItem, observed_at: datetime) -> PersistResult:
        reference = self.work.source_media_refs.by_external_identity(
            self.source_id, item.external_id
        )
        matched_media = None if reference is not None else self._match_by_identifiers(item)
        parent_id = self._ensure_hierarchy(item, observed_at, matched_media=matched_media)
        signature = media_signature(item)
        if reference is None:
            media = matched_media or MediaItem(
                kind=MediaKind(item.kind.value), title=item.title, parent_id=parent_id
            )
            self._apply_media(media, item, parent_id)
            if matched_media is None:
                self.work.media_items.add(media)
                self.work.session.flush()
            reference = SourceMediaRef(
                source_id=self.source_id,
                library_id=self.library_id,
                media_item_id=media.id,
                external_id=item.external_id,
                external_key=item.external_key,
                external_updated_at=item.updated_at,
                last_seen_at=observed_at,
                available=True,
                raw_hash=signature,
            )
            self.work.source_media_refs.add(reference)
            classification = (
                ItemClassification.UPDATED
                if matched_media is not None
                else ItemClassification.INSERTED
            )
        else:
            media = self._required_media(reference.media_item_id)
            classification = (
                ItemClassification.UNCHANGED
                if reference.raw_hash == signature
                else ItemClassification.UPDATED
            )
            if classification is ItemClassification.UPDATED:
                self._apply_media(media, item, parent_id)
            reference.external_key = item.external_key
            reference.external_updated_at = item.updated_at
            reference.last_seen_at = observed_at
            reference.available = True
            reference.unavailable_since = None
            reference.raw_hash = signature

        self._sync_identifiers(media.id, item)
        self._sync_genres(media.id, item.genres)
        self._sync_image(media.id, "poster", item.thumb_path)
        self._sync_image(media.id, "backdrop", item.art_path)
        regression = self._sync_watch_state(media.id, item, observed_at)
        event_inserted = False
        if item.view_count and item.last_viewed_at is not None:
            event_inserted = self.persist_event(
                ExternalWatchEvent(
                    media_external_id=item.external_id,
                    library_external_id=item.library_external_id,
                    watched_at=item.last_viewed_at,
                    completed=True,
                    source_event_id=(f"state:{item.external_id}:{item.last_viewed_at.isoformat()}"),
                    duration_ms=item.duration_ms,
                    view_number=item.view_count,
                )
            )
        return PersistResult(classification, media.id, regression, event_inserted)

    def persist_event(self, event: ExternalWatchEvent) -> bool:
        if not event.completed:
            return False
        reference = self.work.source_media_refs.by_external_identity(
            self.source_id, event.media_external_id
        )
        if reference is None:
            raise ValueError("History event references unknown media.")
        dedup_key = event_dedup_key(self.source_id, event)
        if event.source_event_id is not None:
            existing = self.work.watch_events.by_source_event_id(
                self.source_id, event.source_event_id
            )
            if existing is not None:
                if existing.media_item_id != reference.media_item_id:
                    raise ValueError("History event identity changed its referenced media.")
                existing.dedup_key = dedup_key
                existing.watched_at = event.watched_at
                existing.completed = event.completed
                existing.progress_ms = event.progress_ms
                existing.duration_ms = event.duration_ms
                existing.view_number = event.view_number
                return False
        if self.work.watch_events.by_dedup_key(self.source_id, dedup_key) is not None:
            return False
        nearby = self.work.session.scalar(
            select(WatchEvent)
            .where(
                WatchEvent.media_item_id == reference.media_item_id,
                WatchEvent.completed.is_(True),
                WatchEvent.watched_at.between(
                    event.watched_at - timedelta(minutes=2),
                    event.watched_at + timedelta(minutes=2),
                ),
            )
            .order_by(WatchEvent.id)
        )
        if nearby is not None:
            return False
        self.work.watch_events.add(
            WatchEvent(
                media_item_id=reference.media_item_id,
                source_id=self.source_id,
                source_event_id=event.source_event_id,
                dedup_key=dedup_key,
                watched_at=event.watched_at,
                completed=event.completed,
                progress_ms=event.progress_ms,
                duration_ms=event.duration_ms,
                view_number=event.view_number,
            )
        )
        return True

    def rebuild_container_watch_states(self, observed_at: datetime) -> None:
        """Derive season/show and album/artist state from their playable children."""
        playable = self.work.session.execute(
            select(MediaItem.id, MediaItem.parent_id)
            .join(SourceMediaRef, SourceMediaRef.media_item_id == MediaItem.id)
            .where(
                SourceMediaRef.source_id == self.source_id,
                SourceMediaRef.library_id == self.library_id,
                SourceMediaRef.available.is_(True),
                MediaItem.kind.in_([MediaKind.EPISODE, MediaKind.TRACK]),
            )
            .distinct()
        ).all()
        if not playable:
            return
        playable_ids = [int(row.id) for row in playable]
        states = {
            state.media_item_id: state
            for state in self.work.session.scalars(
                select(WatchState).where(
                    WatchState.source_id == self.source_id,
                    WatchState.media_item_id.in_(playable_ids),
                )
            )
        }
        event_facts = {
            int(media_id): (int(count), last_watched)
            for media_id, count, last_watched in self.work.session.execute(
                select(
                    WatchEvent.media_item_id,
                    func.count(WatchEvent.id),
                    func.max(WatchEvent.watched_at),
                )
                .where(
                    WatchEvent.media_item_id.in_(playable_ids),
                    WatchEvent.source_id == self.source_id,
                    WatchEvent.completed.is_(True),
                )
                .group_by(WatchEvent.media_item_id)
            )
        }
        parent_ids = {int(row.parent_id) for row in playable if row.parent_id is not None}
        parents = {
            item.id: item
            for item in self.work.session.scalars(
                select(MediaItem).where(MediaItem.id.in_(parent_ids))
            )
        }
        grouped: dict[int, list[tuple[int, int, datetime | None]]] = {}
        for row in playable:
            if row.parent_id is None:
                continue
            state = states.get(int(row.id))
            event_count, event_last = event_facts.get(int(row.id), (0, None))
            known_count = max(state.view_count if state is not None else 0, event_count)
            last_watched = _latest_datetime(
                state.last_watched_at if state is not None else None,
                event_last,
            )
            grouped.setdefault(int(row.parent_id), []).append(
                (int(row.id), known_count, last_watched)
            )
        top_grouped: dict[int, list[tuple[int, int, datetime | None]]] = {}
        for parent_id, children in grouped.items():
            count, last_watched = _aggregate_completion(children)
            self._set_derived_watch_state(parent_id, count, last_watched, observed_at)
            parent = parents.get(parent_id)
            if parent is not None and parent.parent_id is not None:
                top_grouped.setdefault(parent.parent_id, []).extend(children)
        for parent_id, children in top_grouped.items():
            count, last_watched = _aggregate_completion(children)
            self._set_derived_watch_state(parent_id, count, last_watched, observed_at)

    def _set_derived_watch_state(
        self,
        media_item_id: int,
        view_count: int,
        last_watched_at: datetime | None,
        observed_at: datetime,
    ) -> None:
        state = self.work.watch_states.by_item_and_source(media_item_id, self.source_id)
        if state is None:
            state = WatchState(
                media_item_id=media_item_id,
                source_id=self.source_id,
                view_count=view_count,
                completed=view_count > 0,
                observed_at=observed_at,
            )
            self.work.watch_states.add(state)
        state.view_count = view_count
        state.completed = view_count > 0
        state.last_watched_at = last_watched_at
        state.progress_ms = None
        state.observed_at = observed_at

    def _ensure_hierarchy(
        self,
        item: ExternalMediaItem,
        observed_at: datetime,
        *,
        matched_media: MediaItem | None,
    ) -> int | None:
        if item.kind is ExternalMediaKind.TRACK:
            assert item.artist_external_id is not None
            assert item.artist_title is not None
            assert item.album_external_id is not None
            assert item.album_title is not None
            artist = self._ensure_container(
                external_id=item.artist_external_id,
                kind=MediaKind.ARTIST,
                title=item.artist_title,
                parent_id=None,
                season_number=None,
                image_source_path=item.artist_thumb_path,
                genres=item.genres,
                observed_at=observed_at,
            )
            album = self._ensure_container(
                external_id=item.album_external_id,
                kind=MediaKind.ALBUM,
                title=item.album_title,
                parent_id=artist.id,
                season_number=None,
                image_source_path=item.album_thumb_path,
                genres=item.genres,
                observed_at=observed_at,
            )
            return album.id
        if item.kind is not ExternalMediaKind.EPISODE:
            return None
        assert item.show_external_id is not None
        assert item.show_title is not None
        assert item.season_number is not None
        if matched_media is not None:
            return self._bind_existing_episode_hierarchy(item, matched_media, observed_at)
        show = self._ensure_container(
            external_id=item.show_external_id,
            kind=MediaKind.SHOW,
            title=item.show_title,
            parent_id=None,
            season_number=None,
            image_source_path=item.artist_thumb_path,
            genres=item.genres,
            observed_at=observed_at,
        )
        season_external_id = item.season_external_id or (
            f"{item.show_external_id}:season:{item.season_number}"
        )
        season = self._ensure_container(
            external_id=season_external_id,
            kind=MediaKind.SEASON,
            title=f"Season {item.season_number}",
            parent_id=show.id,
            season_number=item.season_number,
            image_source_path=item.album_thumb_path,
            genres=item.genres,
            observed_at=observed_at,
        )
        return season.id

    def _match_by_identifiers(self, item: ExternalMediaItem) -> MediaItem | None:
        identities = tuple(
            (identifier.provider, identifier.external_id) for identifier in item.identifiers
        )
        matches = self.work.media_identifiers.media_ids_for_kind(item.kind.value, identities)
        if len(matches) > 1:
            raise ValueError("External identifiers match more than one catalog item.")
        if not matches:
            return None
        return self._required_media(matches.pop())

    def _bind_existing_episode_hierarchy(
        self,
        item: ExternalMediaItem,
        episode: MediaItem,
        observed_at: datetime,
    ) -> int:
        season = self._required_media(episode.parent_id or 0)
        show = self._required_media(season.parent_id or 0)
        if (
            episode.kind is not MediaKind.EPISODE
            or season.kind is not MediaKind.SEASON
            or show.kind is not MediaKind.SHOW
            or season.season_number != item.season_number
        ):
            raise ValueError("Matched episode has an incompatible historical hierarchy.")
        assert item.show_external_id is not None
        assert item.show_title is not None
        show.title = item.show_title
        self._bind_existing_container(
            show,
            external_id=item.show_external_id,
            image_source_path=item.artist_thumb_path,
            genres=item.genres,
            observed_at=observed_at,
        )
        season_external_id = item.season_external_id or (
            f"{item.show_external_id}:season:{item.season_number}"
        )
        self._bind_existing_container(
            season,
            external_id=season_external_id,
            image_source_path=item.album_thumb_path,
            genres=item.genres,
            observed_at=observed_at,
        )
        return season.id

    def _bind_existing_container(
        self,
        media: MediaItem,
        *,
        external_id: str,
        image_source_path: str | None,
        genres: tuple[str, ...],
        observed_at: datetime,
    ) -> None:
        reference = self.work.source_media_refs.by_external_identity(self.source_id, external_id)
        if reference is not None and reference.media_item_id != media.id:
            raise ValueError("External hierarchy identity conflicts with historical media.")
        if reference is None:
            self.work.source_media_refs.add(
                SourceMediaRef(
                    source_id=self.source_id,
                    library_id=self.library_id,
                    media_item_id=media.id,
                    external_id=external_id,
                    last_seen_at=observed_at,
                    available=True,
                )
            )
        else:
            reference.last_seen_at = observed_at
            reference.available = True
            reference.unavailable_since = None
        self._sync_image(media.id, "poster", image_source_path)
        self._merge_genres(media.id, genres)

    def _ensure_container(
        self,
        *,
        external_id: str,
        kind: MediaKind,
        title: str,
        parent_id: int | None,
        season_number: int | None,
        image_source_path: str | None,
        genres: tuple[str, ...],
        observed_at: datetime,
    ) -> MediaItem:
        reference = self.work.source_media_refs.by_external_identity(self.source_id, external_id)
        if reference is not None:
            reference.last_seen_at = observed_at
            reference.available = True
            reference.unavailable_since = None
            media = self._required_media(reference.media_item_id)
            if media.kind is not kind or media.parent_id != parent_id:
                raise ValueError("External hierarchy identity conflicts with existing media.")
            self._sync_image(media.id, "poster", image_source_path)
            if genres:
                self._merge_genres(media.id, genres)
            return media
        historical = self._match_historical_container(
            kind=kind,
            title=title,
            parent_id=parent_id,
            season_number=season_number,
        )
        if historical is not None:
            self._bind_existing_container(
                historical,
                external_id=external_id,
                image_source_path=image_source_path,
                genres=genres,
                observed_at=observed_at,
            )
            return historical
        media = MediaItem(
            kind=kind,
            parent_id=parent_id,
            title=title,
            season_number=season_number,
        )
        self.work.media_items.add(media)
        self.work.session.flush()
        self.work.source_media_refs.add(
            SourceMediaRef(
                source_id=self.source_id,
                library_id=self.library_id,
                media_item_id=media.id,
                external_id=external_id,
                last_seen_at=observed_at,
                available=True,
            )
        )
        self._sync_image(media.id, "poster", image_source_path)
        self._merge_genres(media.id, genres)
        return media

    def _match_historical_container(
        self,
        *,
        kind: MediaKind,
        title: str,
        parent_id: int | None,
        season_number: int | None,
    ) -> MediaItem | None:
        statement = select(MediaItem).where(
            MediaItem.kind == kind,
            MediaItem.parent_id == parent_id,
        )
        if kind is MediaKind.SHOW:
            statement = statement.where(MediaItem.title == title)
        elif kind is MediaKind.SEASON:
            statement = statement.where(MediaItem.season_number == season_number)
        else:
            return None
        candidates = self.work.session.scalars(statement.limit(2)).all()
        if len(candidates) > 1:
            raise ValueError("Historical hierarchy has more than one matching container.")
        return candidates[0] if candidates else None

    def _required_media(self, media_item_id: int) -> MediaItem:
        media = self.work.media_items.get(media_item_id)
        if media is None:
            raise RuntimeError("External reference points to missing media.")
        return media

    @staticmethod
    def _apply_media(media: MediaItem, item: ExternalMediaItem, parent_id: int | None) -> None:
        media.kind = MediaKind(item.kind.value)
        media.parent_id = parent_id
        media.title = item.title
        media.original_title = item.original_title
        media.year = item.year
        media.season_number = item.season_number
        media.episode_number = item.episode_number
        media.disc_number = item.disc_number
        media.track_number = item.track_number
        media.duration_ms = item.duration_ms
        media.originally_available_on = item.originally_available_on
        media.summary = item.summary
        media.tagline = item.tagline
        media.studio = item.studio
        media.content_rating = item.content_rating
        media.audience_rating = item.audience_rating
        media.source_added_at = item.added_at

    def _sync_identifiers(self, media_item_id: int, item: ExternalMediaItem) -> None:
        for identifier in item.identifiers:
            if (
                self.work.media_identifiers.by_identity(
                    media_item_id, identifier.provider, identifier.external_id
                )
                is None
            ):
                self.work.media_identifiers.add(
                    MediaIdentifier(
                        media_item_id=media_item_id,
                        provider=identifier.provider,
                        external_id=identifier.external_id,
                    )
                )

    def _sync_image(self, media_item_id: int, image_type: str, source_path: str | None) -> None:
        if source_path is None:
            return
        image = self.work.media_images.by_item_and_type(media_item_id, image_type)
        provider = self.work.sources.get(self.source_id)
        provider_name = provider.connector_type.value if provider is not None else "plex"
        if image is None:
            self.work.media_images.add(
                MediaImage(
                    media_item_id=media_item_id,
                    source_id=self.source_id,
                    image_type=image_type,
                    provider=provider_name,
                    source_path=source_path,
                    cache_status="pending",
                )
            )
        elif image.source_path != source_path or image.provider != provider_name:
            image.source_id = self.source_id
            image.provider = provider_name
            image.source_path = source_path
            image.source_url = None
            image.local_filename = None
            image.mime_type = None
            image.cache_status = "pending"
            image.cached_at = None

    def _sync_genres(self, media_item_id: int, names: tuple[str, ...]) -> None:
        normalized = {" ".join(name.split()).casefold(): " ".join(name.split()) for name in names}
        existing_links = self.work.session.scalars(
            select(MediaGenre).where(MediaGenre.media_item_id == media_item_id)
        ).all()
        existing_genres = {
            genre.id: genre
            for genre in self.work.session.scalars(
                select(Genre).where(Genre.id.in_([link.genre_id for link in existing_links]))
            ).all()
        }
        existing_by_name = {
            existing_genres[link.genre_id].normalized_name: link
            for link in existing_links
            if link.genre_id in existing_genres
        }
        for normalized_name, display_name in normalized.items():
            if normalized_name in existing_by_name:
                continue
            genre = self.work.session.scalar(
                select(Genre).where(Genre.normalized_name == normalized_name)
            )
            if genre is None:
                genre = Genre(name=display_name, normalized_name=normalized_name)
                self.work.session.add(genre)
                self.work.session.flush()
            self.work.session.add(MediaGenre(media_item_id=media_item_id, genre_id=genre.id))
        for normalized_name, link in existing_by_name.items():
            if normalized_name not in normalized:
                self.work.session.delete(link)

    def _merge_genres(self, media_item_id: int, names: tuple[str, ...]) -> None:
        existing_names = tuple(
            self.work.session.scalars(
                select(Genre.name)
                .join(MediaGenre, MediaGenre.genre_id == Genre.id)
                .where(MediaGenre.media_item_id == media_item_id)
            ).all()
        )
        self._sync_genres(media_item_id, tuple({*existing_names, *names}))

    def _sync_watch_state(
        self, media_item_id: int, item: ExternalMediaItem, observed_at: datetime
    ) -> bool:
        if item.view_count is None and item.last_viewed_at is None and item.view_offset_ms is None:
            return False
        state = self.work.watch_states.by_item_and_source(media_item_id, self.source_id)
        incoming_count = item.view_count or 0
        regression = state is not None and incoming_count < state.view_count
        preserved_count = max(state.view_count if state else 0, incoming_count)
        if state is None:
            state = WatchState(
                media_item_id=media_item_id,
                source_id=self.source_id,
                view_count=preserved_count,
                completed=incoming_count > 0,
                observed_at=observed_at,
            )
            self.work.watch_states.add(state)
        state.view_count = preserved_count
        state.last_watched_at = item.last_viewed_at or state.last_watched_at
        state.completed = state.completed or incoming_count > 0
        state.progress_ms = item.view_offset_ms
        state.observed_at = observed_at
        return regression


def media_signature(item: ExternalMediaItem) -> str:
    """Hash normalized domain-relevant fields without retaining an external payload."""
    payload = asdict(item)
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _aggregate_completion(
    children: list[tuple[int, int, datetime | None]],
) -> tuple[int, datetime | None]:
    last_watched = _latest_datetime(*(value for _, _, value in children))
    if not children or any(view_count <= 0 for _, view_count, _ in children):
        return 0, last_watched
    return min(view_count for _, view_count, _ in children), last_watched


def _latest_datetime(*values: datetime | None) -> datetime | None:
    known = [value for value in values if value is not None]
    return max(known, default=None)


def event_dedup_key(source_id: int, event: ExternalWatchEvent) -> str:
    payload = {
        "source_id": source_id,
        "source_event_id": event.source_event_id,
        "media_external_id": event.media_external_id,
        "watched_at": event.watched_at.astimezone(UTC).isoformat(),
        "completed": event.completed,
        "progress_ms": event.progress_ms,
        "duration_ms": event.duration_ms,
        "view_number": event.view_number,
    }
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode()).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"Unsupported signature value: {type(value).__name__}")
