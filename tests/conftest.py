"""Shared pytest fixtures."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask_migrate import upgrade  # type: ignore[import-untyped]

from euvieouvi import create_app


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Flask]:
    application = create_app(
        {
            "ENVIRONMENT": "testing",
            "SECRET_KEY": "test-secret-key",
            "TESTING": True,
            "LOG_LEVEL": "DEBUG",
            "INSTANCE_PATH": tmp_path / "instance",
            "DATABASE_URI": f"sqlite:///{tmp_path / 'instance' / 'test.db'}",
        }
    )
    with application.app_context():
        upgrade(directory="migrations")
    yield application
