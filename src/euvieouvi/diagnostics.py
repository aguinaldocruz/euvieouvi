"""Build a bounded, secret-free operational diagnostic archive."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from flask import Flask
from sqlalchemy import func, select, text

from euvieouvi.database.models import (
    AsyncTask,
    JobRun,
    Library,
    MediaItem,
    OperationalSample,
    Source,
    SourceMediaRef,
    SyncError,
    SyncRun,
    WatchEvent,
)
from euvieouvi.extensions import db
from euvieouvi.logging import redact_text
from euvieouvi.observability import operational_snapshot

_MAX_LOGS = 20
_MAX_LOG_BYTES = 256 * 1024


def build_diagnostic_archive(app: Flask) -> io.BytesIO:
    generated_at = datetime.now(UTC)
    database = Path(app.instance_path) / "euvieouvi.db"
    revision = db.session.scalar(text("SELECT version_num FROM alembic_version"))
    sources = list(db.session.scalars(select(Source).order_by(Source.id)))
    runs = list(db.session.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(100)))
    sync_errors = list(
        db.session.scalars(select(SyncError).order_by(SyncError.id.desc()).limit(200))
    )
    tasks = list(db.session.scalars(select(AsyncTask).order_by(AsyncTask.id.desc()).limit(100)))
    samples = list(
        db.session.scalars(
            select(OperationalSample).order_by(OperationalSample.id.desc()).limit(288)
        )
    )
    summary = {
        "generated_at": generated_at.isoformat(),
        "version": version("euvieouvi"),
        "schema_revision": revision,
        "database_bytes": database.stat().st_size if database.is_file() else None,
        "counts": {
            "sources": _count(Source),
            "libraries": _count(Library),
            "media_items": _count(MediaItem),
            "source_media_refs": _count(SourceMediaRef),
            "watch_events": _count(WatchEvent),
            "sync_runs": _count(SyncRun),
            "job_runs": _count(JobRun),
            "async_tasks": _count(AsyncTask),
            "operational_samples": _count(OperationalSample),
        },
        "sources": [
            {
                "id": source.id,
                "type": source.connector_type.value,
                "name": source.name,
                "base_url": source.base_url,
                "enabled": source.enabled,
                "last_connection_test_at": _timestamp(source.last_connection_test_at),
                "last_connection_status": source.last_connection_status,
                "server_identifier": source.server_identifier,
            }
            for source in sources
        ],
        "operations": operational_snapshot(),
    }
    archive = io.BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        _write_json(bundle, "summary.json", summary)
        _write_json(
            bundle,
            "recent-job-runs.json",
            [
                {
                    "id": run.id,
                    "job_id": run.job_id,
                    "trigger": run.trigger.value,
                    "status": run.status.value,
                    "attempt_count": run.attempt_count,
                    "max_attempts": run.max_attempts,
                    "started_at": _timestamp(run.started_at),
                    "finished_at": _timestamp(run.finished_at),
                    "heartbeat_at": _timestamp(run.heartbeat_at),
                    "processed": run.processed,
                    "updated": run.updated,
                    "failed": run.failed,
                    "summary": redact_text(run.summary or ""),
                }
                for run in runs
            ],
        )
        _write_json(
            bundle,
            "recent-sync-errors.json",
            [
                {
                    "id": error.id,
                    "sync_run_id": error.sync_run_id,
                    "library_id": error.library_id,
                    "media_external_id": error.media_external_id,
                    "category": error.category,
                    "message": redact_text(error.message),
                    "occurred_at": _timestamp(error.occurred_at),
                }
                for error in sync_errors
            ],
        )
        _write_json(
            bundle,
            "recent-async-tasks.json",
            [
                {
                    "id": task.id,
                    "task_type": task.task_type,
                    "status": task.status,
                    "attempts": task.attempts,
                    "next_attempt_at": _timestamp(task.next_attempt_at),
                    "last_attempt_at": _timestamp(task.last_attempt_at),
                    "last_error": redact_text(task.last_error or ""),
                }
                for task in tasks
            ],
        )
        _write_json(
            bundle,
            "recent-operational-samples.json",
            [
                {
                    "captured_at": _timestamp(sample.captured_at),
                    "queued_jobs": sample.queued_jobs,
                    "pending_tasks": sample.pending_tasks,
                    "heartbeat_age_seconds": sample.heartbeat_age_seconds,
                    "open_circuits": sample.open_circuits,
                    "connector_failures": sample.connector_failures,
                    "average_connector_latency_ms": sample.average_connector_latency_ms,
                    "slow_operations": sample.slow_operations,
                    "database_bytes": sample.database_bytes,
                    "wal_bytes": sample.wal_bytes,
                    "alert_level": sample.alert_level,
                    "alert_count": sample.alert_count,
                    "alert_summary": redact_text(sample.alert_summary or ""),
                }
                for sample in samples
            ],
        )
        log_dir = Path(app.instance_path) / "job-logs"
        for run in runs[:_MAX_LOGS]:
            if not run.log_filename:
                continue
            path = (log_dir / run.log_filename).resolve()
            if path.parent != log_dir.resolve() or not path.is_file():
                continue
            content = path.read_bytes()[-_MAX_LOG_BYTES:].decode("utf-8", errors="replace")
            bundle.writestr(f"job-logs/{path.name}", redact_text(content))
    archive.seek(0)
    return archive


def _count(model: Any) -> int:
    return int(db.session.scalar(select(func.count()).select_from(model)) or 0)


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _write_json(bundle: ZipFile, name: str, value: Any) -> None:
    bundle.writestr(
        name,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str),
    )
