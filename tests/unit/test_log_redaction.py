"""Defensive log redaction tests."""

import logging

from euvieouvi.logging import RequestContextFilter, redact_access_path


def rendered(message: str, *args: object) -> str:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, message, args, None)
    assert RequestContextFilter().filter(record)
    return record.getMessage()


def test_redacts_known_secret_forms_without_changing_safe_context() -> None:
    secret = "super-private-plex-token"
    examples = (
        f"X-Plex-Token={secret}",
        f"Authorization: {secret}",
        f"Authorization: Bearer {secret}",
        f"secret = {secret}",
        f"request http://plex/library?token={secret}&section=1",
        f'HTTP Request: GET https://api.example/item?language=pt-BR&api_key={secret} "200 OK"',
        f"api-key: {secret}",
        "token=%s",
    )
    for message in examples:
        result = rendered(message, secret) if "%s" in message else rendered(message)
        assert secret not in result
        assert "[REDACTED]" in result
    assert rendered("source_id=%s status=%s", 4, "succeeded") == "source_id=4 status=succeeded"


def test_redacts_webhook_credentials_from_access_paths() -> None:
    assert redact_access_path("/webhooks/plex/private-token") == "/webhooks/plex/<redacted>"
    assert redact_access_path("/webhooks/jellyfin/private-token") == "/webhooks/jellyfin/<redacted>"
    assert redact_access_path("/api/v1/media") == "/api/v1/media"
