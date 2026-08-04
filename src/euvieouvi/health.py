"""Operational health endpoints."""

from pathlib import Path
from typing import Any

from flask import Blueprint, Flask, current_app

from euvieouvi.instance import instance_path_is_ready

health_blueprint = Blueprint("health", __name__)


@health_blueprint.get("/health/live")
def live() -> dict[str, str]:
    """Report that the HTTP process is alive without checking dependencies."""
    return {"status": "alive"}


@health_blueprint.get("/health/ready")
def ready() -> tuple[dict[str, Any], int]:
    """Report initial readiness before the database phase is implemented."""
    persistence_ready = instance_path_is_ready(Path(current_app.instance_path))
    status = "ready" if persistence_ready else "not_ready"
    return (
        {
            "status": status,
            "persistence": "ready" if persistence_ready else "unavailable",
            "database": "pending",
            "schema": "pending",
        },
        200 if persistence_ready else 503,
    )


def register_health_routes(app: Flask) -> None:
    """Register operational routes on the application."""
    app.register_blueprint(health_blueprint)
