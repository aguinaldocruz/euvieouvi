"""SQLite WAL-reset runtime compatibility checks."""

import pytest

from euvieouvi.database.sqlite_runtime import (
    is_sqlite_lock_error,
    require_safe_sqlite_runtime,
    sqlite_runtime_is_safe,
)
from euvieouvi.domain.errors import ConfigurationError


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("3.40.1", False),
        ("3.44.5", False),
        ("3.44.6", True),
        ("3.50.6", False),
        ("3.50.7", True),
        ("3.51.2", False),
        ("3.51.3", True),
        ("3.53.4", True),
        ("invalid", False),
    ],
)
def test_sqlite_runtime_safety(version: str, expected: bool) -> None:
    assert sqlite_runtime_is_safe(version) is expected


def test_unsafe_runtime_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "euvieouvi.database.sqlite_runtime.sqlite_runtime_version", lambda: "3.40.1"
    )

    with pytest.raises(ConfigurationError, match="WAL-reset"):
        require_safe_sqlite_runtime()


def test_sqlite_lock_detection_walks_wrapped_exception_chain() -> None:
    try:
        try:
            raise RuntimeError("database is locked")
        except RuntimeError as error:
            raise OSError("wrapped") from error
    except OSError as wrapped:
        assert is_sqlite_lock_error(wrapped) is True

    assert is_sqlite_lock_error(RuntimeError("different failure")) is False
