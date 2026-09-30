"""Operational health endpoint tests."""

import pytest
from flask import Flask

from euvieouvi import health


def test_liveness_has_no_dependency_details(app: Flask) -> None:
    response = app.test_client().get("/health/live")

    assert response.status_code == 200
    assert response.get_json() == {"status": "alive"}


def test_readiness_reports_current_database(app: Flask) -> None:
    response = app.test_client().get("/health/ready")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ready"
    assert payload["persistence"] == "ready"
    assert payload["database"] == "ready"
    assert payload["schema"] == "current"
    assert payload["sqlite_runtime"]
    assert isinstance(payload["sqlite_runtime_safe"], bool)


def test_operational_metrics_are_non_secret_and_structured(app: Flask) -> None:
    response = app.test_client().get("/health/metrics")

    assert response.status_code == 200
    payload = response.get_json()
    assert {
        "generated_at",
        "jobs",
        "async_tasks",
        "sources",
        "slow_operations",
        "database",
        "alerts",
        "history",
    } <= set(payload)
    assert payload["jobs"]["running"] is None
    assert payload["jobs"]["counts"] == {}
    assert payload["database"]["sqlite_runtime"]
    assert isinstance(payload["database"]["sqlite_runtime_safe"], bool)


def test_readiness_fails_when_persistence_is_unavailable(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "instance_path_is_ready", lambda path: False)

    response = app.test_client().get("/health/ready")

    assert response.status_code == 503
    assert response.get_json()["status"] == "not_ready"
    assert response.get_json()["persistence"] == "unavailable"


def test_readiness_fails_when_schema_is_outdated(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "database_status", lambda: (True, False))

    response = app.test_client().get("/health/ready")

    assert response.status_code == 503
    assert response.get_json()["database"] == "ready"
    assert response.get_json()["schema"] == "outdated"
