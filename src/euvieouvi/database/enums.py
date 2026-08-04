"""Closed values persisted as readable strings."""

from enum import StrEnum


class ConnectorType(StrEnum):
    PLEX = "plex"


class LibraryMediaType(StrEnum):
    MOVIE = "movie"
    SHOW = "show"


class MediaKind(StrEnum):
    MOVIE = "movie"
    SHOW = "show"
    SEASON = "season"
    EPISODE = "episode"


class SyncTrigger(StrEnum):
    MANUAL = "manual"
    API = "api"


class SyncStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
