"""Configuration validation tests."""

import pytest

from euvieouvi.config import load_settings
from euvieouvi.domain.errors import ConfigurationError


def test_secret_key_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EUVIEOUVI_SECRET_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="EUVIEOUVI_SECRET_KEY is required"):
        load_settings({"ENVIRONMENT": "production"})


def test_environment_and_log_level_are_normalized() -> None:
    settings = load_settings(
        {
            "ENVIRONMENT": "DEVELOPMENT",
            "SECRET_KEY": "local-secret",
            "LOG_LEVEL": "debug",
            "TESTING": "false",
        }
    )

    assert settings.environment == "development"
    assert settings.log_level == "DEBUG"
    assert settings.testing is False


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
