"""Operational health endpoint tests."""

import pytest
from flask import Flask

from euvieouvi import health


def test_liveness_has_no_dependency_details(app: Flask) -> None:
    response = app.test_client().get("/health/live")

    assert response.status_code == 200
    assert response.get_json() == {"status": "alive"}


def test_readiness_reports_persistence_and_pending_database(app: Flask) -> None:
    response = app.test_client().get("/health/ready")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ready",
        "persistence": "ready",
        "database": "pending",
        "schema": "pending",
    }


def test_readiness_fails_when_persistence_is_unavailable(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "instance_path_is_ready", lambda path: False)

    response = app.test_client().get("/health/ready")

    assert response.status_code == 503
    assert response.get_json()["status"] == "not_ready"
    assert response.get_json()["persistence"] == "unavailable"
