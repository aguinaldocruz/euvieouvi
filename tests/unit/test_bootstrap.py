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
    )
    prepare = MagicMock()
    monkeypatch.setattr(bootstrap, "load_settings", MagicMock(return_value=settings))
    monkeypatch.setattr(bootstrap, "prepare_instance_path", prepare)

    bootstrap.main()

    prepare.assert_called_once_with(settings.instance_path)
