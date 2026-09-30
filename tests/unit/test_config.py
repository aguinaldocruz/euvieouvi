"""Configuration validation tests."""

import sqlite3
import stat
from pathlib import Path

import pytest

from euvieouvi.config import load_settings
from euvieouvi.domain.errors import ConfigurationError


def test_master_key_is_generated_once_with_private_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EUVIEOUVI_SECRET_KEY", raising=False)
    monkeypatch.delenv("EUVIEOUVI_MASTER_KEY", raising=False)

    first = load_settings({"ENVIRONMENT": "production", "INSTANCE_PATH": tmp_path})
    second = load_settings({"ENVIRONMENT": "production", "INSTANCE_PATH": tmp_path})

    key_path = tmp_path / ".euvieouvi-master.key"
    assert first.credential_encryption_key == second.credential_encryption_key
    assert first.secret_key == second.secret_key
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_runtime_settings_are_loaded_from_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "euvieouvi.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE settings (key VARCHAR(255) PRIMARY KEY, value TEXT)")
    connection.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (
            ("system.log_level", "DEBUG"),
            ("system.timezone", "UTC"),
            ("system.gunicorn_threads", "11"),
            ("system.sqlite_busy_timeout_ms", "12000"),
        ),
    )
    connection.commit()
    connection.close()

    settings = load_settings(
        {
            "ENVIRONMENT": "production",
            "INSTANCE_PATH": tmp_path,
            "DATABASE_URI": f"sqlite:///{database}",
        }
    )

    assert settings.log_level == "DEBUG"
    assert settings.timezone == "UTC"
    assert settings.gunicorn_threads == 11
    assert settings.sqlite_busy_timeout_ms == 12000


def test_environment_and_log_level_are_normalized(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "ENVIRONMENT": "DEVELOPMENT",
            "SECRET_KEY": "local-secret",
            "LOG_LEVEL": "debug",
            "TESTING": "false",
            "HOST": "127.0.0.1",
            "PORT": "8080",
            "GUNICORN_THREADS": "6",
            "INSTANCE_PATH": tmp_path / "data",
            "TIMEZONE": "America/Sao_Paulo",
            "SQLITE_BUSY_TIMEOUT_MS": "9000",
        }
    )

    assert settings.environment == "development"
    assert settings.log_level == "DEBUG"
    assert settings.testing is False
    assert settings.host == "127.0.0.1"
    assert settings.port == 8080
    assert settings.gunicorn_threads == 6
    assert settings.instance_path == (tmp_path / "data").resolve()
    assert settings.timezone == "America/Sao_Paulo"
    assert settings.database_uri == f"sqlite:///{tmp_path / 'data' / 'euvieouvi.db'}"
    assert settings.sqlite_busy_timeout_ms == 9000


@pytest.mark.parametrize("value", ["invalid", 1, None])
def test_invalid_testing_value_is_rejected(value: object) -> None:
    with pytest.raises(ConfigurationError, match="TESTING must be a boolean"):
        load_settings(
            {
                "ENVIRONMENT": "testing",
                "SECRET_KEY": "test-secret",
                "TESTING": value,
            }
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("PORT", 0, "EUVIEOUVI_PORT"),
        ("PORT", 65536, "EUVIEOUVI_PORT"),
        ("PORT", "not-a-number", "EUVIEOUVI_PORT"),
        ("GUNICORN_THREADS", 0, "EUVIEOUVI_GUNICORN_THREADS"),
        ("GUNICORN_THREADS", True, "EUVIEOUVI_GUNICORN_THREADS"),
        ("SQLITE_BUSY_TIMEOUT_MS", 0, "EUVIEOUVI_SQLITE_BUSY_TIMEOUT_MS"),
    ],
)
def test_invalid_integer_settings_are_rejected(field: str, value: object, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_settings(
            {
                "ENVIRONMENT": "testing",
                "SECRET_KEY": "test-secret",
                field: value,
            }
        )


@pytest.mark.parametrize("field", ["HOST", "INSTANCE_PATH"])
def test_required_text_settings_reject_empty_values(field: str) -> None:
    with pytest.raises(ConfigurationError, match="must not be empty"):
        load_settings(
            {
                "ENVIRONMENT": "testing",
                "SECRET_KEY": "test-secret",
                field: "   ",
            }
        )


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="valid IANA timezone"):
        load_settings(
            {
                "ENVIRONMENT": "testing",
                "SECRET_KEY": "test-secret",
                "TIMEZONE": "Not/A-Timezone",
            }
        )


def test_non_sqlite_database_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="must use SQLite"):
        load_settings(
            {
                "ENVIRONMENT": "testing",
                "SECRET_KEY": "test-secret",
                "DATABASE_URI": "postgresql://example/test",
            }
        )
