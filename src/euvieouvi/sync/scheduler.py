"""Small persistent daily scheduler for the single-process deployment."""

from __future__ import annotations

import contextlib
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from euvieouvi.database.enums import SyncTrigger
from euvieouvi.database.models import Setting
from euvieouvi.extensions import db
from euvieouvi.sync.errors import SyncAlreadyRunningError, SyncSourceUnavailableError

_POLL_SECONDS = 30


def start_scheduler(app: Flask) -> None:
    """Start one daemon scheduler for the supported one-worker deployment."""
    if app.config.get("TESTING") or "euvieouvi.scheduler" in app.extensions:
        return
    stop = threading.Event()
    thread = threading.Thread(
        target=_scheduler_loop,
        args=(app, stop),
        name="euvieouvi-scheduler",
        daemon=True,
    )
    app.extensions["euvieouvi.scheduler"] = (thread, stop)
    thread.start()


def _scheduler_loop(app: Flask, stop: threading.Event) -> None:
    with app.app_context():
        from euvieouvi.sync.async_tasks import recover_async_tasks
        from euvieouvi.sync.jobs import reconcile_interrupted_job_runs

        interrupted = reconcile_interrupted_job_runs()
        recovered_tasks = recover_async_tasks()
        if interrupted:
            app.logger.warning(
                "reconciled interrupted job runs", extra={"count": interrupted}
            )
        if recovered_tasks:
            app.logger.warning("recovered interrupted asynchronous tasks")
    while not stop.wait(_POLL_SECONDS):
        with app.app_context():
            try:
                _run_jobs_if_due(app)
            except SQLAlchemyError:
                db.session.rollback()
                app.logger.exception("scheduled jobs check failed")
            try:
                from euvieouvi.sync.async_tasks import get_async_task_executor

                get_async_task_executor(app).submit()
            except SQLAlchemyError:
                db.session.rollback()
                app.logger.exception("asynchronous task queue check failed")
            try:
                _backup_if_due(app)
            except SQLAlchemyError:
                db.session.rollback()
                app.logger.exception("scheduled backup check failed")
            except Exception:
                app.logger.exception("scheduled backup failed")


def _run_jobs_if_due(app: Flask, *, now: datetime | None = None) -> bool:
    """Queue each independently configured operational job once per local day."""
    from euvieouvi.sync.jobs import JOBS, setting_key, submit_job

    keys = {
        setting_key(job.id, field) for job in JOBS for field in ("enabled", "time", "last_date")
    }
    values = {
        item.key: item.value
        for item in db.session.scalars(select(Setting).where(Setting.key.in_(keys)))
    }
    local_now = now or datetime.now(ZoneInfo(app.config["TIMEZONE"]))
    local_date = local_now.date().isoformat()
    queued = False
    for job in JOBS:
        if values.get(setting_key(job.id, "enabled"), "false") != "true":
            continue
        try:
            hour, minute = (
                int(part)
                for part in values.get(setting_key(job.id, "time"), job.default_time).split(":")
            )
        except (TypeError, ValueError):
            app.logger.error("invalid schedule for job %s", job.id)
            continue
        if (local_now.hour, local_now.minute) < (hour, minute):
            continue
        last_key = setting_key(job.id, "last_date")
        if values.get(last_key) == local_date:
            continue
        try:
            started = submit_job(app, job.id, trigger=SyncTrigger.SCHEDULED)
        except (SyncAlreadyRunningError, SyncSourceUnavailableError):
            continue
        if not started:
            continue
        setting = db.session.get(Setting, last_key)
        if setting is None:
            db.session.add(Setting(key=last_key, value=local_date))
        else:
            setting.value = local_date
        db.session.commit()
        queued = True
    return queued


def _backup_if_due(app: Flask, *, now: datetime | None = None) -> bool:
    values = {
        item.key: item.value
        for item in db.session.scalars(
            select(Setting).where(
                Setting.key.in_(
                    {
                        "backup.schedule.enabled",
                        "backup.schedule.time",
                        "backup.schedule.last_date",
                        "backup.retention.keep_last",
                    }
                )
            )
        )
    }
    if values.get("backup.schedule.enabled", "false") != "true":
        return False
    t = values.get("backup.schedule.time", "04:00")
    try:
        hour, minute = (int(p) for p in t.split(":"))
    except (TypeError, ValueError):
        app.logger.error("invalid backup schedule")
        return False
    local_now = now or datetime.now(ZoneInfo(app.config["TIMEZONE"]))
    local_date = local_now.date().isoformat()
    if (local_now.hour, local_now.minute) < (hour, minute):
        return False
    if values.get("backup.schedule.last_date") == local_date:
        return False
    # perform backup
    from pathlib import Path as _Path

    from euvieouvi.database.backup import backup_database

    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    src = _Path(app.instance_path) / "euvieouvi.db"
    if uri.startswith("sqlite:///"):
        with contextlib.suppress(Exception):
            src = _Path(uri.removeprefix("sqlite:///"))
    if not src.is_file():
        return False
    dest_dir = _Path(app.instance_path) / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"euvieouvi-{local_now.strftime('%Y%m%d-%H%M%S')}.db"
    try:
        backup_database(src, dest)
    except Exception:
        app.logger.exception("backup failed")
        return False
    # retention
    keep_raw = values.get("backup.retention.keep_last", "15")
    try:
        keep = int(keep_raw)
    except ValueError:
        keep = 15
    # prune oldest beyond keep
    files = sorted(dest_dir.glob("euvieouvi-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        try:
            p.unlink()
        except OSError:
            continue
    s = db.session.get(Setting, "backup.schedule.last_date")
    if s is None:
        db.session.add(Setting(key="backup.schedule.last_date", value=local_date))
    else:
        s.value = local_date
    db.session.commit()
    app.logger.info("daily backup created", extra={"file": str(dest)})
    return True
