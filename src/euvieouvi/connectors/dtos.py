"""Neutral immutable DTOs returned by media connectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class ExternalLibraryType(StrEnum):
    MOVIE = "movie"
    SHOW = "show"
    ARTIST = "artist"


class ExternalMediaKind(StrEnum):
    MOVIE = "movie"
    SHOW = "show"
    SEASON = "season"
    EPISODE = "episode"
    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"


@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    server_name: str
    server_identifier: str
    authenticated: bool
    server_version: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _require_text(self.server_name, "server_name")
        _require_text(self.server_identifier, "server_identifier")


@dataclass(frozen=True, slots=True)
class ExternalLibrary:
    external_id: str
    name: str
    media_type: ExternalLibraryType
    available: bool = True
    source_updated_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(self.external_id, "external_id")
        _require_text(self.name, "name")
        _require_utc(self.source_updated_at, "source_updated_at")


@dataclass(frozen=True, slots=True)
class ExternalLibraryRef:
    external_id: str
    media_type: ExternalLibraryType

    def __post_init__(self) -> None:
        _require_text(self.external_id, "external_id")


@dataclass(frozen=True, slots=True)
class ExternalLibraryRejection:
    external_id: str
    name: str
    source_type: str
    reason: str

    def __post_init__(self) -> None:
        for name in ("external_id", "name", "source_type", "reason"):
            _require_text(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class ExternalIdentifier:
    provider: str
    external_id: str

    def __post_init__(self) -> None:
        _require_text(self.provider, "provider")
        _require_text(self.external_id, "external_id")


@dataclass(frozen=True, slots=True)
class ExternalMediaItem:
    external_id: str
    library_external_id: str
    kind: ExternalMediaKind
    title: str
    external_key: str | None = None
    original_title: str | None = None
    year: int | None = None
    show_external_id: str | None = None
    show_title: str | None = None
    season_external_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    artist_external_id: str | None = None
    artist_title: str | None = None
    album_external_id: str | None = None
    album_title: str | None = None
    disc_number: int | None = None
    track_number: int | None = None
    duration_ms: int | None = None
    originally_available_on: date | None = None
    summary: str | None = None
    tagline: str | None = None
    studio: str | None = None
    content_rating: str | None = None
    audience_rating: float | None = None
    genres: tuple[str, ...] = ()
    added_at: datetime | None = None
    thumb_path: str | None = None
    art_path: str | None = None
    artist_thumb_path: str | None = None
    album_thumb_path: str | None = None
    identifiers: tuple[ExternalIdentifier, ...] = ()
    updated_at: datetime | None = None
    last_viewed_at: datetime | None = None
    view_count: int | None = None
    view_offset_ms: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.external_id, "external_id")
        _require_text(self.library_external_id, "library_external_id")
        _require_text(self.title, "title")
        for name in (
            "year",
            "season_number",
            "episode_number",
            "disc_number",
            "track_number",
            "duration_ms",
            "view_count",
            "view_offset_ms",
        ):
            _require_nonnegative(getattr(self, name), name)
        _require_utc(self.updated_at, "updated_at")
        _require_utc(self.last_viewed_at, "last_viewed_at")
        _require_utc(self.added_at, "added_at")
        if self.kind is ExternalMediaKind.EPISODE:
            _require_text(self.show_external_id, "show_external_id")
            _require_text(self.show_title, "show_title")
            if self.season_number is None or self.episode_number is None:
                raise ValueError("episode requires season_number and episode_number")
        if self.kind is ExternalMediaKind.TRACK:
            _require_text(self.artist_external_id, "artist_external_id")
            _require_text(self.artist_title, "artist_title")
            _require_text(self.album_external_id, "album_external_id")
            _require_text(self.album_title, "album_title")


@dataclass(frozen=True, slots=True)
class ExternalWatchEvent:
    media_external_id: str
    library_external_id: str
    watched_at: datetime
    completed: bool
    source_event_id: str | None = None
    progress_ms: int | None = None
    duration_ms: int | None = None
    view_number: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.media_external_id, "media_external_id")
        _require_text(self.library_external_id, "library_external_id")
        _require_utc(self.watched_at, "watched_at")
        for name in ("progress_ms", "duration_ms", "view_number"):
            _require_nonnegative(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class HistoryCheckpoint:
    watermark_at: datetime | None = None
    last_external_id: str | None = None
    cursor: str | None = None

    def __post_init__(self) -> None:
        _require_utc(self.watermark_at, "watermark_at")


@dataclass(frozen=True, slots=True)
class PageRequest:
    start: int = 0
    size: int = 200
    cursor: str | None = None

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("start must be nonnegative")
        if not 1 <= self.size <= 1000:
            raise ValueError("size must be from 1 to 1000")


@dataclass(frozen=True, slots=True)
class Page[ItemT]:
    items: tuple[ItemT, ...]
    start: int
    size: int
    total_size: int | None = None
    next_start: int | None = None
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if self.start < 0 or self.size < 0:
            raise ValueError("page start and size must be nonnegative")
        if self.total_size is not None and self.total_size < 0:
            raise ValueError("total_size must be nonnegative")
        if self.next_start is not None and self.next_start <= self.start:
            raise ValueError("next_start must advance")

    @property
    def has_more(self) -> bool:
        if not self.items:
            return False
        return self.next_start is not None or self.next_cursor is not None


def _require_text(value: str | None, name: str) -> None:
    if value is None or not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_nonnegative(value: int | None, name: str) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{name} must be nonnegative")


def _require_utc(value: datetime | None, name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{name} must include a timezone")
