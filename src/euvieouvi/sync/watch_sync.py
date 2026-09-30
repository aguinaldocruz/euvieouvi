"""Cross-source propagation of completed watch state after catalog synchronization."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased

from euvieouvi.connectors.base import WatchConnector
from euvieouvi.connectors.errors import ConnectorNotFoundError
from euvieouvi.credentials import decrypt_text
from euvieouvi.database.enums import ConnectorType, MediaKind, SyncStatus
from euvieouvi.database.models import (
    MediaFilePath,
    MediaIdentifier,
    MediaItem,
    Setting,
    Source,
    SourceMediaRef,
    SyncRun,
    WatchEvent,
    WatchState,
)

ConnectorFactory = Callable[[Source], WatchConnector]
SessionFactory = Callable[[], Session]
ProgressCallback = Callable[[int, int, int, int], None]
FailureCallback = Callable[[str], None]
CancellationCallback = Callable[[], bool]
logger = logging.getLogger(__name__)
_SYNCABLE_KINDS = {MediaKind.MOVIE, MediaKind.EPISODE, MediaKind.TRACK}
_PERSIST_BATCH_SIZE = 100
_QUERY_BATCH_SIZE = 900
_FULL_SCAN_INTERVAL = timedelta(days=7)
_IDENTITY_PROVIDERS = ("tmdb", "tvdb", "imdb")


@dataclass(frozen=True, slots=True)
class WatchSyncTarget:
    media_item_id: int
    reference_id: int
    external_id: str
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class WatchSyncCandidate:
    target_source_id: int
    targets: tuple[WatchSyncTarget, ...]
    watched_at: datetime | None
    progress_ms: int | None
    completed: bool
    last_played_at: datetime | None = None

    @property
    def media_item_id(self) -> int:
        return self.targets[0].media_item_id


@dataclass(frozen=True, slots=True)
class WatchSyncResult:
    scanned: int
    updated: int
    skipped: int
    failed: int
    repaired: int = 0


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
    physical: dict[int, set[str]] = {}
    path_owners: dict[tuple[MediaKind, str], int] = {}
    for batch in _batches(item_ids):
        for media_id, path_key in session.execute(
            select(SourceMediaRef.media_item_id, MediaFilePath.path_key)
            .join(MediaFilePath, MediaFilePath.reference_id == SourceMediaRef.id)
            .where(SourceMediaRef.media_item_id.in_(batch), SourceMediaRef.available.is_(True))
        ):
            physical.setdefault(media_id, set()).add(path_key)
            physical_kind = kinds[media_id]
            owner = path_owners.setdefault((physical_kind, path_key), media_id)
            parents[find(media_id)] = find(owner)
    for batch in _batches(item_ids):
        rows = session.execute(
            select(
                MediaIdentifier.media_item_id,
                MediaIdentifier.provider,
                MediaIdentifier.external_id,
            ).where(
                MediaIdentifier.media_item_id.in_(batch),
                MediaIdentifier.provider.in_(_IDENTITY_PROVIDERS),
            )
        )
        for media_item_id, provider, external_id in rows:
            kind = kinds.get(media_item_id)
            if kind not in _SYNCABLE_KINDS:
                continue
            key = (kind, provider, external_id)
            existing = identifiers.setdefault(key, media_item_id)
            if (
                physical.get(existing)
                and physical.get(media_item_id)
                and not (physical[existing] & physical[media_item_id])
            ):
                # Shared libraries: conflicting physical files outweigh reused
                # or incorrect episode/provider ids when propagating completion.
                continue
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
        failure: FailureCallback | None = None,
        cancelled: CancellationCallback | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._connector_factory = connector_factory
        self._clock = clock
        self._progress = progress
        self._failure = failure
        self._cancelled = cancelled

    def run(
        self,
        run_id: int | None,
        *,
        source_type: ConnectorType | None = None,
    ) -> WatchSyncResult:
        """Propagate completions, optionally in one explicit source direction."""
        scan_started_at = self._clock()
        candidates, scanned, skipped, incremental = self._prepare(
            run_id,
            source_type=source_type,
            scan_started_at=scan_started_at,
        )
        connectors: dict[int, WatchConnector] = {}
        pending_successes: list[WatchSyncCandidate] = []
        updated = 0
        failed = 0
        repaired = 0
        self._update_progress(run_id, scanned, updated, skipped, failed)
        try:
            for candidate in candidates:
                if self._cancelled is not None and self._cancelled():
                    from euvieouvi.sync.errors import SyncCancelledError

                    raise SyncCancelledError("Watched-state propagation was cancelled.")
                try:
                    connector = connectors.get(candidate.target_source_id)
                    if connector is None:
                        connector = self._connector(candidate.target_source_id)
                        connectors[candidate.target_source_id] = connector
                    successful = 0
                    for target in candidate.targets:
                        try:
                            if candidate.completed:
                                connector.mark_watched(
                                    target.external_id, watched_at=candidate.watched_at
                                )
                            elif candidate.progress_ms is not None:
                                connector.set_progress(target.external_id, candidate.progress_ms)
                        except ConnectorNotFoundError:
                            repaired += int(self._invalidate_reference(candidate, target))
                        else:
                            successful += 1
                    if successful == 0:
                        raise LookupError(
                            "All known target references were stale; synchronize the target "
                            "catalog before retrying."
                        )
                except Exception as error:
                    failed += 1
                    detail = self._failure_detail(candidate, error)
                    logger.warning("Watched-state item failed: %s", detail, exc_info=True)
                    if self._failure is not None:
                        self._failure(detail)
                else:
                    pending_successes.append(candidate)
                    updated += 1
                    if len(pending_successes) >= _PERSIST_BATCH_SIZE:
                        self._record_successes(run_id, pending_successes)
                        pending_successes.clear()
                self._update_progress(run_id, scanned, updated, skipped, failed)
            if pending_successes:
                self._record_successes(run_id, pending_successes)
            result = WatchSyncResult(scanned, updated, skipped, failed, repaired)
            self._finish(
                run_id,
                result,
                clear_pending=source_type is None,
                incremental=incremental,
                scan_started_at=scan_started_at,
            )
            return result
        finally:
            for connector in connectors.values():
                close = getattr(connector, "close", None)
                if callable(close):
                    close()

    def _failure_detail(self, candidate: WatchSyncCandidate, error: Exception) -> str:
        session = self._session_factory()
        try:
            item = session.get(MediaItem, candidate.media_item_id)
            source = session.get(Source, candidate.target_source_id)
            kind = item.kind.value if item is not None else "desconhecido"
            title = item.title if item is not None else "item ausente"
            target = source.name if source is not None else f"fonte {candidate.target_source_id}"
            return (
                f"operação=propagar estado de reprodução · tipo={kind} · título={title} · "
                f"item={candidate.media_item_id} · destino={target} · "
                f"externo={','.join(target.external_id for target in candidate.targets)} · "
                f"erro={type(error).__name__}: {error}"
            )
        finally:
            session.close()

    def _invalidate_reference(self, candidate: WatchSyncCandidate, target: WatchSyncTarget) -> bool:
        session = self._session_factory()
        try:
            reference = session.get(SourceMediaRef, target.reference_id)
            if reference is None or not reference.available:
                return False
            reference.available = False
            reference.unavailable_since = self._clock()
            source = session.get(Source, candidate.target_source_id)
            source_name = source.name if source is not None else str(candidate.target_source_id)
            item = session.get(MediaItem, target.media_item_id)
            detail = (
                f"AUTO-REPARO · destino={source_name} · "
                f"tipo={item.kind.value if item is not None else 'desconhecido'} · "
                f"título={item.title if item is not None else 'item ausente'} · "
                f"externo={target.external_id} · referência inexistente desativada"
            )
            session.commit()
            logger.warning(detail)
            if self._failure is not None:
                self._failure(detail)
            return True
        finally:
            session.close()

    def _prepare(
        self,
        run_id: int | None,
        *,
        source_type: ConnectorType | None = None,
        scan_started_at: datetime,
    ) -> tuple[tuple[WatchSyncCandidate, ...], int, int, tuple[str, bool] | None]:
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
                return (), 0, 0, None
            source_ids = {source.id for source in by_type.values()}
            configured_users: dict[int, str] = {}
            plex_user = session.get(Setting, "plex.user_id") or session.get(
                Setting, "webhook.plex.user_filter"
            )
            if plex_user is not None and plex_user.value.strip():
                configured_users[by_type[ConnectorType.PLEX].id] = plex_user.value.strip()
            try:
                secret = json.loads(decrypt_text(by_type[ConnectorType.JELLYFIN].secret))
                jellyfin_user = str(secret.get("user_id") or "").strip()
            except (TypeError, ValueError, json.JSONDecodeError):
                jellyfin_user = ""
            if jellyfin_user:
                configured_users[by_type[ConnectorType.JELLYFIN].id] = jellyfin_user
            if configured_users.keys() != source_ids:
                if run is not None:
                    return self._start_without_candidates(run, session)
                return (), 0, 0, None
            scope_ids, checkpoint_key, full_scan = self._incremental_scope(
                session,
                source_ids,
                by_type,
                source_type=source_type,
                scan_started_at=scan_started_at,
            )
            ref_query = select(SourceMediaRef).where(
                SourceMediaRef.source_id.in_(source_ids),
                SourceMediaRef.available.is_(True),
            )
            if scope_ids is not None:
                if not scope_ids:
                    refs: Sequence[SourceMediaRef] = []
                else:
                    refs = [
                        ref
                        for batch in _batches(scope_ids)
                        for ref in session.scalars(
                            ref_query.where(SourceMediaRef.media_item_id.in_(batch))
                        )
                    ]
            else:
                refs = session.scalars(ref_query).all()
            item_ids = {ref.media_item_id for ref in refs}
            kinds: dict[int, MediaKind] = {}
            for batch in _batches(item_ids):
                kinds.update(
                    {
                        item.id: item.kind
                        for item in session.scalars(
                            select(MediaItem).where(MediaItem.id.in_(batch))
                        )
                    }
                )
            watched: dict[tuple[int, int], datetime] = {}

            def remember(media_item_id: int, source_id: int, watched_at: datetime | None) -> None:
                if watched_at is None:
                    return
                normalized = (
                    watched_at.replace(tzinfo=UTC)
                    if watched_at.tzinfo is None
                    else watched_at.astimezone(UTC)
                )
                key = (media_item_id, source_id)
                if key not in watched or normalized > watched[key]:
                    watched[key] = normalized

            current_states = [
                state
                for batch in _batches(item_ids)
                for state in session.scalars(
                    select(WatchState).where(
                        WatchState.source_id.in_(source_ids),
                        WatchState.media_item_id.in_(batch),
                    )
                )
            ]
            partial_progress: dict[tuple[int, int], int] = {}
            playback_dates: dict[tuple[int, int], datetime] = {}
            completion_dates: dict[tuple[int, int], datetime] = {}
            current_state_keys = {
                (state.media_item_id, state.source_id) for state in current_states
            }
            for state in current_states:
                actual_playback = state.last_played_at or state.last_watched_at
                if actual_playback is not None:
                    playback_dates[(state.media_item_id, state.source_id)] = _as_utc(
                        actual_playback
                    )
                if state.completed and state.last_watched_at is not None:
                    completion_dates[(state.media_item_id, state.source_id)] = _as_utc(
                        state.last_watched_at
                    )
                if not state.completed and (state.progress_ms or 0) > 0:
                    partial_progress[(state.media_item_id, state.source_id)] = int(
                        state.progress_ms or 0
                    )
                if state.completed:
                    remember(
                        state.media_item_id,
                        state.source_id,
                        state.last_watched_at or state.observed_at,
                    )
            for source_id, configured_user in configured_users.items():
                for batch in _batches(item_ids):
                    for media_item_id, event_source_id, watched_at in session.execute(
                        select(
                            WatchEvent.media_item_id,
                            WatchEvent.source_id,
                            WatchEvent.watched_at,
                        ).where(
                            WatchEvent.source_id == source_id,
                            WatchEvent.media_item_id.in_(batch),
                            WatchEvent.completed.is_(True),
                            WatchEvent.playback_user == configured_user,
                        )
                    ):
                        if (media_item_id, event_source_id) not in current_state_keys:
                            remember(media_item_id, event_source_id, watched_at)
                        key = (media_item_id, event_source_id)
                        completion_dates[key] = max(
                            completion_dates.get(key, _as_utc(watched_at)), _as_utc(watched_at)
                        )
                        playback_dates[key] = max(
                            playback_dates.get(key, completion_dates[key]), completion_dates[key]
                        )
            refs_by_item: dict[int, dict[int, list[SourceMediaRef]]] = {}
            for ref in refs:
                refs_by_item.setdefault(ref.media_item_id, {}).setdefault(ref.source_id, []).append(
                    ref
                )
            groups = _matching_item_groups(session, item_ids, kinds)
            candidates: list[WatchSyncCandidate] = []
            scanned = 0
            skipped = 0
            for group in groups:
                grouped_refs: dict[int, list[SourceMediaRef]] = {}
                for media_item_id in group:
                    for source_id, item_refs in refs_by_item.get(media_item_id, {}).items():
                        grouped_refs.setdefault(source_id, []).extend(item_refs)
                if not source_ids <= grouped_refs.keys():
                    continue
                scanned += 1
                watched_by_source = {
                    source_id: max(
                        (
                            watched[(media_item_id, source_id)]
                            for media_item_id in group
                            if (media_item_id, source_id) in watched
                        ),
                        default=None,
                    )
                    for source_id in source_ids
                }
                progress_by_source = {
                    source_id: max(
                        (
                            partial_progress.get((media_item_id, source_id), 0)
                            for media_item_id in group
                        ),
                        default=0,
                    )
                    for source_id in source_ids
                }
                source_watched_at: datetime | None = None
                source_progress: int | None = None
                completed = False
                if source_type is not None:
                    requested_source = by_type[source_type].id
                    target_source_id = next(iter(source_ids - {requested_source}))
                    source_watched_at = watched_by_source[requested_source]
                    target_watched_at = watched_by_source[target_source_id]
                    requested_progress = progress_by_source[requested_source]
                    target_progress = progress_by_source[target_source_id]
                    if source_watched_at is not None and target_watched_at is None:
                        completed = True
                    elif (
                        source_watched_at is not None
                        or target_watched_at is not None
                        or requested_progress <= 0
                        or requested_progress == target_progress
                    ):
                        skipped += 1
                        continue
                    elif source_type is ConnectorType.JELLYFIN and target_progress > 0:
                        # Plex owns conflicts: Jellyfin fills only an empty Plex resume position.
                        skipped += 1
                        continue
                    else:
                        source_progress = requested_progress
                else:
                    plex_source_id = by_type[ConnectorType.PLEX].id
                    jellyfin_source_id = by_type[ConnectorType.JELLYFIN].id
                    plex_watched_at = watched_by_source[plex_source_id]
                    jellyfin_watched_at = watched_by_source[jellyfin_source_id]
                    if plex_watched_at is not None and jellyfin_watched_at is None:
                        target_source_id = jellyfin_source_id
                        source_watched_at = plex_watched_at
                        completed = True
                    elif plex_watched_at is None and jellyfin_watched_at is not None:
                        target_source_id = plex_source_id
                        source_watched_at = jellyfin_watched_at
                        completed = True
                    elif plex_watched_at is not None or jellyfin_watched_at is not None:
                        skipped += 1
                        continue
                    elif progress_by_source[plex_source_id] > 0 and (
                        progress_by_source[plex_source_id] != progress_by_source[jellyfin_source_id]
                    ):
                        target_source_id = jellyfin_source_id
                        source_progress = progress_by_source[plex_source_id]
                    elif progress_by_source[jellyfin_source_id] > 0:
                        target_source_id = plex_source_id
                        source_progress = progress_by_source[jellyfin_source_id]
                    else:
                        skipped += 1
                        continue
                target_refs = sorted(
                    grouped_refs[target_source_id],
                    key=lambda ref: (_as_utc(ref.last_seen_at), ref.id),
                    reverse=True,
                )
                if completed:
                    # Completion can be known without a date. Propagate the flag,
                    # never the collection-time fallback used to select candidates.
                    source_watched_at = max(
                        (
                            completion_dates[(media_item_id, source_id)]
                            for media_item_id in group
                            for source_id in source_ids - {target_source_id}
                            if (media_item_id, source_id) in completion_dates
                        ),
                        default=None,
                    )
                candidates.append(
                    WatchSyncCandidate(
                        target_source_id,
                        tuple(
                            WatchSyncTarget(
                                ref.media_item_id,
                                ref.id,
                                ref.external_id,
                                _as_utc(ref.last_seen_at),
                            )
                            for ref in target_refs
                        ),
                        source_watched_at,
                        source_progress,
                        completed,
                        max(
                            (
                                playback_dates[(media_item_id, source_id)]
                                for media_item_id in group
                                for source_id in source_ids - {target_source_id}
                                if (media_item_id, source_id) in playback_dates
                            ),
                            default=None,
                        ),
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
            return tuple(candidates), scanned, skipped, (checkpoint_key, full_scan)
        finally:
            session.close()

    def _start_without_candidates(
        self, run: SyncRun, session: Session
    ) -> tuple[tuple[WatchSyncCandidate, ...], int, int, None]:
        now = self._clock()
        run.watch_sync_status = SyncStatus.SUCCEEDED
        run.watch_sync_started_at = now
        run.watch_sync_finished_at = now
        run.watch_sync_summary = "Propagação ignorada: Plex e Jellyfin ativos são necessários."
        session.commit()
        return (), 0, 0, None

    def _incremental_scope(
        self,
        session: Session,
        source_ids: set[int],
        by_type: dict[ConnectorType, Source],
        *,
        source_type: ConnectorType | None,
        scan_started_at: datetime,
    ) -> tuple[set[int] | None, str, bool]:
        direction = source_type.value if source_type is not None else "bidirectional"
        key = f"watch_sync.incremental.{direction}.last_success_at"
        full_key = f"watch_sync.incremental.{direction}.last_full_at"
        cutoff = _setting_datetime(session.get(Setting, key))
        last_full = _setting_datetime(session.get(Setting, full_key))
        if (
            cutoff is None
            or last_full is None
            or scan_started_at - last_full >= _FULL_SCAN_INTERVAL
        ):
            return None, direction, True

        relevant_source_ids = {by_type[source_type].id} if source_type is not None else source_ids
        changed_ids: set[int] = set(
            session.scalars(
                select(WatchState.media_item_id).where(
                    WatchState.source_id.in_(relevant_source_ids),
                    WatchState.updated_at >= cutoff,
                )
            )
        )
        changed_ids.update(
            session.scalars(
                select(WatchEvent.media_item_id).where(
                    WatchEvent.source_id.in_(relevant_source_ids),
                    WatchEvent.updated_at >= cutoff,
                )
            )
        )
        changed_ids.update(
            session.scalars(
                select(SourceMediaRef.media_item_id).where(
                    SourceMediaRef.source_id.in_(source_ids),
                    SourceMediaRef.updated_at >= cutoff,
                )
            )
        )
        changed_ids.update(
            session.scalars(
                select(MediaIdentifier.media_item_id).where(
                    MediaIdentifier.provider.in_(_IDENTITY_PROVIDERS),
                    MediaIdentifier.created_at >= cutoff,
                )
            )
        )
        if not changed_ids:
            return set(), direction, False

        candidate_identity = aliased(MediaIdentifier)
        matching_ids: set[int] = set()
        for batch in _batches(changed_ids):
            matching_ids.update(
                session.scalars(
                    select(MediaIdentifier.media_item_id)
                    .join(
                        candidate_identity,
                        and_(
                            candidate_identity.provider == MediaIdentifier.provider,
                            candidate_identity.external_id == MediaIdentifier.external_id,
                        ),
                    )
                    .where(
                        candidate_identity.media_item_id.in_(batch),
                        candidate_identity.provider.in_(_IDENTITY_PROVIDERS),
                    )
                )
            )
        changed_ids.update(matching_ids)
        return changed_ids, direction, False

    def _connector(self, source_id: int) -> WatchConnector:
        session = self._session_factory()
        try:
            source = session.get(Source, source_id)
            if source is None:
                raise LookupError("Watch synchronization target disappeared")
            session.expunge(source)
            return self._connector_factory(source)
        finally:
            session.close()

    def _record_successes(self, run_id: int | None, candidates: list[WatchSyncCandidate]) -> None:
        """Persist a successful remote delta in one local transaction."""
        session = self._session_factory()
        try:
            now = self._clock()
            item_ids = {
                target.media_item_id for candidate in candidates for target in candidate.targets
            }
            source_ids = {candidate.target_source_id for candidate in candidates}
            states: dict[tuple[int, int], WatchState] = {}
            for batch in _batches(item_ids):
                states.update(
                    {
                        (state.media_item_id, state.source_id): state
                        for state in session.scalars(
                            select(WatchState).where(
                                WatchState.media_item_id.in_(batch),
                                WatchState.source_id.in_(source_ids),
                            )
                        )
                    }
                )
            for candidate in candidates:
                for media_item_id in {target.media_item_id for target in candidate.targets}:
                    key = (media_item_id, candidate.target_source_id)
                    state = states.get(key)
                    if state is None:
                        state = WatchState(
                            media_item_id=media_item_id,
                            source_id=candidate.target_source_id,
                            view_count=1 if candidate.completed else 0,
                            completed=candidate.completed,
                            last_watched_at=candidate.watched_at,
                            progress_ms=candidate.progress_ms,
                            observed_at=now,
                        )
                        session.add(state)
                        states[key] = state
                    elif candidate.completed:
                        state.completed = True
                        state.last_watched_at = candidate.watched_at
                        state.view_count = max(1, state.view_count)
                        state.progress_ms = None
                        state.observed_at = now
                    elif not state.completed:
                        state.progress_ms = candidate.progress_ms
                        state.observed_at = now
                    if candidate.last_played_at is not None and (
                        state.last_played_at is None
                        or _as_utc(candidate.last_played_at) > _as_utc(state.last_played_at)
                    ):
                        state.last_played_at = candidate.last_played_at
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
        self,
        run_id: int | None,
        result: WatchSyncResult,
        *,
        clear_pending: bool = True,
        incremental: tuple[str, bool] | None = None,
        scan_started_at: datetime,
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
                    f"{result.skipped} já alinhados, {result.repaired} referências reparadas "
                    f"e {result.failed} falhas."
                )
            if result.failed == 0 and clear_pending:
                pending = session.get(Setting, "watch_sync.pending")
                if pending is not None:
                    pending.value = "false"
            if result.failed == 0 and incremental is not None:
                direction, full_scan = incremental
                _set_timestamp_setting(
                    session,
                    f"watch_sync.incremental.{direction}.last_success_at",
                    scan_started_at,
                )
                if full_scan:
                    _set_timestamp_setting(
                        session,
                        f"watch_sync.incremental.{direction}.last_full_at",
                        scan_started_at,
                    )
            session.commit()
        finally:
            session.close()


def _setting_datetime(setting: Setting | None) -> datetime | None:
    if setting is None:
        return None
    try:
        value = datetime.fromisoformat(setting.value)
    except ValueError:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _batches(values: set[int]) -> tuple[tuple[int, ...], ...]:
    """Keep bind counts below SQLite's portable per-statement limit."""
    ordered = tuple(values)
    return tuple(
        ordered[offset : offset + _QUERY_BATCH_SIZE]
        for offset in range(0, len(ordered), _QUERY_BATCH_SIZE)
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _set_timestamp_setting(session: Session, key: str, value: datetime) -> None:
    setting = session.get(Setting, key)
    serialized = value.astimezone(UTC).isoformat()
    if setting is None:
        session.add(Setting(key=key, value=serialized))
    else:
        setting.value = serialized
