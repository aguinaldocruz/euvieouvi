"""Plex hosted PIN authorization tests."""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from euvieouvi.connectors.errors import ConnectorResponseError
from euvieouvi.plex_auth import PlexPinClient


def test_pin_flow_starts_builds_hosted_url_and_claims_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"id": 42, "code": "PIN-CODE"})
        return httpx.Response(200, json={"id": 42, "authToken": "plex-token"})

    client = PlexPinClient(
        client_identifier="stable-client-id",
        application_version="2.0",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    pin = client.start()
    destination = client.authorization_url(
        pin, forward_url="https://example.test/settings/plex/auth/complete"
    )

    assert pin.identifier == 42 and pin.code == "PIN-CODE"
    assert client.claim(pin.identifier) == "plex-token"
    assert requests[0].headers["X-Plex-Client-Identifier"] == "stable-client-id"
    assert parse_qs(urlsplit(destination).fragment.removeprefix("?"))["code"] == ["PIN-CODE"]


def test_pin_flow_rejects_malformed_response() -> None:
    client = PlexPinClient(
        client_identifier="stable-client-id",
        application_version="2.0",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"id": 1}))
        ),
    )

    with pytest.raises(ConnectorResponseError, match="invalid authorization PIN"):
        client.start()
