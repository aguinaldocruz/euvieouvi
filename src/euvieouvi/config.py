"""Environment-backed application configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    host: str
    port: int
    instance_path: Path
    gunicorn_threads: int
    timezone: str

    def as_flask_mapping(self) -> dict[str, Any]:
        """Return Flask configuration without leaking environment-specific names."""
        return {
            "ENVIRONMENT": self.environment,
            "SECRET_KEY": self.secret_key,
            "LOG_LEVEL": self.log_level,
            "TESTING": self.testing,
            "SERVER_HOST": self.host,
            "SERVER_PORT": self.port,
            "GUNICORN_THREADS": self.gunicorn_threads,
            "TIMEZONE": self.timezone,
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

    host = str(values.get("HOST", os.getenv("EUVIEOUVI_HOST", "0.0.0.0"))).strip()
    if not host:
        raise ConfigurationError("EUVIEOUVI_HOST must not be empty.")

    port = _as_int(
        values.get("PORT", os.getenv("EUVIEOUVI_PORT", "8000")),
        name="EUVIEOUVI_PORT",
        minimum=1,
        maximum=65535,
    )
    gunicorn_threads = _as_int(
        values.get("GUNICORN_THREADS", os.getenv("EUVIEOUVI_GUNICORN_THREADS", "4")),
        name="EUVIEOUVI_GUNICORN_THREADS",
        minimum=1,
        maximum=32,
    )

    raw_instance_path = str(
        values.get(
            "INSTANCE_PATH",
            os.getenv("EUVIEOUVI_INSTANCE_PATH", str(Path.cwd() / "instance")),
        )
    ).strip()
    if not raw_instance_path:
        raise ConfigurationError("EUVIEOUVI_INSTANCE_PATH must not be empty.")
    instance_path = Path(raw_instance_path).expanduser().resolve()

    timezone = str(
        values.get("TIMEZONE", os.getenv("EUVIEOUVI_TIMEZONE", "America/Sao_Paulo"))
    ).strip()
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ConfigurationError("EUVIEOUVI_TIMEZONE must be a valid IANA timezone.") from error

    return Settings(
        environment=environment,
        secret_key=secret_key,
        log_level=log_level,
        testing=testing,
        host=host,
        port=port,
        instance_path=instance_path,
        gunicorn_threads=gunicorn_threads,
        timezone=timezone,
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


def _as_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be an integer from {minimum} to {maximum}.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(
            f"{name} must be an integer from {minimum} to {maximum}."
        ) from error
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{name} must be an integer from {minimum} to {maximum}.")
    return parsed
