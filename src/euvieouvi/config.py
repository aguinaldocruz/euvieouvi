"""Environment-backed application configuration."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
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
    database_uri: str
    sqlite_busy_timeout_ms: int
    credential_encryption_key: str

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
            "SQLALCHEMY_DATABASE_URI": self.database_uri,
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "SQLITE_BUSY_TIMEOUT_MS": self.sqlite_busy_timeout_ms,
            "CREDENTIAL_ENCRYPTION_KEY": self.credential_encryption_key,
        }


def load_settings(overrides: Mapping[str, Any] | None = None) -> Settings:
    """Load settings from the environment and optional explicit overrides."""
    values = dict(overrides or {})

    environment = str(values.get("ENVIRONMENT", os.getenv("EUVIEOUVI_ENV", "production"))).lower()
    if environment not in _VALID_ENVIRONMENTS:
        allowed = ", ".join(sorted(_VALID_ENVIRONMENTS))
        raise ConfigurationError(f"EUVIEOUVI_ENV must be one of: {allowed}.")

    raw_instance_path = str(
        values.get(
            "INSTANCE_PATH",
            os.getenv("EUVIEOUVI_INSTANCE_PATH", str(Path.cwd() / "instance")),
        )
    ).strip()
    if not raw_instance_path:
        raise ConfigurationError("EUVIEOUVI_INSTANCE_PATH must not be empty.")
    instance_path = Path(raw_instance_path).expanduser().resolve()

    database_uri = str(
        values.get(
            "DATABASE_URI",
            os.getenv("EUVIEOUVI_DATABASE_URI", f"sqlite:///{instance_path / 'euvieouvi.db'}"),
        )
    ).strip()
    if not database_uri.startswith("sqlite:///"):
        raise ConfigurationError("EUVIEOUVI_DATABASE_URI must use SQLite in this version.")
    database_values = read_database_runtime_settings(database_uri)

    testing = _as_bool(values.get("TESTING", environment == "testing"), name="TESTING")
    explicit_secret = str(values.get("SECRET_KEY", os.getenv("EUVIEOUVI_SECRET_KEY", ""))).strip()
    credential_key = _credential_key(
        instance_path,
        explicit=str(values.get("MASTER_KEY", os.getenv("EUVIEOUVI_MASTER_KEY", ""))).strip(),
        deterministic_seed=explicit_secret if testing else "",
    )
    secret_key = (
        explicit_secret
        or hashlib.sha256(base64.urlsafe_b64decode(credential_key) + b":flask-session").hexdigest()
    )

    log_level = str(
        values.get(
            "LOG_LEVEL",
            database_values.get("system.log_level", os.getenv("EUVIEOUVI_LOG_LEVEL", "INFO")),
        )
    ).upper()
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
        values.get(
            "GUNICORN_THREADS",
            database_values.get(
                "system.gunicorn_threads", os.getenv("EUVIEOUVI_GUNICORN_THREADS", "8")
            ),
        ),
        name="EUVIEOUVI_GUNICORN_THREADS",
        minimum=1,
        maximum=32,
    )
    sqlite_busy_timeout_ms = _as_int(
        values.get(
            "SQLITE_BUSY_TIMEOUT_MS",
            database_values.get(
                "system.sqlite_busy_timeout_ms",
                os.getenv("EUVIEOUVI_SQLITE_BUSY_TIMEOUT_MS", "5000"),
            ),
        ),
        name="EUVIEOUVI_SQLITE_BUSY_TIMEOUT_MS",
        minimum=1,
        maximum=60000,
    )

    timezone = str(
        values.get(
            "TIMEZONE",
            database_values.get(
                "system.timezone", os.getenv("EUVIEOUVI_TIMEZONE", "America/Sao_Paulo")
            ),
        )
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
        database_uri=database_uri,
        sqlite_busy_timeout_ms=sqlite_busy_timeout_ms,
        credential_encryption_key=credential_key,
    )


def read_database_runtime_settings(database_uri: str) -> dict[str, str]:
    """Read the small restart-bound setting set before SQLAlchemy is initialized."""
    if not database_uri.startswith("sqlite:///"):
        return {}
    path = Path(database_uri.removeprefix("sqlite:///") or ".")
    if not path.is_file():
        return {}
    keys = (
        "system.log_level",
        "system.timezone",
        "system.gunicorn_threads",
        "system.sqlite_busy_timeout_ms",
    )
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            placeholders = ",".join("?" for _ in keys)
            rows = connection.execute(
                f"SELECT key,value FROM settings WHERE key IN ({placeholders})",
                keys,
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return {}
    return {str(key): str(value) for key, value in rows}


def _credential_key(instance_path: Path, *, explicit: str, deterministic_seed: str) -> str:
    if deterministic_seed:
        digest = hashlib.sha256(deterministic_seed.encode()).digest()
        return base64.urlsafe_b64encode(digest).decode()
    if explicit:
        _validate_credential_key(explicit)
        return explicit
    instance_path.mkdir(parents=True, exist_ok=True)
    path = instance_path / ".euvieouvi-master.key"
    try:
        value = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        value = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            value = path.read_text(encoding="ascii").strip()
        else:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(value + "\n")
    _validate_credential_key(value)
    try:
        path.chmod(0o600)
    except OSError as error:
        raise ConfigurationError(
            "The credential master key permissions cannot be secured."
        ) from error
    return value


def _validate_credential_key(value: str) -> None:
    try:
        decoded = base64.urlsafe_b64decode(value.encode())
    except (ValueError, TypeError) as error:
        raise ConfigurationError("The credential master key is invalid.") from error
    if len(decoded) != 32:
        raise ConfigurationError("The credential master key is invalid.")


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
