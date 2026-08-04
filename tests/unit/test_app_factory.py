"""Application factory tests."""

from flask import Flask

from euvieouvi import __version__, create_app


def test_create_app_returns_configured_flask_application() -> None:
    app = create_app(
        {
            "ENVIRONMENT": "testing",
            "SECRET_KEY": "test-secret-key",
            "TESTING": True,
        }
    )

    assert isinstance(app, Flask)
    assert app.testing is True
    assert app.config["ENVIRONMENT"] == "testing"
    assert __version__ == "2.0.0.dev0"
