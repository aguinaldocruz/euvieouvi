"""Persistent, retryable queue for instant cross-service updates."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from flask import Flask
from sqlalchemy import case, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from euvieouvi.connectors.dtos import ExternalMediaKind
from euvieouvi.connectors.errors import ConnectorNotFoundError
from euvieouvi.database.enums import LibraryMediaType
from euvieouvi.database.models import (
    AsyncTask,
    Library,
    Source,
    SourceMediaRef,
    WatchState,
)
from euvieouvi.database.unit_of_work import UnitOfWork
from euvieouvi.extensions import db

_MAX_AUTOMATIC_ATTEMPTS = 12


def enqueue_watch_update(
    session: Session,
    *,
    source_id: int,
    external_id: str,
    watched_at: datetime,
    progress_ms: int | None = None,
    completed: bool = True,
    library_external_id: str | None = None,
) -> None:
    identity = (
        f"watch:{source_id}:{external_id}:{watched_at.astimezone(UTC).isoformat()}"
        if completed
        else f"progress:{source_id}:{external_id}"
    )
    dedup_key = hashlib.sha256(identity.encode()).hexdigest()
    serialized = json.dumps(
        {
            "source_id": source_id,
            "external_id": external_id,
            "watched_at": watched_at.astimezone(UTC).isoformat(),
            "progress_ms": progress_ms,
            "completed": completed,
            "library_external_id": library_external_id,
        },
        separators=(",", ":"),
    )
    existing = session.scalar(select(AsyncTask).where(AsyncTask.dedup_key == dedup_key))
    if existing is not None:
        if existing.status == "processing":
            existing.pending_payload = serialized
        else:
            existing.payload = serialized
            existing.status = "pending"
            existing.attempts = 0
            existing.last_error = None
        existing.next_attempt_at = datetime.now(UTC)
        return
    task = AsyncTask(
        task_type="watch_update",
        dedup_key=dedup_key,
        payload=serialized,
        status="pending",
        next_attempt_at=datetime.now(UTC),
    )
    try:
        with session.begin_nested():
            session.add(task)
            session.flush()
    except IntegrityError:
        # Another request inserted the same task after our initial lookup.
        existing = session.scalar(select(AsyncTask).where(AsyncTask.dedup_key == dedup_key))
        if existing is None:
            raise
        if existing.status == "processing":
            existing.pending_payload = serialized
        else:
            existing.payload = serialized
            existing.status = "pending"
            existing.attempts = 0
            existing.last_error = None
        existing.next_attempt_at = datetime.now(UTC)


class AsyncTaskExecutor:
    """Drain due work once; failures remain queued with bounded backoff."""

    def __init__(self, app: Flask) -> None:
        self._app = app
        self._lock = threading.Lock()
        self._active = False
        self._last_submit = 0.0
        self._repair_notices: list[str] = []
        self._snapshot: dict[str, int | bool | str] = {
            "active": False,
            "processed": 0,
            "updated": 0,
            "failed": 0,
            "percent": 100,
            "summary": "Fila aguardando eventos.",
            "failure_details": "",
        }

    @property
    def snapshot(self) -> dict[str, int | bool | str]:
        with self._lock:
            return dict(self._snapshot)

    def submit(self, *, force: bool = False) -> bool:
        with self._lock:
            now = time.monotonic()
            if self._active or (not force and now - self._last_submit < 10):
                return False
            self._active = True
            self._last_submit = now
            self._snapshot.update(
                active=True,
                processed=0,
                updated=0,
                failed=0,
                percent=0,
                summary="Processando atualizações instantâneas pendentes.",
                failure_details="",
            )
            self._repair_notices = []
        threading.Thread(target=self._execute, name="euvieouvi-async-tasks", daemon=True).start()
        return True

    def _execute(self) -> None:
        processed = updated = failed = 0
        failure_details: list[str] = []
        self._repair_notices = []
        fatal_error: Exception | None = None
        try:
            with self._app.app_context():
                while task_id := self._claim_due():
                    succeeded, changed, failure_detail = self._run_one(task_id)
                    processed += 1
                    updated += int(changed)
                    failed += int(not succeeded)
                    if failure_detail:
                        failure_details.append(failure_detail)
                    with self._lock:
                        self._snapshot.update(
                            processed=processed,
                            updated=updated,
                            failed=failed,
                            failure_details="\n".join([*self._repair_notices, *failure_details]),
                            summary=f"{processed} itens da fila processados.",
                        )
        except Exception as error:
            fatal_error = error
            failed += 1
            detail = f"operação=processar fila · erro={type(error).__name__}: {error}"
            failure_details.append(detail)
            self._app.logger.exception("asynchronous task queue failed")
        finally:
            with self._lock:
                self._active = False
                self._snapshot.update(
                    active=False,
                    percent=100,
                    failed=failed,
                    failure_details="\n".join([*self._repair_notices, *failure_details]),
                    summary=(
                        f"Fila interrompida por {type(fatal_error).__name__}: {fatal_error}"
                        if fatal_error is not None
                        else (
                            f"Fila drenada: {processed} processados; {updated} atualizados; "
                            f"{processed - updated - failed} sem alteração; "
                            f"{failed} mantidos para nova tentativa."
                        )
                    ),
                )

    def _claim_due(self) -> int | None:
        now = datetime.now(UTC)
        task_id = db.session.scalar(
            select(AsyncTask.id)
            .where(AsyncTask.status == "pending", AsyncTask.next_attempt_at <= now)
            .order_by(AsyncTask.next_attempt_at, AsyncTask.id)
            .limit(1)
        )
        if task_id is None:
            return None
        result = db.session.execute(
            update(AsyncTask)
            .where(AsyncTask.id == task_id, AsyncTask.status == "pending")
            .values(status="processing", last_attempt_at=now)
        )
        claimed = int(getattr(result, "rowcount", 0) or 0)
        db.session.commit()
        return int(task_id) if claimed else None

    def _run_one(self, task_id: int) -> tuple[bool, bool, str | None]:
        task = db.session.get(AsyncTask, task_id)
        if task is None:
            return True, False, None
        try:
            if task.task_type != "watch_update":
                raise ValueError(f"unsupported task type: {task.task_type}")
            payload = json.loads(task.payload)
            changed = (
                False if self._is_superseded(task, payload) else self._apply_watch_update(payload)
            )
            # End any read transaction before atomically deciding whether this
            # execution has a successor queued by a concurrent webhook.
            db.session.commit()
            removed = db.session.execute(
                delete(AsyncTask).where(
                    AsyncTask.id == task_id,
                    AsyncTask.pending_payload.is_(None),
                )
            )
            if int(getattr(removed, "rowcount", 0) or 0) == 0:
                db.session.execute(
                    update(AsyncTask)
                    .where(AsyncTask.id == task_id, AsyncTask.pending_payload.is_not(None))
                    .values(
                        payload=AsyncTask.pending_payload,
                        pending_payload=None,
                        status="pending",
                        attempts=0,
                        next_attempt_at=datetime.now(UTC),
                    )
                )
            db.session.commit()
            return True, changed, None
        except Exception as error:
            db.session.rollback()
            task = db.session.get(AsyncTask, task_id)
            if task is None:
                return False, False, f"task={task_id} · erro={type(error).__name__}: {error}"
            attempts = task.attempts + 1
            status = "blocked" if attempts >= _MAX_AUTOMATIC_ATTEMPTS else "pending"
            db.session.execute(
                update(AsyncTask)
                .where(AsyncTask.id == task_id)
                .values(
                    attempts=attempts,
                    status=status,
                    last_error=f"{type(error).__name__}: {error}"[:1000],
                    next_attempt_at=datetime.now(UTC)
                    + timedelta(seconds=min(3600, 15 * (2 ** min(attempts - 1, 8)))),
                    payload=case(
                        (AsyncTask.pending_payload.is_not(None), AsyncTask.pending_payload),
                        else_=AsyncTask.payload,
                    ),
                    pending_payload=None,
                )
            )
            db.session.commit()
            task = db.session.get(AsyncTask, task_id)
            if task is None:
                return False, False, f"task={task_id} · erro={type(error).__name__}: {error}"
            detail = self._failure_detail(task, error)
            self._app.logger.warning(
                "asynchronous task %s: %s",
                "isolated after repeated failures" if status == "blocked" else "retained for retry",
                detail,
                exc_info=True,
            )
            return False, False, detail

    @staticmethod
    def _is_superseded(task: AsyncTask, payload: dict[str, object]) -> bool:
        """Drop an obsolete resume-position event when completion is already queued."""
        if bool(payload.get("completed", True)):
            return False
        source_id = str(payload.get("source_id") or "")
        external_id = str(payload.get("external_id") or "")
        for candidate in db.session.scalars(
            select(AsyncTask).where(
                AsyncTask.task_type == "watch_update",
                AsyncTask.id != task.id,
                AsyncTask.status.in_(("pending", "processing", "blocked")),
            )
        ):
            try:
                successor = json.loads(candidate.payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                bool(successor.get("completed", True))
                and str(successor.get("source_id") or "") == source_id
                and str(successor.get("external_id") or "") == external_id
            ):
                return True
        return False

    @staticmethod
    def _failure_detail(task: AsyncTask, error: Exception) -> str:
        try:
            payload = json.loads(task.payload)
        except (TypeError, ValueError):
            payload = {}
        source_id = str(payload.get("source_id") or "?")
        external_id = str(payload.get("external_id") or "?")
        return (
            f"task={task.id} · tipo={task.task_type} · origem={source_id} · "
            f"mídia={external_id} · tentativa={task.attempts} · "
            f"erro={type(error).__name__}: {error}"
        )[:2000]

    def retry_all(self) -> bool:
        with self._app.app_context():
            db.session.execute(
                update(AsyncTask)
                .where(AsyncTask.status.in_(("pending", "blocked")))
                .values(
                    status="pending",
                    attempts=0,
                    last_error=None,
                    next_attempt_at=datetime.now(UTC),
                )
            )
            db.session.commit()
        return self.submit(force=True)

    def _apply_watch_update(self, payload: dict[str, object]) -> bool:
        from euvieouvi.api.runtime import connector_for

        raw_source_id = payload["source_id"]
        if not isinstance(raw_source_id, (int, str)):
            raise ValueError("queued source id is invalid")
        source_id = int(raw_source_id)
        external_id = str(payload["external_id"])
        watched_at = datetime.fromisoformat(str(payload["watched_at"]))
        watched_at = (
            watched_at.replace(tzinfo=UTC)
            if watched_at.tzinfo is None
            else watched_at.astimezone(UTC)
        )
        completed = bool(payload.get("completed", True))
        raw_progress = payload.get("progress_ms")
        if raw_progress is not None and (
            isinstance(raw_progress, bool) or not isinstance(raw_progress, (int, str))
        ):
            raise ValueError("queued progress is invalid")
        progress_ms = int(raw_progress) if raw_progress is not None else None
        if not completed and (progress_ms is None or progress_ms <= 0):
            return False
        origin = db.session.get(Source, source_id)
        if origin is None:
            raise LookupError("watch update source is not available")
        reference = self._origin_reference(origin, external_id, payload)
        if reference is None:
            raise LookupError("source media reference is not available yet")
        targets = db.session.execute(
            select(SourceMediaRef, Source)
            .join(Source, Source.id == SourceMediaRef.source_id)
            .where(
                SourceMediaRef.media_item_id == reference.media_item_id,
                SourceMediaRef.source_id != source_id,
                SourceMediaRef.available.is_(True),
                Source.enabled.is_(True),
            )
        ).all()
        if not targets:
            stale_target = db.session.scalar(
                select(SourceMediaRef.id)
                .join(Source, Source.id == SourceMediaRef.source_id)
                .where(
                    SourceMediaRef.media_item_id == reference.media_item_id,
                    SourceMediaRef.source_id != source_id,
                    SourceMediaRef.available.is_(False),
                    Source.enabled.is_(True),
                )
                .limit(1)
            )
            if stale_target is not None:
                raise LookupError(
                    "all known cross-server references are stale; the event remains queued "
                    "until catalog synchronization repairs the mapping"
                )
            return False
        grouped_targets: dict[int, tuple[Source, list[SourceMediaRef]]] = {}
        for target, source in targets:
            grouped_targets.setdefault(source.id, (source, []))[1].append(target)

        changed = False
        for source, references in grouped_targets.values():
            references.sort(key=_reference_recency, reverse=True)
            target = references[0]
            state = db.session.scalar(
                select(WatchState).where(
                    WatchState.media_item_id == target.media_item_id,
                    WatchState.source_id == target.source_id,
                )
            )
            if state is not None and state.completed:
                continue
            if not completed:
                assert progress_ms is not None
                target_progress = state.progress_ms if state is not None else None
                if target_progress == progress_ms:
                    continue
                if (
                    origin.connector_type.value == "jellyfin"
                    and source.connector_type.value == "plex"
                    and (target_progress or 0) > 0
                ):
                    continue
            connector = connector_for(source)
            successful = 0
            other_error: Exception | None = None
            try:
                for candidate in references:
                    try:
                        if completed:
                            connector.mark_watched(candidate.external_id, watched_at=watched_at)
                        else:
                            assert progress_ms is not None
                            connector.set_progress(candidate.external_id, progress_ms)
                    except ConnectorNotFoundError:
                        self._invalidate_reference(candidate, source)
                    except Exception as error:
                        other_error = error
                        break
                    else:
                        successful += 1
            finally:
                close = getattr(connector, "close", None)
                if callable(close):
                    close()
            if other_error is not None:
                raise other_error
            if successful == 0:
                raise LookupError(
                    f"all {len(references)} known references on {source.name} were stale; "
                    "the event remains queued until catalog synchronization repairs the mapping"
                )
            if state is None:
                state = WatchState(
                    media_item_id=target.media_item_id,
                    source_id=target.source_id,
                    view_count=1 if completed else 0,
                    completed=completed,
                    observed_at=watched_at,
                )
                db.session.add(state)
            if completed:
                state.view_count = max(state.view_count, 1)
                state.completed = True
                state.last_watched_at = watched_at
                state.progress_ms = None
            else:
                state.completed = False
                state.progress_ms = progress_ms
            previous_played = _utc(state.last_played_at)
            if previous_played is None or watched_at > previous_played:
                state.last_played_at = watched_at
            state.observed_at = watched_at
            changed = True
        db.session.commit()
        return changed

    def _origin_reference(
        self, origin: Source, external_id: str, payload: dict[str, object]
    ) -> SourceMediaRef | None:
        reference = db.session.scalar(
            select(SourceMediaRef).where(
                SourceMediaRef.source_id == origin.id,
                SourceMediaRef.external_id == external_id,
                SourceMediaRef.available.is_(True),
            )
        )
        if reference is not None:
            return reference
        self._repair_missing_origin_reference(origin, external_id, payload)
        return db.session.scalar(
            select(SourceMediaRef).where(
                SourceMediaRef.source_id == origin.id,
                SourceMediaRef.external_id == external_id,
                SourceMediaRef.available.is_(True),
            )
        )

    def _repair_missing_origin_reference(
        self, origin: Source, external_id: str, payload: dict[str, object]
    ) -> None:
        """Fetch and persist one webhook item instead of waiting for a full source scan."""
        from euvieouvi.api.runtime import connector_for
        from euvieouvi.sync.persistence import MediaPersistenceService

        libraries = db.session.scalars(
            select(Library)
            .where(
                Library.source_id == origin.id,
                Library.enabled.is_(True),
                Library.available.is_(True),
            )
            .order_by(Library.id)
        ).all()
        if not libraries:
            raise LookupError("source has no enabled library for targeted media recovery")
        requested_library = str(payload.get("library_external_id") or "").strip()
        probe = next(
            (library for library in libraries if library.external_id == requested_library),
            libraries[0],
        )
        connector = connector_for(origin)
        item = None
        try:
            lookup = getattr(connector, "get_media_item", None)
            if not callable(lookup):
                raise LookupError("source connector does not support targeted media recovery")
            item = lookup(external_id, probe.external_id)
            wanted_type = {
                ExternalMediaKind.MOVIE: LibraryMediaType.MOVIE,
                ExternalMediaKind.EPISODE: LibraryMediaType.SHOW,
                ExternalMediaKind.TRACK: LibraryMediaType.ARTIST,
            }[item.kind]
            library = next(
                (value for value in libraries if value.external_id == item.library_external_id),
                None,
            ) or next((value for value in libraries if value.media_type is wanted_type), probe)
            if item.library_external_id != library.external_id:
                item = replace(item, library_external_id=library.external_id)
            MediaPersistenceService(
                UnitOfWork(db.session()), source_id=origin.id, library_id=library.id
            ).persist_media(item, datetime.now(UTC))
            db.session.commit()
            self._record_repair_notice(
                f"AUTO-REPARO · origem={origin.name} · tipo={item.kind.value} · "
                f"título={item.title} · externo={external_id} · referência recriada"
            )
        except ConnectorNotFoundError:
            raise
        except Exception as error:
            db.session.rollback()
            description = (
                f"{item.kind.value} '{item.title}'" if item is not None else "mídia desconhecida"
            )
            raise LookupError(
                f"targeted recovery failed for {description} ({external_id}): "
                f"{type(error).__name__}: {error}"
            ) from error
        finally:
            close = getattr(connector, "close", None)
            if callable(close):
                close()

    def _invalidate_reference(self, reference: SourceMediaRef, source: Source) -> None:
        now = datetime.now(UTC)
        reference.available = False
        reference.unavailable_since = now
        # This repair must survive if every known reference is stale and the
        # queue item is retained for a later catalog synchronization.
        db.session.commit()
        self._record_repair_notice(
            f"AUTO-REPARO · destino={source.name} · externo={reference.external_id} · "
            "referência inexistente desativada"
        )

    def _record_repair_notice(self, detail: str) -> None:
        self._repair_notices.append(detail)
        with self._lock:
            self._snapshot["failure_details"] = "\n".join(self._repair_notices)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _reference_recency(reference: SourceMediaRef) -> tuple[datetime, int]:
    return (_utc(reference.last_seen_at) or datetime.min.replace(tzinfo=UTC), reference.id)


def get_async_task_executor(app: Flask) -> AsyncTaskExecutor:
    concrete = app._get_current_object() if hasattr(app, "_get_current_object") else app
    executor = concrete.extensions.get("euvieouvi.async_tasks")
    if not isinstance(executor, AsyncTaskExecutor):
        executor = AsyncTaskExecutor(concrete)
        concrete.extensions["euvieouvi.async_tasks"] = executor
    return executor


def recover_async_tasks() -> int:
    result = db.session.execute(
        update(AsyncTask)
        .where(AsyncTask.status == "processing")
        .values(
            status="pending",
            payload=case(
                (AsyncTask.pending_payload.is_not(None), AsyncTask.pending_payload),
                else_=AsyncTask.payload,
            ),
            pending_payload=None,
        )
    )
    count = int(getattr(result, "rowcount", 0) or 0)
    db.session.commit()
    return int(count)
