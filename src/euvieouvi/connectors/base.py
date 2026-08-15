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


class MediaConnector(Protocol):
    def test_connection(self) -> ConnectionInfo: ...

    def list_libraries(self) -> list[ExternalLibrary]: ...

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

    def mark_watched(self, external_id: str, *, watched_at: datetime | None = None) -> None: ...
