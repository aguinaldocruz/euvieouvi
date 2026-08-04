"""Small session-bound CSRF protection for browser mutations."""

from __future__ import annotations

import secrets

from flask import Flask, abort, request, session

TOKEN_KEY = "_csrf_token"


def csrf_token() -> str:
    token = session.get(TOKEN_KEY)
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session[TOKEN_KEY] = token
    return token


def register_csrf(app: Flask) -> None:
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")

    @app.before_request
    def protect_web_mutation() -> None:
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        if request.path.startswith("/api/"):
            return
        expected = session.get(TOKEN_KEY)
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if (
            not isinstance(expected, str)
            or not isinstance(supplied, str)
            or not secrets.compare_digest(expected, supplied)
        ):
            abort(400, description="Invalid CSRF token")
