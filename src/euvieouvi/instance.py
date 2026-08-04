"""Persistent instance directory preparation."""

from __future__ import annotations

import os
from pathlib import Path

from euvieouvi.domain.errors import ConfigurationError


def prepare_instance_path(path: Path) -> None:
    """Create and validate the persistent application directory."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ConfigurationError(f"Unable to create instance directory: {path}.") from error

    if not path.is_dir():
        raise ConfigurationError(f"Instance path is not a directory: {path}.")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise ConfigurationError(f"Instance directory is not accessible: {path}.")


def instance_path_is_ready(path: Path) -> bool:
    """Return whether the persistent directory is currently usable."""
    return path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK)
