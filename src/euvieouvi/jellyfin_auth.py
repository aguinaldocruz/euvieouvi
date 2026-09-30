"""Jellyfin Quick Connect authentication without persisted passwords or API keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from euvieouvi.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorConnectionError,
    ConnectorResponseError,
    ConnectorTimeoutError,
)
from euvieouvi.connectors.jellyfin.client import normalize_base_url


@dataclass(frozen=True, slots=True)
class JellyfinQuickConnectRequest:
    secret: str
    code: str


@dataclass(frozen=True, slots=True)
class JellyfinAuthentication:
    access_token: str
    user_id: str
    user_name: str


class JellyfinQuickConnectClient:
    """Bounded client for Jellyfin's device authorization flow."""

    def __init__(
        self,
        base_url: str,
        *,
        device_id: str,
        application_version: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": (
                'MediaBrowser Client="euvieouvi", Device="Server", '
                f'DeviceId="{device_id}", Version="{application_version}"'
            ),
        }
        self._client = client or httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(15, connect=5),
            trust_env=False,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def enabled(self) -> bool:
        response = self._request("GET", "/QuickConnect/Enabled")
        try:
            value = response.json()
        except ValueError as error:
            raise ConnectorResponseError("Jellyfin returned invalid Quick Connect JSON.") from error
        if not isinstance(value, bool):
            raise ConnectorResponseError("Jellyfin returned an invalid Quick Connect status.")
        return value

    def initiate(self) -> JellyfinQuickConnectRequest:
        payload = self._json_request("POST", "/QuickConnect/Initiate", json={})
        secret = payload.get("Secret") or payload.get("secret")
        code = payload.get("Code") or payload.get("code")
        if not isinstance(secret, str) or not secret.strip():
            raise ConnectorResponseError("Jellyfin omitted the Quick Connect secret.")
        if not isinstance(code, str) or not code.strip():
            raise ConnectorResponseError("Jellyfin omitted the Quick Connect code.")
        return JellyfinQuickConnectRequest(secret.strip(), code.strip())

    def is_authenticated(self, secret: str) -> bool:
        payload = self._json_request(
            "GET", "/QuickConnect/Connect", params={"Secret": secret}
        )
        value = payload.get("Authenticated")
        if value is None:
            value = payload.get("authenticated")
        if not isinstance(value, bool):
            raise ConnectorResponseError("Jellyfin returned an invalid authorization state.")
        return value

    def authenticate(self, secret: str) -> JellyfinAuthentication:
        payload = self._json_request(
            "POST",
            "/Users/AuthenticateWithQuickConnect",
            json={"Secret": secret},
        )
        access_token = payload.get("AccessToken") or payload.get("accessToken")
        user = payload.get("User") or payload.get("user")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ConnectorResponseError("Jellyfin omitted the access token.")
        if not isinstance(user, dict):
            raise ConnectorResponseError("Jellyfin omitted the authenticated user.")
        user_id = user.get("Id") or user.get("id")
        user_name = user.get("Name") or user.get("name")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ConnectorResponseError("Jellyfin omitted the authenticated user ID.")
        if not isinstance(user_name, str) or not user_name.strip():
            raise ConnectorResponseError("Jellyfin omitted the authenticated user name.")
        return JellyfinAuthentication(
            access_token.strip(),
            user_id.strip().replace("-", "").lower(),
            user_name.strip(),
        )

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._request(method, path, params=params, json=json)
        try:
            value = response.json()
        except ValueError as error:
            raise ConnectorResponseError("Jellyfin returned invalid Quick Connect JSON.") from error
        if not isinstance(value, dict):
            raise ConnectorResponseError("Jellyfin returned an invalid Quick Connect response.")
        return value

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                f"{self.base_url}{path.lstrip('/')}",
                params=params,
                json=json,
                headers=self._headers,
            )
        except httpx.TimeoutException as error:
            raise ConnectorTimeoutError("Jellyfin Quick Connect timed out.") from error
        except httpx.RequestError as error:
            raise ConnectorConnectionError(
                "Jellyfin Quick Connect could not be reached."
            ) from error
        if response.status_code in {401, 403}:
            raise ConnectorAuthenticationError(
                "Jellyfin rejected Quick Connect or it is disabled."
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise ConnectorResponseError(
                f"Jellyfin Quick Connect returned HTTP {response.status_code}."
            )
        return response
