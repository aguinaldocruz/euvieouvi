"""Basic application error contract and Flask registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from flask import Flask, Response, g, jsonify


@dataclass(slots=True)
class AppError(Exception):
    """An expected application error safe to return to a client."""

    code: str
    message: str
    status: int = 400
    details: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)


def register_error_handlers(app: Flask) -> None:
    """Register handlers shared by future API and web adapters."""

    @app.errorhandler(AppError)
    def handle_app_error(error: AppError) -> tuple[Response, int]:
        payload = {
            "error": {
                "code": error.code,
                "message": error.message,
                "status": error.status,
                "request_id": getattr(g, "request_id", None),
                "details": error.details,
            }
        }
        return jsonify(payload), error.status
