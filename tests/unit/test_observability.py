"""Operational telemetry and connector circuit-breaker behavior."""

from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask

from euvieouvi.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorConnectionError,
    ConnectorNotFoundError,
)
from euvieouvi.database.enums import SyncStatus, SyncTrigger
from euvieouvi.database.models import JobRun, OperationalSample
from euvieouvi.extensions import db
from euvieouvi.observability import (
    CircuitBreakerRegistry,
    operational_snapshot,
    record_operational_sample,
)


def test_circuit_opens_after_repeated_failures_and_rejects_new_calls() -> None:
    registry = CircuitBreakerRegistry(failure_threshold=2, cooldown_seconds=60)

    for _ in range(2):
        with pytest.raises(ConnectorConnectionError), registry.observe("plex:1", "GET"):
            raise ConnectorConnectionError("offline")

    with (
        pytest.raises(ConnectorConnectionError, match="circuit is temporarily open"),
        registry.observe("plex:1", "GET"),
    ):
        raise AssertionError("the protected call must not execute")

    metric = registry.snapshot()["connectors"][0]
    assert metric["circuit"] == "open"
    assert metric["calls"] == 2
    assert metric["failures"] == 2
    assert metric["rejected"] == 1


def test_not_found_proves_server_availability_without_opening_circuit() -> None:
    registry = CircuitBreakerRegistry(failure_threshold=1)

    with pytest.raises(ConnectorNotFoundError), registry.observe("jellyfin:2", "GET"):
        raise ConnectorNotFoundError("missing item")

    metric = registry.snapshot()["connectors"][0]
    assert metric["circuit"] == "closed"
    assert metric["successes"] == 1
    assert metric["failures"] == 0


def test_configuration_failures_do_not_open_transient_circuit() -> None:
    registry = CircuitBreakerRegistry(failure_threshold=1)

    with pytest.raises(ConnectorAuthenticationError), registry.observe("plex:1", "GET"):
        raise ConnectorAuthenticationError("token=do-not-expose")

    metric = registry.snapshot()["connectors"][0]
    assert metric["circuit"] == "closed"
    assert metric["consecutive_failures"] == 0
    assert "do-not-expose" not in metric["last_error"]
    assert "[REDACTED]" in metric["last_error"]


def test_operational_samples_are_throttled_and_report_history(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("euvieouvi.observability.sqlite_runtime_is_safe", lambda: True)
    monkeypatch.setattr("euvieouvi.observability.sqlite_runtime_version", lambda: "3.53.4")
    now = datetime.now(UTC)

    with app.app_context():
        db.session.add(OperationalSample(captured_at=now - timedelta(days=31)))
        db.session.commit()
        assert record_operational_sample(now=now) is True
        assert record_operational_sample(now=now + timedelta(minutes=1)) is False
        assert record_operational_sample(now=now + timedelta(minutes=5)) is True

        samples = db.session.query(OperationalSample).all()
        assert len(samples) == 2
        assert all(
            sample.captured_at >= (now - timedelta(days=30)).replace(tzinfo=None)
            for sample in samples
        )
        snapshot = operational_snapshot()
        assert snapshot["history"]["samples_24h"] == 2
        assert snapshot["database"]["sqlite_runtime_safe"] is True


def test_stale_job_heartbeat_produces_critical_alert(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("euvieouvi.observability.sqlite_runtime_is_safe", lambda: True)
    monkeypatch.setattr("euvieouvi.observability.sqlite_runtime_version", lambda: "3.53.4")
    now = datetime.now(UTC)
    with app.app_context():
        db.session.add(
            JobRun(
                job_id="metadata",
                trigger=SyncTrigger.MANUAL,
                status=SyncStatus.RUNNING,
                heartbeat_at=now - timedelta(seconds=100),
                progress_percent=5,
            )
        )
        db.session.commit()

        snapshot = operational_snapshot()

    assert any(alert["code"] == "stale_heartbeat" for alert in snapshot["alerts"])
    assert any(alert["level"] == "critical" for alert in snapshot["alerts"])
