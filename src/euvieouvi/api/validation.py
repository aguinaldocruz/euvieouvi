"""Strict HTTP input validation and opaque cursor helpers."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

from flask import Request
from werkzeug.exceptions import HTTPException

from euvieouvi.errors import AppError


def json_object(
    request: Request, *, allowed: set[str], required: set[str] | frozenset[str] = frozenset()
) -> dict[str, Any]:
    if not request.is_json:
        raise AppError("unsupported_media_type", "Content-Type must be application/json.", 415)
    try:
        value = request.get_json()
    except HTTPException:
        raise
    except Exception as error:
        raise AppError("invalid_json", "The request body is not valid JSON.", 400) from error
    if not isinstance(value, dict):
        raise validation_error("body", "invalid_type", "A JSON object is required.")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise validation_error(sorted(unknown)[0], "unknown_field", "Unknown fields are rejected.")
    if missing:
        raise validation_error(sorted(missing)[0], "required", "This field is required.")
    return value


def validation_error(field: str, code: str, message: str) -> AppError:
    return AppError(
        "validation_error",
        "The request contains invalid fields.",
        422,
        [{"field": field, "code": code, "message": message}],
    )


def string(value: object, field: str, *, maximum: int = 500, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value.strip())
        or len(value) > maximum
    ):
        raise validation_error(
            field, "invalid_string", f"A string up to {maximum} characters is required."
        )
    return value.strip()


def boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise validation_error(field, "invalid_boolean", "A boolean value is required.")
    return value


def integer(value: object, field: str, *, minimum: int | None = None) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or (minimum is not None and value < minimum)
    ):
        raise validation_error(field, "invalid_integer", "A valid integer is required.")
    return value


def http_url(value: object, field: str = "base_url") -> str:
    result = string(value, field, maximum=2048).rstrip("/")
    parsed = urlsplit(result)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise validation_error(
            field, "invalid_url", "A valid HTTP or HTTPS URL without credentials is required."
        )
    return result


def query_args(request: Request, allowed: set[str]) -> dict[str, str]:
    unknown = set(request.args) - allowed
    repeated = [key for key in request.args if len(request.args.getlist(key)) != 1]
    if unknown or repeated:
        field = sorted(unknown or set(repeated))[0]
        raise validation_error(
            field, "invalid_query_parameter", "Unknown or repeated query parameter."
        )
    return {key: value for key, value in request.args.items()}


def limit(value: str | None) -> int:
    if value is None:
        return 50
    try:
        parsed = int(value)
    except ValueError as error:
        raise validation_error("limit", "invalid_limit", "Limit must be from 1 to 200.") from error
    if not 1 <= parsed <= 200:
        raise validation_error("limit", "invalid_limit", "Limit must be from 1 to 200.")
    return parsed


def bool_query(value: str | None, field: str) -> bool | None:
    if value is None:
        return None
    if value not in {"true", "false"}:
        raise validation_error(field, "invalid_boolean", "Use true or false.")
    return value == "true"


def encode_cursor(last_id: int, fingerprint: str) -> str:
    raw = json.dumps({"id": last_id, "f": fingerprint}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None, fingerprint: str) -> int | None:
    if value is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
        if set(payload) != {"id", "f"} or payload["f"] != fingerprint:
            raise ValueError
        return integer(payload["id"], "cursor", minimum=1)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise validation_error(
            "cursor", "invalid_cursor", "Cursor is invalid for this query."
        ) from error


def fingerprint(values: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]
