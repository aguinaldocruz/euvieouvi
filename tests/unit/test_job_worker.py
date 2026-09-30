"""Durable operational job claiming, recovery and cancellation."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from euvieouvi.database.enums import ConnectorType, SyncStatus, SyncTrigger
from euvieouvi.database.models import JobRun, Source, SyncRun
from euvieouvi.extensions import db
from euvieouvi.sync import jobs
from euvieouvi.sync.job_worker import (
    claim_next_job,
    fail_or_retry_job,
    recover_job_runs,
)
from euvieouvi.sync.jobs import (
    execute_claimed_job,
    reconcile_interrupted_job_runs,
    request_job_cancellation,
)

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)


def _run(job_id: str, status: SyncStatus = SyncStatus.QUEUED) -> JobRun:
    return JobRun(
        job_id=job_id,
        trigger=SyncTrigger.MANUAL,
        status=status,
        available_at=NOW,
        progress_percent=0,
    )


def test_claim_is_atomic_and_honors_media_pipeline_priority(app: Flask) -> None:
    with app.app_context():
        jellyfin = _run("sync_jellyfin")
        plex = _run("sync_plex")
        db.session.add_all([jellyfin, plex])
        db.session.commit()

        assert claim_next_job("worker-a", now=NOW) == plex.id
        db.session.refresh(plex)
        assert plex.status is SyncStatus.RUNNING
        assert plex.lease_owner == "worker-a"
        assert plex.attempt_count == 1

        assert claim_next_job("worker-b", now=NOW) is None
        plex.status = SyncStatus.SUCCEEDED
        plex.finished_at = NOW
        plex.lease_owner = None
        db.session.commit()
        assert claim_next_job("worker-b", now=NOW) == jellyfin.id


def test_restart_requeues_a_leased_attempt_without_losing_progress(app: Flask) -> None:
    with app.app_context():
        run = _run("sync_plex", SyncStatus.RUNNING)
        run.started_at = NOW - timedelta(minutes=5)
        run.heartbeat_at = NOW - timedelta(minutes=1)
        run.lease_expires_at = NOW + timedelta(minutes=1)
        run.lease_owner = "dead-worker"
        run.execution_reference = "sync:41"
        run.attempt_count = 1
        run.processed = 1500
        db.session.add(run)
        db.session.commit()

        assert recover_job_runs(force=True, now=NOW) == 1
        recovered = db.session.scalar(
            select(JobRun).where(JobRun.id == run.id).execution_options(populate_existing=True)
        )
        assert recovered is not None
        assert recovered.available_at == NOW.replace(tzinfo=None)
        assert recovered.execution_reference is None
        assert recovered.lease_owner is None
        assert recovered.processed == 1500
        assert "checkpoint" in (recovered.summary or "")
        assert recovered.status is SyncStatus.QUEUED


def test_queued_cancellation_is_terminal_and_persistent(app: Flask) -> None:
    with app.app_context():
        run = _run("metadata")
        db.session.add(run)
        db.session.commit()

        assert request_job_cancellation(app, run.id) is True
        db.session.refresh(run)
        assert run.status is SyncStatus.INTERRUPTED
        assert run.cancel_requested_at is not None
        assert run.finished_at is not None


def test_worker_failure_uses_bounded_persistent_backoff(app: Flask) -> None:
    with app.app_context():
        run = _run("metadata", SyncStatus.RUNNING)
        run.lease_owner = "worker-a"
        run.attempt_count = 1
        run.max_attempts = 3
        run.log_filename = "retry-test.log"
        db.session.add(run)
        db.session.commit()

        fail_or_retry_job(app, run.id, "worker-a", OSError("temporary"), now=NOW)
        retried = db.session.scalar(
            select(JobRun).where(JobRun.id == run.id).execution_options(populate_existing=True)
        )
        assert retried is not None
        assert retried.available_at == (NOW + timedelta(seconds=5)).replace(tzinfo=None)
        assert retried.lease_owner is None
        assert "OSError: temporary" in (retried.summary or "")
        assert retried.status is SyncStatus.QUEUED


def test_claimed_sync_reloads_job_after_dispatch_session_closes(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        run = _run("sync_plex", SyncStatus.RUNNING)
        run.lease_owner = "worker-a"
        run.attempt_count = 1
        run.log_filename = "detached-session.log"
        db.session.add(run)
        db.session.commit()
        run_id = run.id

    monitored: list[tuple[int, str, int | str, str]] = []

    def dispatch(app: Flask, job_id: str, *, trigger: SyncTrigger) -> int:
        del app, job_id, trigger
        db.session.close()
        return 42

    def monitor(
        app: Flask,
        job_run_id: int,
        job_id: str,
        reference: int | str,
        *,
        worker_id: str,
    ) -> None:
        del app
        monitored.append((job_run_id, job_id, reference, worker_id))

    monkeypatch.setattr(jobs, "_dispatch_job", dispatch)
    monkeypatch.setattr(jobs, "_monitor_job", monitor)

    execute_claimed_job(app, run_id, "worker-a")

    with app.app_context():
        persisted = db.session.get(JobRun, run_id)
        assert persisted is not None
        assert persisted.execution_reference == "sync:42"
        assert persisted.status is SyncStatus.RUNNING
    assert monitored == [(run_id, "sync_plex", 42, "worker-a")]


def test_monitored_job_survives_writer_lock_while_sync_is_running(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        source = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex:32400",
            secret="secret",
            enabled=True,
        )
        job = _run("sync_plex", SyncStatus.RUNNING)
        job.lease_owner = "worker-a"
        job.log_filename = "queued-sync.log"
        db.session.add_all([source, job])
        db.session.flush()
        sync = SyncRun(
            source_id=source.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.QUEUED,
            summary="Sincronização aguardando início.",
        )
        db.session.add(sync)
        db.session.commit()
        job_id = job.id
        sync_id = sync.id

    observed: list[SyncStatus] = []
    real_commit = db.session.commit
    commit_calls = 0

    def transiently_locked_commit() -> None:
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            raise OperationalError(
                "UPDATE job_runs",
                {},
                sqlite3.OperationalError("database is locked"),
            )
        real_commit()

    def finish_sync(_: float) -> None:
        persisted_job = db.session.get(JobRun, job_id)
        persisted_sync = db.session.get(SyncRun, sync_id)
        assert persisted_job is not None and persisted_sync is not None
        observed.append(persisted_job.status)
        persisted_sync.status = SyncStatus.SUCCEEDED
        persisted_sync.finished_at = NOW
        db.session.commit()

    monkeypatch.setattr("euvieouvi.sync.jobs.time.sleep", finish_sync)
    monkeypatch.setattr(db.session, "commit", transiently_locked_commit)
    jobs._monitor_job(app, job_id, "sync_plex", sync_id, worker_id="worker-a")

    with app.app_context():
        persisted = db.session.get(JobRun, job_id)
        assert persisted is not None
        assert persisted.status is SyncStatus.SUCCEEDED
        assert persisted.execution_reference is None
    assert observed == [SyncStatus.RUNNING]
    assert commit_calls >= 2


def test_startup_interrupts_low_level_sync_and_requeues_its_job(app: Flask) -> None:
    with app.app_context():
        source = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex:32400",
            secret="secret",
            enabled=True,
        )
        job = _run("sync_plex", SyncStatus.RUNNING)
        job.lease_owner = "previous-process"
        job.lease_expires_at = NOW + timedelta(minutes=1)
        job.attempt_count = 1
        db.session.add_all([source, job])
        db.session.flush()
        sync = SyncRun(
            source_id=source.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.RUNNING,
        )
        db.session.add(sync)
        db.session.flush()
        job.execution_reference = f"sync:{sync.id}"
        db.session.commit()

        assert reconcile_interrupted_job_runs() == 1
        db.session.refresh(sync)
        db.session.refresh(job)
        assert sync.status is SyncStatus.INTERRUPTED
        assert job.status is SyncStatus.QUEUED
        assert job.execution_reference is None
