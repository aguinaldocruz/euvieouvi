"""Shared pytest fixtures."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask

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
        }
    )
    yield application
