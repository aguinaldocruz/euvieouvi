"""Operational health endpoints."""

from pathlib import Path
from typing import Any

from flask import Blueprint, Flask, current_app

from euvieouvi.database.schema import database_status
from euvieouvi.instance import instance_path_is_ready

health_blueprint = Blueprint("health", __name__)


@health_blueprint.get("/health/live")
def live() -> dict[str, str]:
    """Report that the HTTP process is alive without checking dependencies."""
    return {"status": "alive"}


@health_blueprint.get("/health/ready")
def ready() -> tuple[dict[str, Any], int]:
    """Report storage, database and schema readiness."""
    persistence_ready = instance_path_is_ready(Path(current_app.instance_path))
    database_ready, schema_ready = database_status() if persistence_ready else (False, False)
    is_ready = persistence_ready and database_ready and schema_ready
    status = "ready" if is_ready else "not_ready"
    return (
        {
            "status": status,
            "persistence": "ready" if persistence_ready else "unavailable",
            "database": "ready" if database_ready else "unavailable",
            "schema": "current" if schema_ready else "outdated",
        },
        200 if is_ready else 503,
    )


def register_health_routes(app: Flask) -> None:
    """Register operational routes on the application."""
    app.register_blueprint(health_blueprint)
