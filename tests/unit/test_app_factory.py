"""Application factory tests."""

from pathlib import Path

from flask import Flask

from euvieouvi import __version__, create_app


def test_create_app_returns_configured_flask_application(tmp_path: Path) -> None:
    app = create_app(
        {
            "ENVIRONMENT": "testing",
            "SECRET_KEY": "test-secret-key",
            "TESTING": True,
            "INSTANCE_PATH": tmp_path / "instance",
        }
    )

    assert isinstance(app, Flask)
    assert app.testing is True
    assert app.config["ENVIRONMENT"] == "testing"
    assert app.instance_path == str((tmp_path / "instance").resolve())
    assert __version__ == "2.0.0.dev0"
