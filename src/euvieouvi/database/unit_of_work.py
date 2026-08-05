"""Transaction boundary shared by services and repositories."""

from __future__ import annotations

from sqlalchemy.orm import Session

from euvieouvi.database.repositories import (
    LibraryRepository,
    MediaIdentifierRepository,
    MediaImageRepository,
    MediaItemRepository,
    SettingRepository,
    SourceMediaRefRepository,
    SourceRepository,
    SyncCheckpointRepository,
    SyncErrorRepository,
    SyncRunLibraryRepository,
    SyncRunRepository,
    WatchEventRepository,
    WatchStateRepository,
)


class UnitOfWork:
    """Own one explicit database transaction without hidden commits."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.sources = SourceRepository(session)
        self.libraries = LibraryRepository(session)
        self.media_items = MediaItemRepository(session)
        self.source_media_refs = SourceMediaRefRepository(session)
        self.media_identifiers = MediaIdentifierRepository(session)
        self.media_images = MediaImageRepository(session)
        self.watch_events = WatchEventRepository(session)
        self.watch_states = WatchStateRepository(session)
        self.sync_runs = SyncRunRepository(session)
        self.sync_run_libraries = SyncRunLibraryRepository(session)
        self.sync_checkpoints = SyncCheckpointRepository(session)
        self.sync_errors = SyncErrorRepository(session)
        self.settings = SettingRepository(session)
        self._completed = False

    def __enter__(self) -> UnitOfWork:
        return self

    def commit(self) -> None:
        self.session.commit()
        self._completed = True

    def rollback(self) -> None:
        self.session.rollback()
        self._completed = True

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is not None or not self._completed:
            self.session.rollback()
