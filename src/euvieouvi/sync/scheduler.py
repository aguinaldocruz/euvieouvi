"""Small persistent daily scheduler for the single-process deployment."""

from __future__ import annotations

import contextlib
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from euvieouvi.api.runtime import get_executor
from euvieouvi.database.enums import ConnectorType, SyncTrigger
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
            try:
                _backup_if_due(app)
            except SQLAlchemyError:
                db.session.rollback()
                app.logger.exception("scheduled backup check failed")
            except Exception:
                app.logger.exception("scheduled backup failed")


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
                        "sync.schedule.mode",
                        "sync.schedule.enabled.plex",
                        "sync.schedule.enabled.jellyfin",
                        "sync.schedule.time.plex",
                        "sync.schedule.time.jellyfin",
                        "sync.schedule.last_date.plex",
                        "sync.schedule.last_date.jellyfin",
                    }
                )
            )
        )
    }
    mode = values.get("sync.schedule.mode", "shared")
    if mode not in {"shared", "per_source"}:
        mode = "shared"
    local_now = now or datetime.now(ZoneInfo(app.config["TIMEZONE"]))
    local_date = local_now.date().isoformat()

    # Determine which connector types are due
    due_types: list[ConnectorType] = []
    if mode == "per_source":
        for ctype, key_enabled, key_time, key_last in [
            (
                ConnectorType.PLEX,
                "sync.schedule.enabled.plex",
                "sync.schedule.time.plex",
                "sync.schedule.last_date.plex",
            ),
            (
                ConnectorType.JELLYFIN,
                "sync.schedule.enabled.jellyfin",
                "sync.schedule.time.jellyfin",
                "sync.schedule.last_date.jellyfin",
            ),
        ]:
            if values.get(key_enabled, "false") != "true":
                continue
            t = values.get(key_time, values.get("sync.schedule.time", "03:00"))
            try:
                hour, minute = (int(p) for p in t.split(":"))
            except (TypeError, ValueError):
                app.logger.error("invalid persisted synchronization schedule for %s", ctype.value)
                continue
            if (local_now.hour, local_now.minute) < (hour, minute):
                continue
            if values.get(key_last) == local_date:
                continue
            due_types.append(ctype)
    else:
        # shared mode: legacy single time, but respect per-source enabled flags if present
        has_per = (
            "sync.schedule.enabled.plex" in values or "sync.schedule.enabled.jellyfin" in values
        )
        if has_per:
            any_enabled = (
                values.get("sync.schedule.enabled.plex", "false") == "true"
                or values.get("sync.schedule.enabled.jellyfin", "false") == "true"
            )
            if not any_enabled:
                return False
        else:
            if values.get("sync.schedule.enabled", "false") != "true":
                return False
        scheduled_time = values.get("sync.schedule.time", "03:00")
        try:
            hour, minute = (int(part) for part in scheduled_time.split(":"))
        except (TypeError, ValueError):
            app.logger.error("invalid persisted synchronization schedule")
            return False
        if (local_now.hour, local_now.minute) < (hour, minute):
            return False
        if values.get("sync.schedule.last_date") == local_date:
            return False
        # shared mode: queue enabled sources (per-source flags or legacy)
        if has_per:
            for ctype, key_enabled in [
                (ConnectorType.PLEX, "sync.schedule.enabled.plex"),
                (ConnectorType.JELLYFIN, "sync.schedule.enabled.jellyfin"),
            ]:
                if values.get(key_enabled, "false") == "true":
                    due_types.append(ctype)
        else:
            due_types = [ConnectorType.PLEX, ConnectorType.JELLYFIN]

    if not due_types:
        return False

    # Resolve source_ids for due types that are actually configured and enabled
    source_ids: list[int] = []
    for ctype in due_types:
        ids = db.session.scalars(
            select(Source.id)
            .where(Source.connector_type == ctype, Source.enabled.is_(True))
            .order_by(Source.id)
        ).all()
        source_ids.extend(ids)

    if not source_ids:
        return False

    try:
        executor = get_executor(app)
        submit_all = getattr(executor, "submit_all", None)
        if callable(submit_all):
            submit_all(tuple(source_ids), trigger=SyncTrigger.SCHEDULED)
        else:
            executor.submit(source_ids[0], trigger=SyncTrigger.SCHEDULED)
    except (SyncAlreadyRunningError, SyncSourceUnavailableError):
        return False

    # mark last_date per type
    if mode == "per_source":
        for ctype in due_types:
            key_last = (
                "sync.schedule.last_date.plex"
                if ctype == ConnectorType.PLEX
                else "sync.schedule.last_date.jellyfin"
            )
            s = db.session.get(Setting, key_last)
            if s is None:
                db.session.add(Setting(key=key_last, value=local_date))
            else:
                s.value = local_date
    else:
        s = db.session.get(Setting, "sync.schedule.last_date")
        if s is None:
            db.session.add(Setting(key="sync.schedule.last_date", value=local_date))
        else:
            s.value = local_date
        # also update per-source last_dates for consistency
        for key_last in ("sync.schedule.last_date.plex", "sync.schedule.last_date.jellyfin"):
            s2 = db.session.get(Setting, key_last)
            if s2 is None:
                db.session.add(Setting(key=key_last, value=local_date))
            else:
                s2.value = local_date
    db.session.commit()
    app.logger.info(
        "daily synchronization queued",
        extra={
            "source_ids": tuple(source_ids),
            "mode": mode,
            "due_types": [t.value for t in due_types],
        },
    )
    return True


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
