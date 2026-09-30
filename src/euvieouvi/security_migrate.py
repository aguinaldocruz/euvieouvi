"""One-shot startup migration for legacy plaintext credentials."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from euvieouvi import create_app
from euvieouvi.credentials import migrate_persisted_credentials
from euvieouvi.database.models import Setting
from euvieouvi.extensions import db

_RUNTIME_SETTINGS = {
    "system.log_level": "LOG_LEVEL",
    "system.timezone": "TIMEZONE",
    "system.gunicorn_threads": "GUNICORN_THREADS",
    "system.sqlite_busy_timeout_ms": "SQLITE_BUSY_TIMEOUT_MS",
}


def ensure_runtime_settings(session: Session, config: Mapping[str, Any]) -> int:
    """Persist validated defaults so future starts no longer depend on environment values."""
    created = 0
    for key, config_key in _RUNTIME_SETTINGS.items():
        if session.get(Setting, key) is not None:
            continue
        session.add(Setting(key=key, value=str(config[config_key])))
        created += 1
    if created:
        session.commit()
    return created


def main() -> None:
    app = create_app({"DISABLE_BACKGROUND_SERVICES": True})
    with app.app_context():
        sources, settings = migrate_persisted_credentials(db.session())
        runtime_settings = ensure_runtime_settings(db.session(), app.config)
    print(
        "euvieouvi startup settings migration succeeded: "
        f"encrypted_sources={sources} encrypted_settings={settings} "
        f"runtime_settings_created={runtime_settings}",
        flush=True,
    )


if __name__ == "__main__":
    main()
