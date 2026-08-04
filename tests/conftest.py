"""Shared pytest fixtures."""

from collections.abc import Iterator

import pytest
from flask import Flask

from euvieouvi import create_app


@pytest.fixture
def app() -> Iterator[Flask]:
    application = create_app(
        {
            "ENVIRONMENT": "testing",
            "SECRET_KEY": "test-secret-key",
            "TESTING": True,
            "LOG_LEVEL": "DEBUG",
        }
    )
    yield application
