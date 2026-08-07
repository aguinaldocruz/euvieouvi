"""Synchronous, resumable synchronization orchestrator."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from euvieouvi.connectors.base import MediaConnector
from euvieouvi.connectors.dtos import (
    ExternalLibraryRef,
    ExternalLibraryType,
    ExternalMediaItem,
    ExternalMediaKind,
    ExternalWatchEvent,
    HistoryCheckpoint,
    PageRequest,
)
from euvieouvi.connectors.errors import ConnectorError
from euvieouvi.database.enums import LibraryMediaType, SyncStatus, SyncTrigger
from euvieouvi.database.models import Library, SyncCheckpoint, SyncError, SyncRun, SyncRunLibrary
from euvieouvi.database.unit_of_work import UnitOfWork
from euvieouvi.sync.cancellation import CancellationToken
from euvieouvi.sync.errors import (
    SyncAlreadyRunningError,
    SyncCancelledError,
    SyncSourceUnavailableError,
)
from euvieouvi.sync.persistence import ItemClassification, MediaPersistenceService

SessionFactory = Callable[[], Session]


@dataclass(frozen=True, slots=True)
class SyncRunResult:
    run_id: int
    status: SyncStatus


class _PagePersistenceError(Exception):
    pass


class SyncOrchestrator:
    """Coordinate one source while preserving page-level transaction boundaries."""

    def __init__(
        self,
        session_factory: SessionFactory,
        connector: MediaConnector,
        *,
        page_size: int = 200,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_acquired: Callable[[int], None] | None = None,
    ) -> None:
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be from 1 to 1000")
        self._session_factory = session_factory
        self._connector = connector
        self._page_size = page_size
        self._clock = clock
        self._on_acquired = on_acquired

    def run(
        self,
        source_id: int,
        *,
        trigger: SyncTrigger = SyncTrigger.MANUAL,
        cancellation: CancellationToken | None = None,
    ) -> SyncRunResult:
        token = cancellation or CancellationToken()
        run_id, library_ids = self._acquire(source_id, trigger, queued=False)
        if self._on_acquired is not None:
            self._on_acquired(run_id)
        return self._execute(run_id, source_id, library_ids, token)

    def enqueue(self, source_id: int, *, trigger: SyncTrigger = SyncTrigger.API) -> int:
        """Persist a queued run and its immutable library snapshot."""
        run_id, _ = self._acquire(source_id, trigger, queued=True)
        return run_id

    def run_queued(
        self,
        run_id: int,
        *,
        cancellation: CancellationToken | None = None,
        finalize_on_success: bool = True,
    ) -> SyncRunResult:
        """Claim and execute a previously queued run."""
        session = self._session_factory()
        try:
            work = UnitOfWork(session)
            run = work.sync_runs.get(run_id)
            if run is None or run.status is not SyncStatus.QUEUED:
                raise SyncSourceUnavailableError("Queued synchronization is missing or invalid.")
            now = self._clock()
            run.status = SyncStatus.RUNNING
            run.started_at = now
            run.heartbeat_at = now
            libraries = tuple(item.library_id for item in work.sync_run_libraries.for_run(run_id))
            source_id = run.source_id
            session.commit()
        finally:
            session.close()
        if self._on_acquired is not None:
            self._on_acquired(run_id)
        return self._execute(
            run_id,
            source_id,
            libraries,
            cancellation or CancellationToken(),
            finalize_on_success=finalize_on_success,
        )

    def _execute(
        self,
        run_id: int,
        source_id: int,
        library_ids: tuple[int, ...],
        token: CancellationToken,
        *,
        finalize_on_success: bool = True,
    ) -> SyncRunResult:
        try:
            for library_id in library_ids:
                token.raise_if_cancelled()
                self._sync_library(run_id, source_id, library_id, token)
        except SyncCancelledError:
            self._finish_run(run_id, SyncStatus.INTERRUPTED, "Synchronization was cancelled.")
            return SyncRunResult(run_id, SyncStatus.INTERRUPTED)
        except (ConnectorError, _PagePersistenceError) as error:
            self._record_run_error(run_id, error)
            self._finish_run(run_id, SyncStatus.FAILED, "Synchronization failed safely.")
            return SyncRunResult(run_id, SyncStatus.FAILED)
        except Exception as error:
            self._record_run_error(run_id, error)
            self._finish_run(run_id, SyncStatus.FAILED, "Synchronization failed safely.")
            raise
        if finalize_on_success:
            self._finish_run(run_id, SyncStatus.SUCCEEDED, "Synchronization completed.")
        return SyncRunResult(run_id, SyncStatus.SUCCEEDED)

    def update_progress(self, run_id: int, summary: str) -> None:
        self._update_progress(run_id, summary)

    def finish_success(self, run_id: int, summary: str) -> None:
        self._finish_run(run_id, SyncStatus.SUCCEEDED, summary)

    def finish_failure(self, run_id: int, error: Exception, summary: str) -> None:
        self._record_run_error(run_id, error)
        self._finish_run(run_id, SyncStatus.FAILED, summary)

    def _acquire(
        self, source_id: int, trigger: SyncTrigger, *, queued: bool
    ) -> tuple[int, tuple[int, ...]]:
        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            work = UnitOfWork(session)
            if work.sync_runs.running():
                session.rollback()
                raise SyncAlreadyRunningError("A synchronization is already running.")
            source = work.sources.get(source_id)
            if source is None or not source.enabled:
                session.rollback()
                raise SyncSourceUnavailableError("Source is missing or disabled.")
            now = self._clock()
            run = SyncRun(
                source_id=source_id,
                trigger=trigger,
                status=SyncStatus.QUEUED if queued else SyncStatus.RUNNING,
                started_at=None if queued else now,
                heartbeat_at=None if queued else now,
                summary=(
                    "Sincronização aguardando início." if queued else "Preparando sincronização."
                ),
            )
            work.sync_runs.add(run)
            session.flush()
            libraries = tuple(work.libraries.enabled_available_for_source(source_id))
            for library in libraries:
                work.sync_run_libraries.add(
                    SyncRunLibrary(
                        sync_run_id=run.id,
                        library_id=library.id,
                        status=SyncStatus.QUEUED,
                    )
                )
            session.commit()
            return run.id, tuple(library.id for library in libraries)
        finally:
            session.close()

    def _sync_library(
        self,
        run_id: int,
        source_id: int,
        library_id: int,
        token: CancellationToken,
    ) -> None:
        library = self._start_library(run_id, library_id)
        checkpoint = self._load_checkpoint(library_id)
        stage, start = _resume_position(checkpoint)
        full_catalog_scan = stage == "media" and start == 0
        seen_external_ids: set[str] = set()
        reference = ExternalLibraryRef(
            library.external_id,
            ExternalLibraryType(library.media_type.value),
        )
        media_kind = (
            ExternalMediaKind.MOVIE
            if library.media_type is LibraryMediaType.MOVIE
            else ExternalMediaKind.EPISODE
            if library.media_type is LibraryMediaType.SHOW
            else ExternalMediaKind.TRACK
        )
        if stage == "media":
            while True:
                token.raise_if_cancelled()
                self._update_progress(
                    run_id,
                    f"Coletando catálogo de {library.name} · posição {start}.",
                )
                media_page = self._connector.get_media_page(
                    reference,
                    media_kind,
                    PageRequest(start=start, size=self._page_size),
                )
                for item in media_page.items:
                    seen_external_ids.update(_external_ids_for_item(item))
                next_stage = "media" if media_page.has_more else "history"
                next_start = media_page.next_start or 0
                self._persist_media_page(
                    run_id,
                    source_id,
                    library_id,
                    media_page.items,
                    next_stage=next_stage,
                    next_start=next_start,
                )
                if not media_page.has_more:
                    break
                start = next_start
            if full_catalog_scan:
                self._mark_missing_unavailable(library_id, seen_external_ids)
            stage, start = "history", 0

        while stage == "history":
            token.raise_if_cancelled()
            self._update_progress(
                run_id,
                f"Coletando histórico de {library.name} · posição {start}.",
            )
            history_page = self._connector.get_history_page(
                reference,
                HistoryCheckpoint(
                    watermark_at=checkpoint.watermark_at if checkpoint else None,
                    last_external_id=checkpoint.last_external_id if checkpoint else None,
                ),
                PageRequest(start=start, size=self._page_size),
            )
            next_start = history_page.next_start or 0
            self._persist_history_page(
                run_id,
                source_id,
                library_id,
                history_page.items,
                next_start=next_start,
                complete=not history_page.has_more,
            )
            if not history_page.has_more:
                break
            start = next_start
        self._update_progress(run_id, f"Reconciliando estados assistidos de {library.name}.")
        self._rebuild_container_watch_states(source_id, library_id)
        self._finish_library(run_id, library_id, SyncStatus.SUCCEEDED, None)

    def _persist_media_page(
        self,
        run_id: int,
        source_id: int,
        library_id: int,
        items: tuple[ExternalMediaItem, ...],
        *,
        next_stage: str,
        next_start: int,
    ) -> None:
        session = self._session_factory()
        failures = 0
        try:
            work = UnitOfWork(session)
            run, detail = self._required_execution(work, run_id, library_id)
            service = MediaPersistenceService(work, source_id=source_id, library_id=library_id)
            for item in items:
                run.items_read += 1
                detail.items_read += 1
                try:
                    with session.begin_nested():
                        result = service.persist_media(item, self._clock())
                        session.flush()
                except Exception:
                    failures += 1
                    run.items_failed += 1
                    detail.items_failed += 1
                    self._add_item_error(work, run_id, library_id, item.external_id)
                    continue
                if result.classification is ItemClassification.INSERTED:
                    run.items_inserted += 1
                    detail.items_inserted += 1
                elif result.classification is ItemClassification.UPDATED:
                    run.items_updated += 1
                    detail.items_updated += 1
                else:
                    run.items_unchanged += 1
                if result.event_inserted:
                    run.events_inserted += 1
                if result.view_count_regression:
                    self._add_reconciliation_warning(work, run_id, library_id, item.external_id)
            run.heartbeat_at = self._clock()
            if failures == 0:
                self._set_checkpoint(work, library_id, run_id, next_stage, next_start)
            session.commit()
        finally:
            session.close()
        if failures:
            raise _PagePersistenceError("A media page contained invalid items.")

    def _persist_history_page(
        self,
        run_id: int,
        source_id: int,
        library_id: int,
        items: tuple[ExternalWatchEvent, ...],
        *,
        next_start: int,
        complete: bool,
    ) -> None:
        session = self._session_factory()
        failures = 0
        try:
            work = UnitOfWork(session)
            run, _ = self._required_execution(work, run_id, library_id)
            service = MediaPersistenceService(work, source_id=source_id, library_id=library_id)
            for event in items:
                try:
                    with session.begin_nested():
                        inserted = service.persist_event(event)
                        session.flush()
                except Exception:
                    failures += 1
                    run.items_failed += 1
                    self._add_item_error(work, run_id, library_id, None)
                    continue
                if inserted:
                    run.events_inserted += 1
            run.heartbeat_at = self._clock()
            if failures == 0:
                self._set_checkpoint(
                    work,
                    library_id,
                    run_id,
                    "complete" if complete else "history",
                    next_start,
                )
            session.commit()
        finally:
            session.close()
        if failures:
            raise _PagePersistenceError("A history page contained invalid events.")

    def _set_checkpoint(
        self,
        work: UnitOfWork,
        library_id: int,
        run_id: int,
        stage: str,
        start: int,
    ) -> None:
        checkpoint = work.sync_checkpoints.by_library(library_id)
        if checkpoint is None:
            checkpoint = SyncCheckpoint(library_id=library_id, strategy="full_scan_v1")
            work.sync_checkpoints.add(checkpoint)
        checkpoint.cursor = (
            None if stage == "complete" else json.dumps({"stage": stage, "start": start})
        )
        checkpoint.last_successful_run_id = (
            run_id if stage == "complete" else checkpoint.last_successful_run_id
        )
        checkpoint.updated_at = self._clock()

    def _mark_missing_unavailable(self, library_id: int, seen_external_ids: set[str]) -> None:
        session = self._session_factory()
        try:
            work = UnitOfWork(session)
            for reference in work.source_media_refs.for_library(library_id):
                if reference.external_id not in seen_external_ids:
                    if reference.available:
                        reference.unavailable_since = self._clock()
                    reference.available = False
            session.commit()
        finally:
            session.close()

    def _rebuild_container_watch_states(self, source_id: int, library_id: int) -> None:
        session = self._session_factory()
        try:
            work = UnitOfWork(session)
            MediaPersistenceService(
                work,
                source_id=source_id,
                library_id=library_id,
            ).rebuild_container_watch_states(self._clock())
            session.commit()
        finally:
            session.close()

    def _start_library(self, run_id: int, library_id: int) -> Library:
        session = self._session_factory()
        try:
            work = UnitOfWork(session)
            library = work.libraries.get(library_id)
            detail = work.sync_run_libraries.by_run_and_library(run_id, library_id)
            if library is None or detail is None:
                raise RuntimeError("Synchronization snapshot is inconsistent.")
            detail.status = SyncStatus.RUNNING
            detail.started_at = self._clock()
            session.commit()
            session.refresh(library)
            session.expunge(library)
            return library
        finally:
            session.close()

    def _load_checkpoint(self, library_id: int) -> SyncCheckpoint | None:
        session = self._session_factory()
        try:
            checkpoint = UnitOfWork(session).sync_checkpoints.by_library(library_id)
            if checkpoint is not None:
                session.expunge(checkpoint)
            return checkpoint
        finally:
            session.close()

    def _finish_library(
        self, run_id: int, library_id: int, status: SyncStatus, message: str | None
    ) -> None:
        session = self._session_factory()
        try:
            work = UnitOfWork(session)
            detail = work.sync_run_libraries.by_run_and_library(run_id, library_id)
            if detail is not None:
                detail.status = status
                detail.finished_at = self._clock()
                detail.message = message
                session.commit()
        finally:
            session.close()

    def _finish_run(self, run_id: int, status: SyncStatus, summary: str) -> None:
        session = self._session_factory()
        try:
            work = UnitOfWork(session)
            run = work.sync_runs.get(run_id)
            if run is None:
                raise RuntimeError("Synchronization run disappeared.")
            run.status = status
            run.finished_at = self._clock()
            run.heartbeat_at = self._clock()
            run.summary = summary
            for detail in work.sync_run_libraries.for_run(run_id):
                if detail.status in {
                    SyncStatus.QUEUED,
                    SyncStatus.RUNNING,
                }:
                    detail.status = status
                    detail.finished_at = self._clock()
                    detail.message = summary
            session.commit()
        finally:
            session.close()

    def _update_progress(self, run_id: int, summary: str) -> None:
        session = self._session_factory()
        try:
            work = UnitOfWork(session)
            run = work.sync_runs.get(run_id)
            if run is None:
                raise RuntimeError("Synchronization run disappeared.")
            run.summary = summary
            run.heartbeat_at = self._clock()
            session.commit()
        finally:
            session.close()

    def _record_run_error(self, run_id: int, error: Exception) -> None:
        session = self._session_factory()
        try:
            work = UnitOfWork(session)
            work.sync_errors.add(
                SyncError(
                    sync_run_id=run_id,
                    category=type(error).__name__,
                    message="Synchronization dependency failed.",
                    retryable=isinstance(error, ConnectorError),
                )
            )
            session.commit()
        finally:
            session.close()

    @staticmethod
    def _required_execution(
        work: UnitOfWork, run_id: int, library_id: int
    ) -> tuple[SyncRun, SyncRunLibrary]:
        run = work.sync_runs.get(run_id)
        detail = work.sync_run_libraries.by_run_and_library(run_id, library_id)
        if run is None or detail is None:
            raise RuntimeError("Synchronization execution data is missing.")
        return run, detail

    @staticmethod
    def _add_item_error(
        work: UnitOfWork,
        run_id: int,
        library_id: int,
        external_id: str | None,
    ) -> None:
        work.sync_errors.add(
            SyncError(
                sync_run_id=run_id,
                library_id=library_id,
                media_external_id=external_id,
                category="item_persistence",
                message="Item could not be persisted.",
                retryable=False,
            )
        )

    @staticmethod
    def _add_reconciliation_warning(
        work: UnitOfWork, run_id: int, library_id: int, external_id: str
    ) -> None:
        work.sync_errors.add(
            SyncError(
                sync_run_id=run_id,
                library_id=library_id,
                media_external_id=external_id,
                category="view_count_regression",
                message="A lower external view count was preserved for reconciliation.",
                retryable=False,
            )
        )


def _resume_position(checkpoint: SyncCheckpoint | None) -> tuple[str, int]:
    if checkpoint is None or checkpoint.strategy != "full_scan_v1" or checkpoint.cursor is None:
        return "media", 0
    try:
        payload = json.loads(checkpoint.cursor)
        stage = str(payload["stage"])
        start = int(payload["start"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return "media", 0
    if stage not in {"media", "history"} or start < 0:
        return "media", 0
    return stage, start


def _external_ids_for_item(item: ExternalMediaItem) -> set[str]:
    result = {item.external_id}
    if item.show_external_id is not None:
        result.add(item.show_external_id)
    if item.kind is ExternalMediaKind.EPISODE and item.show_external_id is not None:
        assert item.season_number is not None
        result.add(
            item.season_external_id or f"{item.show_external_id}:season:{item.season_number}"
        )
    if item.kind is ExternalMediaKind.TRACK:
        if item.artist_external_id is not None:
            result.add(item.artist_external_id)
        if item.album_external_id is not None:
            result.add(item.album_external_id)
    return result
