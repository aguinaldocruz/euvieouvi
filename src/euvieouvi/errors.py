"""Basic application error contract and Flask registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from flask import Flask, Response, g, jsonify
from werkzeug.exceptions import HTTPException


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
        payload: dict[str, Any] = {
            "error": {
                "code": error.code,
                "message": error.message,
                "status": error.status,
                "request_id": getattr(g, "request_id", None),
                "details": error.details,
            }
        }
        return jsonify(payload), error.status

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException) -> tuple[Response, int]:
        status = error.code or 500
        codes = {404: "not_found", 405: "method_not_allowed", 413: "request_too_large"}
        messages = {
            404: "The requested resource was not found.",
            405: "The method is not allowed for this resource.",
            413: "The request body is too large.",
        }
        payload: dict[str, Any] = {
            "error": {
                "code": codes.get(status, "http_error"),
                "message": messages.get(status, "The HTTP request could not be processed."),
                "status": status,
                "request_id": getattr(g, "request_id", None),
                "details": [],
            }
        }
        return jsonify(payload), status
