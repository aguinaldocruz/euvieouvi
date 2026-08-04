"""Container healthcheck client tests."""

from unittest.mock import MagicMock

import pytest

from euvieouvi import healthcheck


def test_healthcheck_succeeds_for_http_200(monkeypatch: pytest.MonkeyPatch) -> None:
    response = MagicMock()
    response.__enter__.return_value.status = 200
    monkeypatch.setattr(healthcheck, "urlopen", MagicMock(return_value=response))

    assert healthcheck.main() == 0


def test_healthcheck_fails_when_service_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck, "urlopen", MagicMock(side_effect=OSError("offline")))

    assert healthcheck.main() == 1


def test_healthcheck_fails_for_non_success_status(monkeypatch: pytest.MonkeyPatch) -> None:
    response = MagicMock()
    response.__enter__.return_value.status = 503
    monkeypatch.setattr(healthcheck, "urlopen", MagicMock(return_value=response))

    assert healthcheck.main() == 1
