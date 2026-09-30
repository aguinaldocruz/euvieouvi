"""Container bootstrap tests."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from euvieouvi import bootstrap
from euvieouvi.config import Settings


def test_bootstrap_validates_instance_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        environment="production",
        secret_key="secret",
        log_level="INFO",
        testing=False,
        host="0.0.0.0",
        port=8000,
        instance_path=tmp_path / "instance",
        gunicorn_threads=4,
        timezone="America/Sao_Paulo",
        database_uri=f"sqlite:///{tmp_path / 'instance' / 'euvieouvi.db'}",
        sqlite_busy_timeout_ms=5000,
        credential_encryption_key="ZmFrZS10ZXN0LWtleS0xMjM0NTY3ODkwMTIzNDU2Nzg5MDE=",
    )
    prepare = MagicMock()
    monkeypatch.setattr(bootstrap, "load_settings", MagicMock(return_value=settings))
    monkeypatch.setattr(bootstrap, "prepare_instance_path", prepare)
    runtime = MagicMock()
    monkeypatch.setattr(bootstrap, "require_safe_sqlite_runtime", runtime)

    bootstrap.main()

    prepare.assert_called_once_with(settings.instance_path)
    runtime.assert_called_once_with()
