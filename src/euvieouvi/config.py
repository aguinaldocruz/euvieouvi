"""Environment-backed application configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from euvieouvi.domain.errors import ConfigurationError

_VALID_ENVIRONMENTS: Final = frozenset({"development", "production", "testing"})
_VALID_LOG_LEVELS: Final = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated settings used to initialize Flask."""

    environment: str
    secret_key: str
    log_level: str
    testing: bool

    def as_flask_mapping(self) -> dict[str, Any]:
        """Return Flask configuration without leaking environment-specific names."""
        return {
            "ENVIRONMENT": self.environment,
            "SECRET_KEY": self.secret_key,
            "LOG_LEVEL": self.log_level,
            "TESTING": self.testing,
        }


def load_settings(overrides: Mapping[str, Any] | None = None) -> Settings:
    """Load settings from the environment and optional explicit overrides."""
    values = dict(overrides or {})

    environment = str(values.get("ENVIRONMENT", os.getenv("EUVIEOUVI_ENV", "production"))).lower()
    if environment not in _VALID_ENVIRONMENTS:
        allowed = ", ".join(sorted(_VALID_ENVIRONMENTS))
        raise ConfigurationError(f"EUVIEOUVI_ENV must be one of: {allowed}.")

    testing = _as_bool(values.get("TESTING", environment == "testing"), name="TESTING")
    secret_key = str(values.get("SECRET_KEY", os.getenv("EUVIEOUVI_SECRET_KEY", ""))).strip()
    if not secret_key:
        raise ConfigurationError("EUVIEOUVI_SECRET_KEY is required.")

    log_level = str(values.get("LOG_LEVEL", os.getenv("EUVIEOUVI_LOG_LEVEL", "INFO"))).upper()
    if log_level not in _VALID_LOG_LEVELS:
        allowed = ", ".join(sorted(_VALID_LOG_LEVELS))
        raise ConfigurationError(f"EUVIEOUVI_LOG_LEVEL must be one of: {allowed}.")

    return Settings(
        environment=environment,
        secret_key=secret_key,
        log_level=log_level,
        testing=testing,
    )


def _as_bool(value: Any, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigurationError(f"{name} must be a boolean value.")
