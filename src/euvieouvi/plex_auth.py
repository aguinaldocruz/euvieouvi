"""Hosted Plex account authorization using Plex's PIN flow."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from euvieouvi.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorConnectionError,
    ConnectorResponseError,
    ConnectorTimeoutError,
)

_PLEX_API = "https://plex.tv/api/v2"
_PLEX_AUTH = "https://app.plex.tv/auth"


@dataclass(frozen=True, slots=True)
class PlexPin:
    """Short-lived Plex authorization request."""

    identifier: int
    code: str


class PlexPinClient:
    """Bounded client for starting and claiming Plex PIN authorizations."""

    def __init__(
        self,
        *,
        client_identifier: str,
        application_version: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.client_identifier = client_identifier
        self._headers = {
            "Accept": "application/json",
            "X-Plex-Product": "euvieouvi",
            "X-Plex-Version": application_version,
            "X-Plex-Client-Identifier": client_identifier,
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

    def start(self) -> PlexPin:
        payload = self._request("POST", "/pins", params={"strong": "true"})
        identifier = payload.get("id")
        code = payload.get("code")
        if not isinstance(identifier, int) or not isinstance(code, str) or not code:
            raise ConnectorResponseError("Plex returned an invalid authorization PIN.")
        return PlexPin(identifier=identifier, code=code)

    def claim(self, pin_identifier: int) -> str | None:
        payload = self._request("GET", f"/pins/{pin_identifier}")
        token = payload.get("authToken") or payload.get("auth_token")
        if token is None:
            return None
        if not isinstance(token, str) or not token.strip():
            raise ConnectorResponseError("Plex returned an invalid authorization token.")
        return token.strip()

    def authorization_url(self, pin: PlexPin, *, forward_url: str) -> str:
        query = urlencode(
            {
                "clientID": self.client_identifier,
                "code": pin.code,
                "forwardUrl": forward_url,
                "context[device][product]": "euvieouvi",
            }
        )
        return f"{_PLEX_AUTH}#?{query}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, object]:
        try:
            response = self._client.request(
                method,
                f"{_PLEX_API}{path}",
                params=params,
                headers=self._headers,
            )
        except httpx.TimeoutException as error:
            raise ConnectorTimeoutError("Plex authorization timed out.") from error
        except httpx.RequestError as error:
            raise ConnectorConnectionError("Plex authorization could not be reached.") from error
        if response.status_code in {401, 403}:
            raise ConnectorAuthenticationError("Plex rejected the authorization request.")
        if response.status_code < 200 or response.status_code >= 300:
            raise ConnectorResponseError(
                f"Plex authorization returned HTTP {response.status_code}."
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise ConnectorResponseError("Plex authorization returned invalid JSON.") from error
        if not isinstance(payload, dict):
            raise ConnectorResponseError("Plex authorization returned an invalid response.")
        return payload
