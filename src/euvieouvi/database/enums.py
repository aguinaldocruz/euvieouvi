"""Closed values persisted as readable strings."""

from enum import StrEnum


class ConnectorType(StrEnum):
    PLEX = "plex"


class LibraryMediaType(StrEnum):
    MOVIE = "movie"
    SHOW = "show"
    ARTIST = "artist"


class MediaKind(StrEnum):
    MOVIE = "movie"
    SHOW = "show"
    SEASON = "season"
    EPISODE = "episode"
    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"


class SyncTrigger(StrEnum):
    MANUAL = "manual"
    API = "api"
    SCHEDULED = "scheduled"


class SyncStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
