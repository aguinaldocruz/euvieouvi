"""Authenticated encryption for persisted connector and application credentials."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from euvieouvi.database.models import Setting, Source
from euvieouvi.domain.errors import ConfigurationError

_PREFIX = "enc:v1:"
SENSITIVE_SETTING_KEYS = frozenset(
    {
        "metadata.tmdb.token",
        "webhook.plex.token",
        "webhook.jellyfin.token",
    }
)


def encrypt_text(value: str) -> str:
    """Encrypt plaintext once; already encrypted values remain unchanged."""
    if value.startswith(_PREFIX):
        return value
    token = _cipher().encrypt(value.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt_text(value: str) -> str:
    """Decrypt v1 ciphertext while accepting legacy plaintext during migration."""
    if not value.startswith(_PREFIX):
        return value
    try:
        return _cipher().decrypt(value.removeprefix(_PREFIX).encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as error:
        raise ConfigurationError(
            "Stored credentials cannot be decrypted with this installation's master key."
        ) from error


def is_encrypted(value: str) -> bool:
    return value.startswith(_PREFIX)


def setting_value(setting: Setting | None) -> str | None:
    if setting is None:
        return None
    return decrypt_text(setting.value) if setting.key in SENSITIVE_SETTING_KEYS else setting.value


def save_setting_value(session: Session, key: str, value: str) -> Setting:
    persisted = encrypt_text(value) if key in SENSITIVE_SETTING_KEYS else value
    setting = session.get(Setting, key)
    if setting is None:
        setting = Setting(key=key, value=persisted)
        session.add(setting)
    else:
        setting.value = persisted
    return setting


def migrate_persisted_credentials(session: Session) -> tuple[int, int]:
    """Encrypt legacy plaintext secrets in place; safe to execute at every startup."""
    sources = 0
    settings = 0
    for source in session.scalars(select(Source)):
        if not is_encrypted(source.secret):
            source.secret = encrypt_text(source.secret)
            sources += 1
    for setting in session.scalars(select(Setting).where(Setting.key.in_(SENSITIVE_SETTING_KEYS))):
        if not is_encrypted(setting.value):
            setting.value = encrypt_text(setting.value)
            settings += 1
    if sources or settings:
        session.commit()
    return sources, settings


def _cipher() -> Fernet:
    value = current_app.config.get("CREDENTIAL_ENCRYPTION_KEY")
    if not isinstance(value, str) or not value:
        raise ConfigurationError("Credential encryption is not configured.")
    try:
        return Fernet(value.encode("ascii"))
    except (TypeError, ValueError) as error:
        raise ConfigurationError("Credential encryption is not configured correctly.") from error
