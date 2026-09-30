"""Database-backed worker for durable operational jobs."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

from flask import Flask
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import OperationalError
from werkzeug.local import LocalProxy

from euvieouvi.database.enums import SyncStatus
from euvieouvi.database.models import JobRun
from euvieouvi.database.sqlite_runtime import is_sqlite_lock_error
from euvieouvi.extensions import db
from euvieouvi.logging import concise_error

LEASE_SECONDS = 45
_JOB_PRIORITY = {
    "sync_plex": 10,
    "watched_plex_to_jellyfin": 20,
    "watched_jellyfin_to_plex": 30,
    "sync_jellyfin": 40,
    "metadata": 50,
    "catalog_images": 60,
    "catalog_reconcile": 70,
    "async_tasks": 80,
    "maintenance": 90,
}
_JOB_DEPENDENCIES = {
    "watched_plex_to_jellyfin": ("sync_plex",),
    "watched_jellyfin_to_plex": ("sync_plex", "watched_plex_to_jellyfin"),
    "sync_jellyfin": (
        "sync_plex",
        "watched_plex_to_jellyfin",
        "watched_jellyfin_to_plex",
    ),
}


class DurableJobWorker:
    """Claim and execute queued rows without relying on thread-local state."""

    def __init__(self, app: Flask) -> None:
        self._app = app._get_current_object() if isinstance(app, LocalProxy) else app
        self.worker_id = f"{uuid.uuid4().hex}:{threading.get_native_id()}"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._wake_pending = False
        self._wake_event = threading.Event()

    def wake(self) -> None:
        """Start a drain pass, coalescing concurrent wake-up requests."""
        with self._lock:
            self._wake_pending = True
            self._wake_event.set()
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._drain,
                name="euvieouvi-durable-jobs",
                daemon=True,
            )
            self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                with self._lock:
                    self._wake_pending = False
                try:
                    with self._app.app_context():
                        recover_job_runs(force=False)
                        run_id = claim_next_job(self.worker_id)
                except OperationalError as error:
                    if not is_sqlite_lock_error(error):
                        raise
                    self._wake_event.wait(1)
                    continue
                if run_id is not None:
                    self._execute(run_id)
                    continue
                with self._app.app_context():
                    retry_delay = seconds_until_next_job()
                if retry_delay is not None:
                    with self._lock:
                        if self._wake_pending:
                            continue
                        self._wake_event.clear()
                    self._wake_event.wait(min(30.0, max(0.05, retry_delay)))
                    continue
                with self._lock:
                    if self._wake_pending:
                        continue
                    self._thread = None
                    return
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _execute(self, run_id: int) -> None:
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(run_id, heartbeat_stop),
            name=f"euvieouvi-job-heartbeat-{run_id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            from euvieouvi.sync.jobs import execute_claimed_job

            execute_claimed_job(self._app, run_id, self.worker_id)
            with self._app.app_context():
                run = db.session.get(JobRun, run_id)
                if (
                    run is not None
                    and run.status is SyncStatus.RUNNING
                    and run.lease_owner == self.worker_id
                ):
                    raise RuntimeError("O executor terminou sem finalizar o job persistente.")
        except Exception as error:
            self._app.logger.exception("durable job execution failed", extra={"job_run_id": run_id})
            with self._app.app_context():
                fail_or_retry_job(self._app, run_id, self.worker_id, error)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)

    def _heartbeat(self, run_id: int, stop: threading.Event) -> None:
        while not stop.wait(10):
            try:
                with self._app.app_context():
                    renew_job_lease(run_id, self.worker_id)
            except OperationalError as error:
                if is_sqlite_lock_error(error):
                    continue
                self._app.logger.exception(
                    "durable job heartbeat failed", extra={"job_run_id": run_id}
                )
            except Exception:
                self._app.logger.exception(
                    "durable job heartbeat failed", extra={"job_run_id": run_id}
                )


def get_job_worker(app: Flask) -> DurableJobWorker:
    concrete = app._get_current_object() if isinstance(app, LocalProxy) else app
    worker = concrete.extensions.get("euvieouvi.job_worker")
    if not isinstance(worker, DurableJobWorker):
        worker = DurableJobWorker(concrete)
        concrete.extensions["euvieouvi.job_worker"] = worker
    return worker


def wake_job_worker(app: Flask) -> None:
    get_job_worker(app).wake()


def claim_next_job(worker_id: str, *, now: datetime | None = None) -> int | None:
    """Atomically move the next available row from queued to running."""
    claimed_at = now or datetime.now(UTC)
    priority = case(_JOB_PRIORITY, value=JobRun.job_id, else_=999)
    candidates = list(
        db.session.scalars(
            select(JobRun)
            .where(
                JobRun.status == SyncStatus.QUEUED,
                or_(JobRun.available_at.is_(None), JobRun.available_at <= claimed_at),
            )
            .order_by(priority, JobRun.available_at, JobRun.id)
        )
    )
    queued = list(
        db.session.scalars(
            select(JobRun).where(JobRun.status == SyncStatus.QUEUED).order_by(JobRun.id)
        )
    )
    candidate = next(
        (
            item
            for item in candidates
            if not any(
                dependency.job_id in _JOB_DEPENDENCIES.get(item.job_id, ())
                and dependency.id < item.id
                for dependency in queued
            )
        ),
        None,
    )
    if candidate is None:
        db.session.rollback()
        return None
    no_running_job = ~select(JobRun.id).where(JobRun.status == SyncStatus.RUNNING).exists()
    result = db.session.execute(
        update(JobRun)
        .where(
            JobRun.id == candidate.id,
            JobRun.status == SyncStatus.QUEUED,
            no_running_job,
        )
        .values(
            status=SyncStatus.RUNNING,
            started_at=claimed_at,
            finished_at=None,
            heartbeat_at=claimed_at,
            lease_expires_at=claimed_at + timedelta(seconds=LEASE_SECONDS),
            lease_owner=worker_id,
            attempt_count=JobRun.attempt_count + 1,
            progress_percent=0,
            summary="Execução reivindicada pelo worker persistente.",
        )
    )
    db.session.commit()
    return int(candidate.id) if int(getattr(result, "rowcount", 0) or 0) else None


def seconds_until_next_job(*, now: datetime | None = None) -> float | None:
    if (
        db.session.scalar(select(JobRun.id).where(JobRun.status == SyncStatus.RUNNING).limit(1))
        is not None
    ):
        db.session.rollback()
        return 1.0
    available_at = db.session.scalar(
        select(func.min(JobRun.available_at)).where(JobRun.status == SyncStatus.QUEUED)
    )
    db.session.rollback()
    if available_at is None:
        return None
    current = now or datetime.now(UTC)
    if available_at.tzinfo is None:
        available_at = available_at.replace(tzinfo=UTC)
    return max(0.5, (available_at - current).total_seconds())


def fail_or_retry_job(
    app: Flask,
    run_id: int,
    worker_id: str,
    error: Exception,
    *,
    now: datetime | None = None,
) -> None:
    """Persist a dispatch/worker failure and retry it with bounded backoff."""
    from euvieouvi.sync.jobs import _write_log

    failed_at = now or datetime.now(UTC)
    run = db.session.get(JobRun, run_id)
    if run is None or run.lease_owner != worker_id:
        return
    detail = concise_error(error)
    if run.cancel_requested_at is not None:
        run.status = SyncStatus.INTERRUPTED
        run.finished_at = failed_at
        run.progress_percent = 100
        run.summary = "Execução cancelada; dados já confirmados foram preservados."
    elif run.attempt_count < run.max_attempts:
        delay = min(300, 5 * (2 ** max(0, run.attempt_count - 1)))
        run.status = SyncStatus.QUEUED
        run.available_at = failed_at + timedelta(seconds=delay)
        run.started_at = None
        run.finished_at = None
        run.execution_reference = None
        run.summary = (
            f"Tentativa {run.attempt_count} falhou ({detail}); nova tentativa em {delay}s."
        )
    else:
        run.status = SyncStatus.FAILED
        run.finished_at = failed_at
        run.progress_percent = 100
        run.failed = max(1, run.failed)
        run.summary = f"Falha após {run.attempt_count} tentativas: {detail}"
    run.lease_owner = None
    run.lease_expires_at = None
    run.heartbeat_at = failed_at
    db.session.commit()
    _write_log(
        app,
        run.log_filename or "",
        f"ERRO · operação=executar job · tentativa={run.attempt_count} · erro={detail}",
    )


def renew_job_lease(run_id: int, worker_id: str, *, now: datetime | None = None) -> bool:
    heartbeat_at = now or datetime.now(UTC)
    result = db.session.execute(
        update(JobRun)
        .where(
            JobRun.id == run_id,
            JobRun.status == SyncStatus.RUNNING,
            JobRun.lease_owner == worker_id,
        )
        .values(
            heartbeat_at=heartbeat_at,
            lease_expires_at=heartbeat_at + timedelta(seconds=LEASE_SECONDS),
        )
    )
    db.session.commit()
    return bool(int(getattr(result, "rowcount", 0) or 0))


def recover_job_runs(*, force: bool = False, now: datetime | None = None) -> int:
    """Requeue work whose owning process no longer has a valid lease."""
    recovered_at = now or datetime.now(UTC)
    conditions = [JobRun.status == SyncStatus.RUNNING]
    if not force:
        conditions.append(
            or_(JobRun.lease_expires_at.is_(None), JobRun.lease_expires_at < recovered_at)
        )
    runs = list(db.session.scalars(select(JobRun).where(*conditions).order_by(JobRun.id)))
    queued = list(
        db.session.scalars(
            select(JobRun).where(
                JobRun.status == SyncStatus.QUEUED,
                JobRun.lease_owner.is_not(None),
            )
        )
    )
    for run in [*runs, *queued]:
        if run.cancel_requested_at is not None:
            run.status = SyncStatus.INTERRUPTED
            run.finished_at = recovered_at
            run.progress_percent = 100
            run.summary = "Cancelamento confirmado durante a recuperação do worker."
        elif run.attempt_count >= run.max_attempts:
            run.status = SyncStatus.FAILED
            run.finished_at = recovered_at
            run.progress_percent = 100
            run.failed = max(1, run.failed)
            run.summary = "Limite de tentativas atingido durante a recuperação do worker."
        else:
            run.status = SyncStatus.QUEUED
            run.available_at = recovered_at
            run.started_at = None
            run.finished_at = None
            run.execution_reference = None
            run.summary = "Execução recuperada após reinício; será retomada pelo checkpoint."
        run.lease_owner = None
        run.lease_expires_at = None
        run.heartbeat_at = recovered_at
    if runs or queued:
        db.session.commit()
    return len(runs) + len(queued)
