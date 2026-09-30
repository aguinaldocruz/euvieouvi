"""Runtime telemetry, connector circuit breaking and operational snapshots."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, g, has_request_context, request
from sqlalchemy import case, delete, func, select

from euvieouvi.connectors.errors import (
    ConnectorConnectionError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorRateLimitError,
)
from euvieouvi.database.enums import SyncStatus
from euvieouvi.database.models import AsyncTask, JobRun, OperationalSample, Source
from euvieouvi.database.sqlite_runtime import (
    sqlite_runtime_is_safe,
    sqlite_runtime_version,
)
from euvieouvi.extensions import db
from euvieouvi.logging import redact_access_path, redact_text


@dataclass(slots=True)
class ConnectorMetric:
    key: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    rejected: int = 0
    consecutive_failures: int = 0
    total_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    last_latency_ms: float | None = None
    last_error: str | None = None
    last_call_at: str | None = None
    opened_until_monotonic: float | None = None
    opened_until: str | None = None

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("opened_until_monotonic", None)
        value["average_latency_ms"] = (
            round(self.total_latency_ms / self.calls, 1) if self.calls else None
        )
        value["total_latency_ms"] = round(self.total_latency_ms, 1)
        value["max_latency_ms"] = round(self.max_latency_ms, 1)
        if self.last_latency_ms is not None:
            value["last_latency_ms"] = round(self.last_latency_ms, 1)
        return value


@dataclass(frozen=True, slots=True)
class SlowOperation:
    category: str
    name: str
    duration_ms: float
    occurred_at: str
    detail: str | None = None


class CircuitBreakerRegistry:
    """Thread-safe process-local connector protection and bounded telemetry."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
        utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._monotonic = monotonic
        self._utc_clock = utc_clock
        self._lock = threading.Lock()
        self._metrics: dict[str, ConnectorMetric] = {}
        self._slow: deque[SlowOperation] = deque(maxlen=50)

    @contextmanager
    def observe(self, key: str | None, operation: str) -> Iterator[None]:
        if not key:
            yield
            return
        started = self._monotonic()
        with self._lock:
            metric = self._metrics.setdefault(key, ConnectorMetric(key=key))
            if (
                metric.opened_until_monotonic is not None
                and metric.opened_until_monotonic > started
            ):
                metric.rejected += 1
                raise ConnectorConnectionError(
                    f"{key} circuit is temporarily open after repeated failures."
                )
            if metric.opened_until_monotonic is not None:
                metric.opened_until_monotonic = None
                metric.opened_until = None
            metric.calls += 1
            metric.last_call_at = self._utc_clock().isoformat()
        try:
            yield
        except ConnectorNotFoundError:
            self._record_success(key, operation, started)
            raise
        except ConnectorError as error:
            retry_after = (
                error.retry_after_seconds if isinstance(error, ConnectorRateLimitError) else None
            )
            self._record_failure(
                key,
                operation,
                started,
                error,
                trips_circuit=isinstance(
                    error, (ConnectorConnectionError, ConnectorRateLimitError)
                ),
                retry_after=retry_after,
            )
            raise
        except Exception as error:
            self._record_failure(key, operation, started, error, trips_circuit=False)
            raise
        else:
            self._record_success(key, operation, started)

    def record_slow(
        self, category: str, name: str, duration_ms: float, detail: str | None = None
    ) -> None:
        operation = SlowOperation(
            category=category,
            name=name,
            duration_ms=round(duration_ms, 1),
            occurred_at=self._utc_clock().isoformat(),
            detail=detail,
        )
        with self._lock:
            self._slow.appendleft(operation)

    def snapshot(self) -> dict[str, Any]:
        now = self._monotonic()
        with self._lock:
            connectors = []
            for metric in self._metrics.values():
                value = metric.public()
                value["circuit"] = (
                    "open"
                    if metric.opened_until_monotonic is not None
                    and metric.opened_until_monotonic > now
                    else "closed"
                )
                connectors.append(value)
            slow = [asdict(item) for item in self._slow]
        return {"connectors": sorted(connectors, key=lambda item: item["key"]), "slow": slow}

    def reset(self) -> None:
        with self._lock:
            self._metrics.clear()
            self._slow.clear()

    def _record_success(self, key: str, operation: str, started: float) -> None:
        duration_ms = (self._monotonic() - started) * 1000
        with self._lock:
            metric = self._metrics[key]
            metric.successes += 1
            metric.consecutive_failures = 0
            metric.last_error = None
            metric.opened_until_monotonic = None
            metric.opened_until = None
            self._record_latency(metric, duration_ms)
        if duration_ms >= 1000:
            self.record_slow("connector", f"{key}:{operation}", duration_ms)

    def _record_failure(
        self,
        key: str,
        operation: str,
        started: float,
        error: Exception,
        *,
        trips_circuit: bool,
        retry_after: float | None = None,
    ) -> None:
        duration_ms = (self._monotonic() - started) * 1000
        with self._lock:
            metric = self._metrics[key]
            metric.failures += 1
            metric.consecutive_failures = metric.consecutive_failures + 1 if trips_circuit else 0
            metric.last_error = redact_text(f"{type(error).__name__}: {error}")[:500]
            self._record_latency(metric, duration_ms)
            if trips_circuit and metric.consecutive_failures >= self.failure_threshold:
                cooldown = max(self.cooldown_seconds, retry_after or 0.0)
                metric.opened_until_monotonic = self._monotonic() + cooldown
                metric.opened_until = datetime.fromtimestamp(
                    self._utc_clock().timestamp() + cooldown, tz=UTC
                ).isoformat()
        if duration_ms >= 1000:
            self.record_slow("connector", f"{key}:{operation}", duration_ms, type(error).__name__)

    @staticmethod
    def _record_latency(metric: ConnectorMetric, duration_ms: float) -> None:
        metric.total_latency_ms += duration_ms
        metric.max_latency_ms = max(metric.max_latency_ms, duration_ms)
        metric.last_latency_ms = duration_ms


connector_telemetry = CircuitBreakerRegistry()


@contextmanager
def observe_connector(key: str | None, operation: str) -> Iterator[None]:
    with connector_telemetry.observe(key, operation):
        yield


def operational_snapshot(*, include_history: bool = True) -> dict[str, Any]:
    """Return non-secret queue, worker and connector health data."""
    now = datetime.now(UTC)
    job_counts = {
        str(status): int(count)
        for status, count in db.session.execute(
            select(JobRun.status, func.count(JobRun.id)).group_by(JobRun.status)
        )
    }
    task_counts = {
        str(status): int(count)
        for status, count in db.session.execute(
            select(AsyncTask.status, func.count(AsyncTask.id)).group_by(AsyncTask.status)
        )
    }
    oldest_job = db.session.scalar(
        select(func.min(JobRun.available_at)).where(JobRun.status == SyncStatus.QUEUED)
    )
    oldest_task = db.session.scalar(
        select(func.min(AsyncTask.next_attempt_at)).where(AsyncTask.status == "pending")
    )
    running = db.session.scalar(
        select(JobRun).where(JobRun.status == SyncStatus.RUNNING).order_by(JobRun.id)
    )
    runtime = connector_telemetry.snapshot()
    runtime_by_key = {item["key"]: item for item in runtime["connectors"]}
    sources = []
    for source in db.session.scalars(select(Source).order_by(Source.id)):
        key = f"{source.connector_type.value}:{source.id}"
        metric = runtime_by_key.get(key, {})
        sources.append(
            {
                "id": source.id,
                "name": source.name,
                "type": source.connector_type.value,
                "enabled": source.enabled,
                "last_connection_status": redact_text(source.last_connection_status or "") or None,
                "circuit": metric.get("circuit", "closed"),
                "consecutive_failures": metric.get("consecutive_failures", 0),
                "average_latency_ms": metric.get("average_latency_ms"),
                "last_error": metric.get("last_error"),
                "opened_until": metric.get("opened_until"),
            }
        )
    database_bytes, wal_bytes = _database_sizes()
    snapshot = {
        "generated_at": now.isoformat(),
        "jobs": {
            "counts": job_counts,
            "oldest_queued_seconds": _age_seconds(oldest_job, now),
            "running": (
                {
                    "id": running.id,
                    "job_id": running.job_id,
                    "attempt": running.attempt_count,
                    "heartbeat_age_seconds": _age_seconds(running.heartbeat_at, now),
                    "lease_owner": bool(running.lease_owner),
                }
                if running is not None
                else None
            ),
        },
        "async_tasks": {
            "counts": task_counts,
            "oldest_pending_seconds": _age_seconds(oldest_task, now),
        },
        "sources": sources,
        "slow_operations": runtime["slow"],
        "database": {
            "sqlite_runtime": sqlite_runtime_version(),
            "sqlite_runtime_safe": sqlite_runtime_is_safe(),
            "database_bytes": database_bytes,
            "wal_bytes": wal_bytes,
        },
    }
    snapshot["alerts"] = _operational_alerts(snapshot)
    snapshot["history"] = _operational_history(now) if include_history else None
    return snapshot


def record_operational_sample(*, now: datetime | None = None, force: bool = False) -> bool:
    """Persist one five-minute operational rollup and prune samples older than 30 days."""
    captured_at = now or datetime.now(UTC)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=UTC)
    latest = db.session.scalar(
        select(OperationalSample.captured_at)
        .order_by(OperationalSample.captured_at.desc(), OperationalSample.id.desc())
        .limit(1)
    )
    if latest is not None and not force:
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=UTC)
        if captured_at - latest < timedelta(minutes=5):
            return False

    snapshot = operational_snapshot(include_history=False)
    sources = snapshot["sources"]
    latencies = [
        float(source["average_latency_ms"])
        for source in sources
        if source["average_latency_ms"] is not None
    ]
    alerts = snapshot["alerts"]
    level = (
        "critical"
        if any(item["level"] == "critical" for item in alerts)
        else ("warning" if alerts else "ok")
    )
    running = snapshot["jobs"]["running"]
    db.session.add(
        OperationalSample(
            captured_at=captured_at,
            queued_jobs=int(snapshot["jobs"]["counts"].get("queued", 0)),
            pending_tasks=int(snapshot["async_tasks"]["counts"].get("pending", 0)),
            heartbeat_age_seconds=(
                int(running["heartbeat_age_seconds"])
                if running is not None and running["heartbeat_age_seconds"] is not None
                else None
            ),
            open_circuits=sum(source["circuit"] == "open" for source in sources),
            connector_failures=sum(int(source["consecutive_failures"]) for source in sources),
            average_connector_latency_ms=(sum(latencies) / len(latencies) if latencies else None),
            slow_operations=len(snapshot["slow_operations"]),
            database_bytes=int(snapshot["database"]["database_bytes"]),
            wal_bytes=int(snapshot["database"]["wal_bytes"]),
            alert_level=level,
            alert_count=len(alerts),
            alert_summary=" | ".join(str(item["message"]) for item in alerts) or None,
        )
    )
    db.session.execute(
        delete(OperationalSample).where(
            OperationalSample.captured_at < captured_at - timedelta(days=30)
        )
    )
    db.session.commit()
    return True


def register_observability(app: Flask) -> None:
    @app.before_request
    def start_request_timer() -> None:
        g.euvieouvi_started_at = time.perf_counter()

    @app.after_request
    def record_request_timing(response: Any) -> Any:
        started = getattr(g, "euvieouvi_started_at", None)
        if started is None:
            return response
        duration_ms = (time.perf_counter() - started) * 1000
        response.headers.setdefault("Server-Timing", f"app;dur={duration_ms:.1f}")
        if duration_ms >= 1000 and has_request_context():
            rule = request.url_rule.rule if request.url_rule is not None else request.path
            safe_path = redact_access_path(str(rule))
            connector_telemetry.record_slow(
                "http", f"{request.method} {safe_path}", duration_ms, str(response.status_code)
            )
            app.logger.warning(
                "slow request method=%s path=%s duration_ms=%.1f status=%s",
                request.method,
                safe_path,
                duration_ms,
                response.status_code,
            )
        return response


def _age_seconds(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0, int((now - value).total_seconds()))


def _database_sizes() -> tuple[int, int]:
    database_name = db.engine.url.database
    if not database_name or database_name == ":memory:":
        return (0, 0)
    path = Path(database_name)
    database_bytes = path.stat().st_size if path.is_file() else 0
    wal = Path(f"{path}-wal")
    return (database_bytes, wal.stat().st_size if wal.is_file() else 0)


def _operational_alerts(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    if not snapshot["database"]["sqlite_runtime_safe"]:
        version = snapshot["database"]["sqlite_runtime"]
        alerts.append(
            {
                "level": "critical",
                "code": "unsafe_sqlite",
                "message": f"SQLite {version} requer atualização para uma versão WAL segura.",
            }
        )
    running = snapshot["jobs"]["running"]
    heartbeat = running["heartbeat_age_seconds"] if running is not None else None
    if heartbeat is not None and heartbeat > 90:
        alerts.append(
            {
                "level": "critical",
                "code": "stale_heartbeat",
                "message": f"Heartbeat do job está atrasado em {heartbeat} segundos.",
            }
        )
    elif heartbeat is not None and heartbeat > 45:
        alerts.append(
            {
                "level": "warning",
                "code": "late_heartbeat",
                "message": f"Heartbeat do job está atrasado em {heartbeat} segundos.",
            }
        )
    queued_age = snapshot["jobs"]["oldest_queued_seconds"]
    if queued_age is not None and queued_age > 300:
        alerts.append(
            {
                "level": "warning",
                "code": "old_job_queue",
                "message": f"Job aguarda na fila há {queued_age} segundos.",
            }
        )
    task_age = snapshot["async_tasks"]["oldest_pending_seconds"]
    if task_age is not None and task_age > 300:
        alerts.append(
            {
                "level": "warning",
                "code": "old_async_queue",
                "message": f"Atualização instantânea aguarda há {task_age} segundos.",
            }
        )
    for source in snapshot["sources"]:
        if source["circuit"] == "open":
            alerts.append(
                {
                    "level": "critical",
                    "code": "open_circuit",
                    "message": f"Circuito aberto para {source['name']}.",
                }
            )
        elif int(source["consecutive_failures"]) >= 3:
            alerts.append(
                {
                    "level": "warning",
                    "code": "connector_failures",
                    "message": (
                        f"{source['name']} tem "
                        f"{source['consecutive_failures']} falhas consecutivas."
                    ),
                }
            )
    return alerts


def _operational_history(now: datetime) -> dict[str, Any]:
    since = now - timedelta(hours=24)
    row = db.session.execute(
        select(
            func.count(OperationalSample.id),
            func.sum(case((OperationalSample.alert_level != "ok", 1), else_=0)),
            func.max(OperationalSample.queued_jobs),
            func.max(OperationalSample.pending_tasks),
            func.max(OperationalSample.heartbeat_age_seconds),
            func.max(OperationalSample.average_connector_latency_ms),
            func.max(OperationalSample.wal_bytes),
            func.max(OperationalSample.captured_at),
        ).where(OperationalSample.captured_at >= since)
    ).one()
    return {
        "samples_24h": int(row[0] or 0),
        "alert_samples_24h": int(row[1] or 0),
        "max_queued_jobs_24h": int(row[2] or 0),
        "max_pending_tasks_24h": int(row[3] or 0),
        "max_heartbeat_age_seconds_24h": int(row[4]) if row[4] is not None else None,
        "max_connector_latency_ms_24h": round(float(row[5]), 1) if row[5] is not None else None,
        "max_wal_bytes_24h": int(row[6] or 0),
        "last_sample_at": row[7].isoformat() if row[7] is not None else None,
    }
