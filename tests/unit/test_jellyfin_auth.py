"""Jellyfin Quick Connect boundary tests."""

import httpx
import pytest

from euvieouvi.connectors.errors import ConnectorAuthenticationError, ConnectorResponseError
from euvieouvi.jellyfin_auth import JellyfinQuickConnectClient


def test_quick_connect_authorizes_device_and_returns_user_token() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        authorization = request.headers["Authorization"]
        assert 'DeviceId="persistent-device"' in authorization
        if request.url.path == "/base/QuickConnect/Enabled":
            return httpx.Response(200, json=True, request=request)
        if request.url.path == "/base/QuickConnect/Initiate":
            return httpx.Response(
                200, json={"Secret": "temporary", "Code": "654321"}, request=request
            )
        if request.url.path == "/base/QuickConnect/Connect":
            assert request.url.params["Secret"] == "temporary"
            return httpx.Response(200, json={"Authenticated": True}, request=request)
        assert request.url.path == "/base/Users/AuthenticateWithQuickConnect"
        assert request.read() == b'{"Secret":"temporary"}'
        return httpx.Response(
            200,
            json={
                "AccessToken": "user-token",
                "User": {
                    "Id": "01234567-89ab-cdef-0123-456789abcdef",
                    "Name": "Viewer",
                },
            },
            request=request,
        )

    client = JellyfinQuickConnectClient(
        "http://jellyfin.local/base",
        device_id="persistent-device",
        application_version="2.0",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.enabled() is True
    pending = client.initiate()
    assert pending.code == "654321" and pending.secret == "temporary"
    assert client.is_authenticated(pending.secret) is True
    authentication = client.authenticate(pending.secret)
    assert authentication.access_token == "user-token"
    assert authentication.user_id == "0123456789abcdef0123456789abcdef"
    assert authentication.user_name == "Viewer"
    assert calls == [
        "/base/QuickConnect/Enabled",
        "/base/QuickConnect/Initiate",
        "/base/QuickConnect/Connect",
        "/base/Users/AuthenticateWithQuickConnect",
    ]


def test_quick_connect_rejects_authentication_and_invalid_payloads() -> None:
    def rejected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    client = JellyfinQuickConnectClient(
        "http://jellyfin.local",
        device_id="device",
        application_version="2.0",
        client=httpx.Client(transport=httpx.MockTransport(rejected)),
    )
    with pytest.raises(ConnectorAuthenticationError):
        client.enabled()

    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Authenticated": "yes"}, request=request)

    client = JellyfinQuickConnectClient(
        "http://jellyfin.local",
        device_id="device",
        application_version="2.0",
        client=httpx.Client(transport=httpx.MockTransport(malformed)),
    )
    with pytest.raises(ConnectorResponseError):
        client.is_authenticated("temporary")
