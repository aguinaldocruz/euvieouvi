"""Versioned REST API adapter."""

from flask import Flask

from euvieouvi.api.routes import blueprint


def register_api(app: Flask) -> None:
    """Register the approved v1 API without enabling cross-origin access."""
    app.register_blueprint(blueprint)


__all__ = ["register_api"]
