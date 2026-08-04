"""Transactional source connection test and library discovery service."""

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from euvieouvi.connectors.base import MediaConnector
from euvieouvi.connectors.dtos import ExternalLibraryType
from euvieouvi.connectors.errors import ConnectorError
from euvieouvi.database.enums import LibraryMediaType
from euvieouvi.database.models import Library
from euvieouvi.database.unit_of_work import UnitOfWork
from euvieouvi.sync.errors import SyncSourceUnavailableError


class LibraryDiscoveryService:
    """Upsert a complete successful discovery without changing operator selection."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        connector: MediaConnector,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._connector = connector
        self._clock = clock

    def discover(self, source_id: int) -> int:
        self._ensure_source_enabled(source_id)
        try:
            self._connector.test_connection()
            discovered = self._connector.list_libraries()
        except ConnectorError:
            self._record_connection(source_id, "failed")
            raise
        session = self._session_factory()
        try:
            work = UnitOfWork(session)
            source = work.sources.get(source_id)
            if source is None or not source.enabled:
                raise SyncSourceUnavailableError("Source changed during discovery.")
            now = self._clock()
            seen: set[str] = set()
            for external in discovered:
                seen.add(external.external_id)
                library = work.libraries.by_external_identity(source_id, external.external_id)
                if library is None:
                    library = Library(
                        source_id=source_id,
                        external_id=external.external_id,
                        name=external.name,
                        media_type=_library_type(external.media_type),
                        enabled=False,
                        available=True,
                        discovered_at=now,
                        last_seen_at=now,
                    )
                    work.libraries.add(library)
                else:
                    library.name = external.name
                    library.media_type = _library_type(external.media_type)
                    library.available = True
                    library.last_seen_at = now
            for library in work.libraries.for_source(source_id):
                if library.external_id not in seen:
                    library.available = False
            source.last_connection_test_at = now
            source.last_connection_status = "succeeded"
            session.commit()
            return len(discovered)
        finally:
            session.close()

    def _ensure_source_enabled(self, source_id: int) -> None:
        session = self._session_factory()
        try:
            source = UnitOfWork(session).sources.get(source_id)
            if source is None or not source.enabled:
                raise SyncSourceUnavailableError("Source is missing or disabled.")
        finally:
            session.close()

    def _record_connection(self, source_id: int, status: str) -> None:
        session = self._session_factory()
        try:
            work = UnitOfWork(session)
            source = work.sources.get(source_id)
            if source is not None:
                source.last_connection_test_at = self._clock()
                source.last_connection_status = status
                session.commit()
        finally:
            session.close()


def _library_type(value: ExternalLibraryType) -> LibraryMediaType:
    return LibraryMediaType(value.value)
