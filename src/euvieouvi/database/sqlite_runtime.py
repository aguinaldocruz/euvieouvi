"""SQLite runtime compatibility checks for the WAL-reset fix."""

from __future__ import annotations

import sqlite3

from euvieouvi.domain.errors import ConfigurationError


def sqlite_runtime_version() -> str:
    return sqlite3.sqlite_version


def sqlite_runtime_is_safe(version: str | None = None) -> bool:
    """Return whether a runtime contains the upstream WAL-reset correction."""
    parts = _version_tuple(version or sqlite_runtime_version())
    if parts >= (3, 51, 3):
        return True
    return (parts[:2] == (3, 50) and parts[2] >= 7) or (parts[:2] == (3, 44) and parts[2] >= 6)


def require_safe_sqlite_runtime() -> None:
    version = sqlite_runtime_version()
    if not sqlite_runtime_is_safe(version):
        raise ConfigurationError(
            "SQLite runtime "
            f"{version} is affected by the WAL-reset race; use SQLite 3.51.3 or a fixed backport."
        )


def is_sqlite_lock_error(error: BaseException) -> bool:
    """Return whether an exception chain represents transient SQLite writer contention."""
    current: BaseException | None = error
    while current is not None:
        message = str(current).casefold()
        if "database is locked" in message or "database table is locked" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


def _version_tuple(value: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(part) for part in value.split(".")[:3])
    except ValueError:
        return (0, 0, 0)
    padded = (*parts, 0, 0, 0)
    return (padded[0], padded[1], padded[2])
