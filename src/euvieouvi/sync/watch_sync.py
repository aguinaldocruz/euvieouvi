"""Cross-source propagation of completed watch state after catalog synchronization."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from euvieouvi.connectors.base import MediaConnector
from euvieouvi.database.enums import ConnectorType, MediaKind, SyncStatus
from euvieouvi.database.models import (
    MediaItem,
    Setting,
    Source,
    SourceMediaRef,
    SyncRun,
    WatchState,
)

ConnectorFactory = Callable[[Source], MediaConnector]
SessionFactory = Callable[[], Session]
_SYNCABLE_KINDS = {MediaKind.MOVIE, MediaKind.EPISODE, MediaKind.TRACK}


@dataclass(frozen=True, slots=True)
class WatchSyncCandidate:
    media_item_id: int
    target_source_id: int
    target_external_id: str


@dataclass(frozen=True, slots=True)
class WatchSyncResult:
    scanned: int
    updated: int
    skipped: int
    failed: int


class WatchSyncService:
    """Propagate watched state without ever propagating an unwatched state."""

    def __init__(
        self,
        session_factory: SessionFactory,
        connector_factory: ConnectorFactory,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._connector_factory = connector_factory
        self._clock = clock

    def run(self, run_id: int | None) -> WatchSyncResult:
        candidates, scanned, skipped = self._prepare(run_id)
        connectors: dict[int, MediaConnector] = {}
        updated = 0
        failed = 0
        try:
            for candidate in candidates:
                try:
                    connector = connectors.get(candidate.target_source_id)
                    if connector is None:
                        connector = self._connector(candidate.target_source_id)
                        connectors[candidate.target_source_id] = connector
                    connector.mark_watched(candidate.target_external_id)
                    self._record_success(run_id, candidate)
                    updated += 1
                except Exception:
                    failed += 1
                    self._update_progress(run_id, scanned, updated, skipped, failed)
            result = WatchSyncResult(scanned, updated, skipped, failed)
            self._finish(run_id, result)
            return result
        finally:
            for connector in connectors.values():
                close = getattr(connector, "close", None)
                if callable(close):
                    close()

    def _prepare(self, run_id: int | None) -> tuple[tuple[WatchSyncCandidate, ...], int, int]:
        session = self._session_factory()
        try:
            run = session.get(SyncRun, run_id) if run_id is not None else None
            if run_id is not None and run is None:
                raise LookupError("Synchronization run not found")
            sources = session.scalars(
                select(Source).where(
                    Source.enabled.is_(True),
                    Source.connector_type.in_([ConnectorType.PLEX, ConnectorType.JELLYFIN]),
                )
            ).all()
            by_type = {source.connector_type: source for source in sources}
            if ConnectorType.PLEX not in by_type or ConnectorType.JELLYFIN not in by_type:
                if run is not None:
                    return self._start_without_candidates(run, session)
                return (), 0, 0
            source_ids = {source.id for source in by_type.values()}
            refs = session.scalars(
                select(SourceMediaRef).where(
                    SourceMediaRef.source_id.in_(source_ids),
                    SourceMediaRef.available.is_(True),
                )
            ).all()
            item_ids = {ref.media_item_id for ref in refs}
            kinds = {
                item.id: item.kind
                for item in session.scalars(select(MediaItem).where(MediaItem.id.in_(item_ids)))
            }
            states = session.scalars(
                select(WatchState).where(
                    WatchState.source_id.in_(source_ids), WatchState.completed.is_(True)
                )
            ).all()
            watched = {(state.media_item_id, state.source_id) for state in states}
            refs_by_item: dict[int, dict[int, SourceMediaRef]] = {}
            for ref in refs:
                refs_by_item.setdefault(ref.media_item_id, {}).setdefault(ref.source_id, ref)
            candidates: list[WatchSyncCandidate] = []
            scanned = 0
            skipped = 0
            for media_item_id, item_refs in refs_by_item.items():
                if (
                    kinds.get(media_item_id) not in _SYNCABLE_KINDS
                    or not source_ids <= item_refs.keys()
                ):
                    continue
                scanned += 1
                completed_sources = {
                    source_id for source_id in source_ids if (media_item_id, source_id) in watched
                }
                if not completed_sources or completed_sources == source_ids:
                    skipped += 1
                    continue
                target_source_id = next(iter(source_ids - completed_sources))
                candidates.append(
                    WatchSyncCandidate(
                        media_item_id,
                        target_source_id,
                        item_refs[target_source_id].external_id,
                    )
                )
            now = self._clock()
            if run is not None:
                run.watch_sync_status = SyncStatus.RUNNING
                run.watch_sync_started_at = now
                run.watch_sync_finished_at = None
                run.watch_sync_scanned = scanned
                run.watch_sync_updated = 0
                run.watch_sync_skipped = skipped
                run.watch_sync_failed = 0
                run.watch_sync_summary = "Propagando conclusões entre Plex e Jellyfin."
            session.commit()
            return tuple(candidates), scanned, skipped
        finally:
            session.close()

    def _start_without_candidates(
        self, run: SyncRun, session: Session
    ) -> tuple[tuple[WatchSyncCandidate, ...], int, int]:
        now = self._clock()
        run.watch_sync_status = SyncStatus.SUCCEEDED
        run.watch_sync_started_at = now
        run.watch_sync_finished_at = now
        run.watch_sync_summary = "Propagação ignorada: Plex e Jellyfin ativos são necessários."
        session.commit()
        return (), 0, 0

    def _connector(self, source_id: int) -> MediaConnector:
        session = self._session_factory()
        try:
            source = session.get(Source, source_id)
            if source is None:
                raise LookupError("Watch synchronization target disappeared")
            session.expunge(source)
            return self._connector_factory(source)
        finally:
            session.close()

    def _record_success(self, run_id: int | None, candidate: WatchSyncCandidate) -> None:
        session = self._session_factory()
        try:
            now = self._clock()
            state = session.scalar(
                select(WatchState).where(
                    WatchState.media_item_id == candidate.media_item_id,
                    WatchState.source_id == candidate.target_source_id,
                )
            )
            if state is None:
                state = WatchState(
                    media_item_id=candidate.media_item_id,
                    source_id=candidate.target_source_id,
                    view_count=1,
                    completed=True,
                    last_watched_at=now,
                    observed_at=now,
                )
                session.add(state)
            else:
                state.completed = True
                state.last_watched_at = now
                state.view_count = max(1, state.view_count)
                state.progress_ms = None
                state.observed_at = now
            if run_id is not None:
                run = session.get(SyncRun, run_id)
                if run is not None:
                    run.watch_sync_updated += 1
            session.commit()
        finally:
            session.close()

    def _update_progress(
        self, run_id: int | None, scanned: int, updated: int, skipped: int, failed: int
    ) -> None:
        if run_id is None:
            return
        session = self._session_factory()
        try:
            run = session.get(SyncRun, run_id)
            if run is not None:
                run.watch_sync_scanned = scanned
                run.watch_sync_updated = updated
                run.watch_sync_skipped = skipped
                run.watch_sync_failed = failed
                session.commit()
        finally:
            session.close()

    def _finish(self, run_id: int | None, result: WatchSyncResult) -> None:
        session = self._session_factory()
        try:
            if run_id is not None:
                run = session.get(SyncRun, run_id)
                if run is None:
                    raise LookupError("Synchronization run not found")
                run.watch_sync_status = (
                    SyncStatus.SUCCEEDED if result.failed == 0 else SyncStatus.FAILED
                )
                run.watch_sync_finished_at = self._clock()
                run.watch_sync_scanned = result.scanned
                run.watch_sync_updated = result.updated
                run.watch_sync_skipped = result.skipped
                run.watch_sync_failed = result.failed
                run.watch_sync_summary = (
                    f"Propagação concluída: {result.updated} atualizados, "
                    f"{result.skipped} já alinhados e {result.failed} falhas."
                )
            if result.failed == 0:
                pending = session.get(Setting, "watch_sync.pending")
                if pending is not None:
                    pending.value = "false"
            session.commit()
        finally:
            session.close()
