"""Cross-source propagation of completed watch state after catalog synchronization."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from euvieouvi.connectors.base import MediaConnector
from euvieouvi.database.enums import ConnectorType, MediaKind, SyncStatus
from euvieouvi.database.models import (
    MediaIdentifier,
    MediaItem,
    Setting,
    Source,
    SourceMediaRef,
    SyncRun,
    WatchEvent,
    WatchState,
)

ConnectorFactory = Callable[[Source], MediaConnector]
SessionFactory = Callable[[], Session]
ProgressCallback = Callable[[int, int, int, int], None]
_SYNCABLE_KINDS = {MediaKind.MOVIE, MediaKind.EPISODE, MediaKind.TRACK}
_PERSIST_BATCH_SIZE = 100


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


def _matching_item_groups(
    session: Session,
    item_ids: set[int],
    kinds: dict[int, MediaKind],
) -> tuple[frozenset[int], ...]:
    """Group same-kind catalog rows using stable cross-provider identifiers."""
    parents = {media_item_id: media_item_id for media_item_id in item_ids}

    def find(media_item_id: int) -> int:
        while parents[media_item_id] != media_item_id:
            parents[media_item_id] = parents[parents[media_item_id]]
            media_item_id = parents[media_item_id]
        return media_item_id

    identifiers: dict[tuple[MediaKind, str, str], int] = {}
    rows = session.execute(
        select(
            MediaIdentifier.media_item_id,
            MediaIdentifier.provider,
            MediaIdentifier.external_id,
        ).where(
            MediaIdentifier.media_item_id.in_(item_ids),
            MediaIdentifier.provider.in_(("tmdb", "tvdb", "imdb")),
        )
    )
    for media_item_id, provider, external_id in rows:
        kind = kinds.get(media_item_id)
        if kind not in _SYNCABLE_KINDS:
            continue
        key = (kind, provider, external_id)
        existing = identifiers.setdefault(key, media_item_id)
        left = find(media_item_id)
        right = find(existing)
        if left != right:
            parents[right] = left

    groups: dict[int, set[int]] = {}
    for media_item_id in item_ids:
        if kinds.get(media_item_id) in _SYNCABLE_KINDS:
            groups.setdefault(find(media_item_id), set()).add(media_item_id)
    return tuple(frozenset(group) for group in groups.values())


class WatchSyncService:
    """Propagate watched state without ever propagating an unwatched state."""

    def __init__(
        self,
        session_factory: SessionFactory,
        connector_factory: ConnectorFactory,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        progress: ProgressCallback | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._connector_factory = connector_factory
        self._clock = clock
        self._progress = progress

    def run(
        self,
        run_id: int | None,
        *,
        source_type: ConnectorType | None = None,
    ) -> WatchSyncResult:
        """Propagate completions, optionally in one explicit source direction."""
        candidates, scanned, skipped = self._prepare(run_id, source_type=source_type)
        connectors: dict[int, MediaConnector] = {}
        pending_successes: list[WatchSyncCandidate] = []
        updated = 0
        failed = 0
        self._update_progress(run_id, scanned, updated, skipped, failed)
        try:
            for candidate in candidates:
                try:
                    connector = connectors.get(candidate.target_source_id)
                    if connector is None:
                        connector = self._connector(candidate.target_source_id)
                        connectors[candidate.target_source_id] = connector
                    connector.mark_watched(candidate.target_external_id)
                except Exception:
                    failed += 1
                else:
                    pending_successes.append(candidate)
                    updated += 1
                    if len(pending_successes) >= _PERSIST_BATCH_SIZE:
                        self._record_successes(run_id, pending_successes)
                        pending_successes.clear()
                self._update_progress(run_id, scanned, updated, skipped, failed)
            if pending_successes:
                self._record_successes(run_id, pending_successes)
            result = WatchSyncResult(scanned, updated, skipped, failed)
            self._finish(run_id, result, clear_pending=source_type is None)
            return result
        finally:
            for connector in connectors.values():
                close = getattr(connector, "close", None)
                if callable(close):
                    close()

    def _prepare(
        self,
        run_id: int | None,
        *,
        source_type: ConnectorType | None = None,
    ) -> tuple[tuple[WatchSyncCandidate, ...], int, int]:
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
            configured_users: dict[int, str] = {}
            plex_user = session.get(Setting, "plex.user_id") or session.get(
                Setting, "webhook.plex.user_filter"
            )
            if plex_user is not None and plex_user.value.strip():
                configured_users[by_type[ConnectorType.PLEX].id] = plex_user.value.strip()
            try:
                secret = json.loads(by_type[ConnectorType.JELLYFIN].secret)
                jellyfin_user = str(secret.get("user_id") or "").strip()
            except (TypeError, ValueError, json.JSONDecodeError):
                jellyfin_user = ""
            if jellyfin_user:
                configured_users[by_type[ConnectorType.JELLYFIN].id] = jellyfin_user
            if configured_users.keys() != source_ids:
                if run is not None:
                    return self._start_without_candidates(run, session)
                return (), 0, 0
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
            watched: set[tuple[int, int]] = set()
            watched.update(
                session.execute(
                    select(WatchState.media_item_id, WatchState.source_id).where(
                        WatchState.source_id.in_(source_ids),
                        WatchState.media_item_id.in_(item_ids),
                        WatchState.completed.is_(True),
                    )
                ).all()
            )
            for source_id, configured_user in configured_users.items():
                watched.update(
                    session.execute(
                        select(WatchEvent.media_item_id, WatchEvent.source_id).where(
                            WatchEvent.source_id == source_id,
                            WatchEvent.media_item_id.in_(item_ids),
                            WatchEvent.completed.is_(True),
                            WatchEvent.playback_user == configured_user,
                        )
                    ).all()
                )
            refs_by_item: dict[int, dict[int, SourceMediaRef]] = {}
            for ref in refs:
                refs_by_item.setdefault(ref.media_item_id, {}).setdefault(ref.source_id, ref)
            groups = _matching_item_groups(session, item_ids, kinds)
            candidates: list[WatchSyncCandidate] = []
            scanned = 0
            skipped = 0
            for group in groups:
                grouped_refs: dict[int, list[SourceMediaRef]] = {}
                for media_item_id in group:
                    for source_id, ref in refs_by_item.get(media_item_id, {}).items():
                        grouped_refs.setdefault(source_id, []).append(ref)
                if not source_ids <= grouped_refs.keys() or any(
                    len(grouped_refs[source_id]) != 1 for source_id in source_ids
                ):
                    continue
                scanned += 1
                completed_sources = {
                    source_id
                    for source_id in source_ids
                    if any((media_item_id, source_id) in watched for media_item_id in group)
                }
                if not completed_sources or completed_sources == source_ids:
                    skipped += 1
                    continue
                if source_type is not None:
                    requested_source = by_type[source_type].id
                    if requested_source not in completed_sources:
                        skipped += 1
                        continue
                target_source_id = next(iter(source_ids - completed_sources))
                target_ref = grouped_refs[target_source_id][0]
                candidates.append(
                    WatchSyncCandidate(
                        target_ref.media_item_id,
                        target_source_id,
                        target_ref.external_id,
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

    def _record_successes(
        self, run_id: int | None, candidates: list[WatchSyncCandidate]
    ) -> None:
        """Persist a successful remote delta in one local transaction."""
        session = self._session_factory()
        try:
            now = self._clock()
            item_ids = {candidate.media_item_id for candidate in candidates}
            source_ids = {candidate.target_source_id for candidate in candidates}
            states = {
                (state.media_item_id, state.source_id): state
                for state in session.scalars(
                    select(WatchState).where(
                        WatchState.media_item_id.in_(item_ids),
                        WatchState.source_id.in_(source_ids),
                    )
                )
            }
            for candidate in candidates:
                key = (candidate.media_item_id, candidate.target_source_id)
                state = states.get(key)
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
                    states[key] = state
                else:
                    state.completed = True
                    state.last_watched_at = now
                    state.view_count = max(1, state.view_count)
                    state.progress_ms = None
                    state.observed_at = now
            if run_id is not None:
                run = session.get(SyncRun, run_id)
                if run is not None:
                    run.watch_sync_updated += len(candidates)
            session.commit()
        finally:
            session.close()

    def _update_progress(
        self, run_id: int | None, scanned: int, updated: int, skipped: int, failed: int
    ) -> None:
        if self._progress is not None:
            self._progress(scanned, updated, skipped, failed)
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

    def _finish(
        self, run_id: int | None, result: WatchSyncResult, *, clear_pending: bool = True
    ) -> None:
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
            if result.failed == 0 and clear_pending:
                pending = session.get(Setting, "watch_sync.pending")
                if pending is not None:
                    pending.value = "false"
            session.commit()
        finally:
            session.close()
