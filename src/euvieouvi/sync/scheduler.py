"""Small persistent daily scheduler for the single-process deployment."""

from __future__ import annotations

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from euvieouvi.api.runtime import get_executor
from euvieouvi.database.enums import SyncTrigger
from euvieouvi.database.models import Setting, Source
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
    while not stop.wait(_POLL_SECONDS):
        with app.app_context():
            try:
                _run_if_due(app)
            except SQLAlchemyError:
                db.session.rollback()
                app.logger.exception("scheduled synchronization check failed")


def _run_if_due(app: Flask, *, now: datetime | None = None) -> bool:
    values = {
        item.key: item.value
        for item in db.session.scalars(
            select(Setting).where(
                Setting.key.in_(
                    {
                        "sync.schedule.enabled",
                        "sync.schedule.time",
                        "sync.schedule.last_date",
                    }
                )
            )
        )
    }
    if values.get("sync.schedule.enabled", "false") != "true":
        return False
    scheduled_time = values.get("sync.schedule.time", "03:00")
    try:
        hour, minute = (int(part) for part in scheduled_time.split(":"))
    except (TypeError, ValueError):
        app.logger.error("invalid persisted synchronization schedule")
        return False
    local_now = now or datetime.now(ZoneInfo(app.config["TIMEZONE"]))
    local_date = local_now.date().isoformat()
    if (local_now.hour, local_now.minute) < (hour, minute):
        return False
    if values.get("sync.schedule.last_date") == local_date:
        return False
    source_ids = tuple(
        db.session.scalars(
            select(Source.id).where(Source.enabled.is_(True)).order_by(Source.id)
        ).all()
    )
    if not source_ids:
        return False
    try:
        executor = get_executor(app)
        submit_all = getattr(executor, "submit_all", None)
        if callable(submit_all):
            submit_all(source_ids, trigger=SyncTrigger.SCHEDULED)
        else:
            executor.submit(source_ids[0], trigger=SyncTrigger.SCHEDULED)
    except (SyncAlreadyRunningError, SyncSourceUnavailableError):
        return False
    setting = db.session.get(Setting, "sync.schedule.last_date")
    if setting is None:
        db.session.add(Setting(key="sync.schedule.last_date", value=local_date))
    else:
        setting.value = local_date
    db.session.commit()
    app.logger.info("daily synchronization queued", extra={"source_ids": source_ids})
    return True
