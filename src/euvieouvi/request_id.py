"""Request correlation for logs and responses."""

from __future__ import annotations

from uuid import UUID, uuid4

from flask import Flask, Response, g, request

REQUEST_ID_HEADER = "X-Request-ID"


def register_request_id(app: Flask) -> None:
    """Generate or accept a valid UUID request ID for each request."""

    @app.before_request
    def set_request_id() -> None:
        supplied = request.headers.get(REQUEST_ID_HEADER, "")
        g.request_id = _validated_request_id(supplied) or str(uuid4())

    @app.after_request
    def add_request_id_header(response: Response) -> Response:
        response.headers[REQUEST_ID_HEADER] = g.request_id
        return response


def _validated_request_id(value: str) -> str | None:
    if not value or len(value) > 36:
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return str(parsed)
