"""Database-independent media connector protocol."""

from datetime import datetime
from typing import Protocol

from euvieouvi.connectors.dtos import (
    ConnectionInfo,
    ExternalLibrary,
    ExternalLibraryRef,
    ExternalMediaItem,
    ExternalMediaKind,
    ExternalWatchEvent,
    HistoryCheckpoint,
    Page,
    PageRequest,
)


class DiscoveryConnector(Protocol):
    def test_connection(self) -> ConnectionInfo: ...

    def list_libraries(self) -> list[ExternalLibrary]: ...


class SyncConnector(DiscoveryConnector, Protocol):
    def get_media_page(
        self,
        library: ExternalLibraryRef,
        media_kind: ExternalMediaKind,
        page: PageRequest,
    ) -> Page[ExternalMediaItem]: ...

    def get_history_page(
        self,
        library: ExternalLibraryRef,
        checkpoint: HistoryCheckpoint | None,
        page: PageRequest,
    ) -> Page[ExternalWatchEvent]: ...

    def fetch_image(self, source_path: str, *, width: int, height: int) -> tuple[bytes, str]: ...


class WatchConnector(Protocol):
    def mark_watched(self, external_id: str, *, watched_at: datetime | None = None) -> None: ...

    def set_progress(self, external_id: str, progress_ms: int) -> None: ...


class MediaConnector(SyncConnector, WatchConnector, Protocol):
    """Complete connector contract implemented by production media servers."""
