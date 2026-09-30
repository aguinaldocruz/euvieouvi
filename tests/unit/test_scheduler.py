"""Persistent daily scheduler behavior."""

import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask
from pytest import MonkeyPatch

from euvieouvi.database.enums import SyncStatus, SyncTrigger
from euvieouvi.database.models import JobRun, Setting
from euvieouvi.extensions import db
from euvieouvi.sync import scheduler


class _SingleIterationEvent(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.wait_count = 0

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        self.wait_count += 1
        return self.wait_count > 1


def test_independent_job_schedule_runs_once_per_day(app: Flask, monkeypatch: MonkeyPatch) -> None:
    calls: list[tuple[str, SyncTrigger]] = []

    def submit(_app: Flask, job_id: str, *, trigger: SyncTrigger) -> bool:
        calls.append((job_id, trigger))
        return True

    monkeypatch.setattr("euvieouvi.sync.jobs.submit_job", submit)
    with app.app_context():
        db.session.add_all(
            [
                Setting(key="jobs.metadata.enabled", value="true"),
                Setting(key="jobs.metadata.time", value="04:00"),
            ]
        )
        db.session.commit()
        now = datetime(2026, 8, 5, 4, 1, tzinfo=ZoneInfo("America/Sao_Paulo"))

        assert scheduler._run_jobs_if_due(app, now=now) is True
        assert scheduler._run_jobs_if_due(app, now=now) is False
        assert calls == [("metadata", SyncTrigger.SCHEDULED)]
        assert db.session.get(Setting, "jobs.metadata.last_date").value == "2026-08-05"  # type: ignore[union-attr]


def test_due_schedule_is_consumed_when_manual_job_is_already_active(app: Flask) -> None:
    with app.app_context():
        db.session.add_all(
            [
                Setting(key="jobs.metadata.enabled", value="true"),
                Setting(key="jobs.metadata.time", value="04:00"),
                JobRun(
                    job_id="metadata",
                    trigger=SyncTrigger.MANUAL,
                    status=SyncStatus.RUNNING,
                    progress_percent=25,
                ),
            ]
        )
        db.session.commit()
        now = datetime(2026, 8, 5, 4, 1, tzinfo=ZoneInfo("America/Sao_Paulo"))

        assert scheduler._run_jobs_if_due(app, now=now) is False
        handled = db.session.get(Setting, "jobs.metadata.last_date")
        assert handled is not None and handled.value == "2026-08-05"


def test_scheduled_backup_runs_once_and_applies_retention(app: Flask) -> None:
    with app.app_context():
        db.session.add_all(
            [
                Setting(key="backup.schedule.enabled", value="true"),
                Setting(key="backup.schedule.time", value="04:00"),
                Setting(key="backup.retention.keep_last", value="1"),
            ]
        )
        db.session.commit()
        backup_dir = Path(app.instance_path) / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        old_backup = backup_dir / "euvieouvi-20260804-040000.db"
        old_backup.write_bytes(b"old")
        now = datetime(2026, 8, 5, 4, 1, tzinfo=ZoneInfo("America/Sao_Paulo"))

        assert scheduler._backup_if_due(app, now=now) is True
        files = list(backup_dir.glob("euvieouvi-*.db"))
        assert len(files) == 1
        assert files[0].name == "euvieouvi-20260805-040100.db"
        assert scheduler._backup_if_due(app, now=now) is False
        last_date = db.session.get(Setting, "backup.schedule.last_date")
        assert last_date is not None and last_date.value == "2026-08-05"


def test_scheduled_backup_handles_invalid_time_and_copy_failure(
    app: Flask, monkeypatch: MonkeyPatch
) -> None:
    now = datetime(2026, 8, 5, 4, 1, tzinfo=ZoneInfo("America/Sao_Paulo"))
    with app.app_context():
        db.session.add_all(
            [
                Setting(key="backup.schedule.enabled", value="true"),
                Setting(key="backup.schedule.time", value="invalid"),
            ]
        )
        db.session.commit()
        assert scheduler._backup_if_due(app, now=now) is False

        db.session.get(Setting, "backup.schedule.time").value = "04:00"  # type: ignore[union-attr]
        db.session.commit()

        def fail_backup(_source: Path, _destination: Path) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("euvieouvi.database.backup.backup_database", fail_backup)
        assert scheduler._backup_if_due(app, now=now) is False
        assert db.session.get(Setting, "backup.schedule.last_date") is None


def test_scheduler_loop_recovers_state_and_runs_all_checks_once(
    app: Flask, monkeypatch: MonkeyPatch
) -> None:
    calls: list[str] = []

    class Executor:
        def submit(self) -> bool:
            calls.append("queue")
            return True

    def recovered(name: str) -> int:
        calls.append(name)
        return 1

    def checked(name: str) -> bool:
        calls.append(name)
        return False

    monkeypatch.setattr(
        "euvieouvi.sync.jobs.reconcile_interrupted_job_runs",
        lambda: recovered("jobs-recovered"),
    )
    monkeypatch.setattr(
        "euvieouvi.sync.async_tasks.recover_async_tasks",
        lambda: recovered("tasks-recovered"),
    )
    monkeypatch.setattr(
        "euvieouvi.sync.async_tasks.get_async_task_executor", lambda _app: Executor()
    )
    monkeypatch.setattr(scheduler, "_run_jobs_if_due", lambda _app: checked("jobs"))
    monkeypatch.setattr(scheduler, "_backup_if_due", lambda _app: checked("backup"))
    monkeypatch.setattr(scheduler, "_record_operational_sample", lambda: checked("sample"))

    scheduler._scheduler_loop(app, _SingleIterationEvent())

    assert calls == [
        "jobs-recovered",
        "tasks-recovered",
        "sample",
        "jobs",
        "queue",
        "backup",
        "sample",
    ]


def test_scheduler_loop_survives_unexpected_failures(app: Flask, monkeypatch: MonkeyPatch) -> None:
    calls: list[str] = []

    def fail(name: str) -> bool:
        calls.append(name)
        raise RuntimeError(name)

    monkeypatch.setattr(
        "euvieouvi.sync.jobs.reconcile_interrupted_job_runs", lambda: fail("recover-jobs")
    )
    monkeypatch.setattr(
        "euvieouvi.sync.async_tasks.recover_async_tasks", lambda: fail("recover-tasks")
    )
    monkeypatch.setattr(scheduler, "_run_jobs_if_due", lambda _app: fail("jobs"))
    monkeypatch.setattr(
        "euvieouvi.sync.async_tasks.get_async_task_executor", lambda _app: fail("queue")
    )
    monkeypatch.setattr(scheduler, "_backup_if_due", lambda _app: fail("backup"))
    monkeypatch.setattr(scheduler, "_record_operational_sample", lambda: fail("sample"))

    scheduler._scheduler_loop(app, _SingleIterationEvent())

    assert calls == [
        "recover-jobs",
        "recover-tasks",
        "sample",
        "jobs",
        "queue",
        "backup",
        "sample",
    ]
