"""Application factory for euvieouvi."""

from collections.abc import Mapping
from typing import Any

from flask import Flask

from euvieouvi.config import load_settings
from euvieouvi.errors import register_error_handlers
from euvieouvi.extensions import init_extensions
from euvieouvi.health import register_health_routes
from euvieouvi.instance import prepare_instance_path
from euvieouvi.logging import configure_logging
from euvieouvi.request_id import register_request_id

__version__ = "2.0.0.dev0"


def create_app(config_overrides: Mapping[str, Any] | None = None) -> Flask:
    """Create and configure an euvieouvi Flask application."""
    settings = load_settings(config_overrides)

    prepare_instance_path(settings.instance_path)

    app = Flask(
        __name__,
        instance_path=str(settings.instance_path),
        instance_relative_config=True,
    )
    app.config.from_mapping(settings.as_flask_mapping())

    configure_logging(settings.log_level)
    register_request_id(app)
    init_extensions(app)
    register_error_handlers(app)
    register_health_routes(app)

    app.logger.info(
        "application initialized",
        extra={"component": "application", "environment": settings.environment},
    )
    return app


__all__ = ["__version__", "create_app"]
