"""Persistent, retryable queue for instant cross-service updates."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from flask import Flask
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from euvieouvi.database.models import AsyncTask, Source, SourceMediaRef, WatchState
from euvieouvi.extensions import db


def enqueue_watch_update(
    session: Session,
    *,
    source_id: int,
    external_id: str,
    watched_at: datetime,
) -> None:
    identity = f"watch:{source_id}:{external_id}:{watched_at.astimezone(UTC).isoformat()}"
    task = AsyncTask(
        task_type="watch_update",
        dedup_key=hashlib.sha256(identity.encode()).hexdigest(),
        payload=json.dumps(
            {
                "source_id": source_id,
                "external_id": external_id,
                "watched_at": watched_at.astimezone(UTC).isoformat(),
            },
            separators=(",", ":"),
        ),
        status="pending",
        next_attempt_at=datetime.now(UTC),
    )
    with session.begin_nested():
        session.add(task)
        with suppress(IntegrityError):
            session.flush()


class AsyncTaskExecutor:
    """Drain due work once; failures remain queued with bounded backoff."""

    def __init__(self, app: Flask) -> None:
        self._app = app
        self._lock = threading.Lock()
        self._active = False
        self._last_submit = 0.0
        self._snapshot: dict[str, int | bool | str] = {
            "active": False,
            "processed": 0,
            "updated": 0,
            "failed": 0,
            "percent": 100,
            "summary": "Fila aguardando eventos.",
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
            )
        threading.Thread(target=self._execute, name="euvieouvi-async-tasks", daemon=True).start()
        return True

    def _execute(self) -> None:
        processed = updated = failed = 0
        try:
            with self._app.app_context():
                while task_id := self._claim_due():
                    succeeded = self._run_one(task_id)
                    processed += 1
                    updated += int(succeeded)
                    failed += int(not succeeded)
                    with self._lock:
                        self._snapshot.update(
                            processed=processed,
                            updated=updated,
                            failed=failed,
                            summary=f"{processed} itens da fila processados.",
                        )
        finally:
            with self._lock:
                self._active = False
                self._snapshot.update(
                    active=False,
                    percent=100,
                    summary=(
                        f"Fila drenada: {updated} concluídos; "
                        f"{failed} mantidos para nova tentativa."
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

    def _run_one(self, task_id: int) -> bool:
        task = db.session.get(AsyncTask, task_id)
        if task is None:
            return True
        try:
            if task.task_type != "watch_update":
                raise ValueError(f"unsupported task type: {task.task_type}")
            self._apply_watch_update(json.loads(task.payload))
            db.session.delete(task)
            db.session.commit()
            return True
        except Exception as error:
            db.session.rollback()
            task = db.session.get(AsyncTask, task_id)
            if task is None:
                return False
            task.attempts += 1
            task.status = "pending"
            task.last_error = f"{type(error).__name__}: {error}"[:1000]
            task.next_attempt_at = datetime.now(UTC) + timedelta(
                seconds=min(3600, 15 * (2 ** min(task.attempts - 1, 8)))
            )
            db.session.commit()
            self._app.logger.warning("asynchronous task retained for retry", exc_info=True)
            return False

    def retry_all(self) -> bool:
        with self._app.app_context():
            db.session.execute(
                update(AsyncTask)
                .where(AsyncTask.status == "pending")
                .values(next_attempt_at=datetime.now(UTC))
            )
            db.session.commit()
        return self.submit(force=True)

    def _apply_watch_update(self, payload: dict[str, object]) -> None:
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
        origin = db.session.get(Source, source_id)
        if origin is None:
            raise LookupError("watch update source is not available")
        reference = db.session.scalar(
            select(SourceMediaRef).where(
                SourceMediaRef.source_id == source_id,
                SourceMediaRef.external_id == external_id,
                SourceMediaRef.available.is_(True),
            )
        )
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
        if len(targets) != 1:
            raise LookupError("exactly one cross-server media reference is required")
        target, source = targets[0]
        state = db.session.scalar(
            select(WatchState).where(
                WatchState.media_item_id == target.media_item_id,
                WatchState.source_id == target.source_id,
            )
        )
        target_watched_at = state.last_watched_at if state is not None else None
        if target_watched_at is not None:
            target_watched_at = (
                target_watched_at.replace(tzinfo=UTC)
                if target_watched_at.tzinfo is None
                else target_watched_at.astimezone(UTC)
            )
        should_apply = (
            target_watched_at != watched_at
            if origin.connector_type.value == "plex"
            else target_watched_at is None or watched_at > target_watched_at
        )
        if not should_apply:
            return
        if state is None or not state.completed or target_watched_at != watched_at:
            connector = connector_for(source)
            try:
                connector.mark_watched(target.external_id, watched_at=watched_at)
            finally:
                close = getattr(connector, "close", None)
                if callable(close):
                    close()
        if state is None:
            state = WatchState(
                media_item_id=target.media_item_id,
                source_id=target.source_id,
                view_count=1,
                completed=True,
                observed_at=watched_at,
            )
            db.session.add(state)
        state.view_count = max(state.view_count, 1)
        state.completed = True
        state.last_watched_at = watched_at
        state.progress_ms = None
        state.observed_at = watched_at
        db.session.commit()


def get_async_task_executor(app: Flask) -> AsyncTaskExecutor:
    concrete = app._get_current_object() if hasattr(app, "_get_current_object") else app
    executor = concrete.extensions.get("euvieouvi.async_tasks")
    if not isinstance(executor, AsyncTaskExecutor):
        executor = AsyncTaskExecutor(concrete)
        concrete.extensions["euvieouvi.async_tasks"] = executor
    return executor


def recover_async_tasks() -> int:
    result = db.session.execute(
        update(AsyncTask).where(AsyncTask.status == "processing").values(status="pending")
    )
    count = int(getattr(result, "rowcount", 0) or 0)
    db.session.commit()
    return int(count)
