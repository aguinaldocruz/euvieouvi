"""Credential encryption and legacy migration tests."""

import pytest
from flask import Flask

from euvieouvi.credentials import (
    decrypt_text,
    encrypt_text,
    is_encrypted,
    migrate_persisted_credentials,
    setting_value,
)
from euvieouvi.database.enums import ConnectorType
from euvieouvi.database.models import Setting, Source
from euvieouvi.domain.errors import ConfigurationError
from euvieouvi.extensions import db
from euvieouvi.security_migrate import ensure_runtime_settings


def test_authenticated_encryption_round_trip_and_tamper_detection(app: Flask) -> None:
    with app.app_context():
        encrypted = encrypt_text("very-secret-token")
        assert is_encrypted(encrypted)
        assert "very-secret-token" not in encrypted
        assert decrypt_text(encrypted) == "very-secret-token"

        replacement = "A" if encrypted[-1] != "A" else "B"
        with pytest.raises(ConfigurationError, match="cannot be decrypted"):
            decrypt_text(encrypted[:-1] + replacement)


def test_legacy_credentials_are_migrated_idempotently(app: Flask) -> None:
    with app.app_context():
        source = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex:32400",
            secret="plex-token",
            enabled=True,
        )
        tmdb = Setting(key="metadata.tmdb.token", value="tmdb-token")
        ordinary = Setting(key="ui.language", value="en")
        db.session.add_all((source, tmdb, ordinary))
        db.session.commit()

        assert migrate_persisted_credentials(db.session()) == (1, 1)
        assert migrate_persisted_credentials(db.session()) == (0, 0)
        assert decrypt_text(source.secret) == "plex-token"
        assert setting_value(tmdb) == "tmdb-token"
        assert ordinary.value == "en"
        assert "plex-token" not in source.secret
        assert "tmdb-token" not in tmdb.value


def test_runtime_defaults_are_persisted_once(app: Flask) -> None:
    with app.app_context():
        assert ensure_runtime_settings(db.session(), app.config) == 4
        app.config["LOG_LEVEL"] = "ERROR"
        assert ensure_runtime_settings(db.session(), app.config) == 0
        setting = db.session.get(Setting, "system.log_level")
        assert setting is not None and setting.value == "DEBUG"
