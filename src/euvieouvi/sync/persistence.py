"""Map neutral connector facts into the approved persistence model."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum

from euvieouvi.connectors.dtos import ExternalMediaItem, ExternalMediaKind, ExternalWatchEvent
from euvieouvi.database.enums import MediaKind
from euvieouvi.database.models import (
    MediaIdentifier,
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


class MediaPersistenceService:
    """Persist one item or event using repositories from an existing transaction."""

    def __init__(self, work: UnitOfWork, *, source_id: int, library_id: int) -> None:
        self.work = work
        self.source_id = source_id
        self.library_id = library_id

    def persist_media(self, item: ExternalMediaItem, observed_at: datetime) -> PersistResult:
        parent_id = self._ensure_hierarchy(item, observed_at)
        signature = media_signature(item)
        reference = self.work.source_media_refs.by_external_identity(
            self.source_id, item.external_id
        )
        if reference is None:
            media = MediaItem(
                kind=MediaKind(item.kind.value), title=item.title, parent_id=parent_id
            )
            self._apply_media(media, item, parent_id)
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
            classification = ItemClassification.INSERTED
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
            reference.raw_hash = signature

        self._sync_identifiers(media.id, item)
        regression = self._sync_watch_state(media.id, item, observed_at)
        return PersistResult(classification, media.id, regression)

    def persist_event(self, event: ExternalWatchEvent) -> bool:
        reference = self.work.source_media_refs.by_external_identity(
            self.source_id, event.media_external_id
        )
        if reference is None:
            raise ValueError("History event references unknown media.")
        dedup_key = event_dedup_key(self.source_id, event)
        if event.source_event_id is not None and self.work.watch_events.by_source_event_id(
            self.source_id, event.source_event_id
        ):
            return False
        if self.work.watch_events.by_dedup_key(self.source_id, dedup_key) is not None:
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

    def _ensure_hierarchy(self, item: ExternalMediaItem, observed_at: datetime) -> int | None:
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
                observed_at=observed_at,
            )
            album = self._ensure_container(
                external_id=item.album_external_id,
                kind=MediaKind.ALBUM,
                title=item.album_title,
                parent_id=artist.id,
                season_number=None,
                observed_at=observed_at,
            )
            return album.id
        if item.kind is not ExternalMediaKind.EPISODE:
            return None
        assert item.show_external_id is not None
        assert item.show_title is not None
        assert item.season_number is not None
        show = self._ensure_container(
            external_id=item.show_external_id,
            kind=MediaKind.SHOW,
            title=item.show_title,
            parent_id=None,
            season_number=None,
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
            observed_at=observed_at,
        )
        return season.id

    def _ensure_container(
        self,
        *,
        external_id: str,
        kind: MediaKind,
        title: str,
        parent_id: int | None,
        season_number: int | None,
        observed_at: datetime,
    ) -> MediaItem:
        reference = self.work.source_media_refs.by_external_identity(self.source_id, external_id)
        if reference is not None:
            reference.last_seen_at = observed_at
            reference.available = True
            media = self._required_media(reference.media_item_id)
            if media.kind is not kind or media.parent_id != parent_id:
                raise ValueError("External hierarchy identity conflicts with existing media.")
            return media
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
        return media

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
