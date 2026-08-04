"""Server-rendered web interface."""

from flask import Flask

from euvieouvi.web.csrf import register_csrf
from euvieouvi.web.routes import blueprint


def register_web(app: Flask) -> None:
    register_csrf(app)
    app.register_blueprint(blueprint)


__all__ = ["register_web"]
