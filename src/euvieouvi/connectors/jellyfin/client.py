"""Authenticated bounded Jellyfin HTTP client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from euvieouvi.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorConfigurationError,
    ConnectorConnectionError,
    ConnectorNotFoundError,
    ConnectorResponseError,
    ConnectorTimeoutError,
)


class JellyfinHttpClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        max_response_bytes: int = 10 * 1024 * 1024,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        if not api_key.strip():
            raise ConnectorConfigurationError("Jellyfin API key must not be empty.")
        self._headers = {
            "Accept": "application/json",
            "X-Emby-Token": api_key.strip(),
            "Authorization": (
                'MediaBrowser Client="euvieouvi", Device="Server", '
                'DeviceId="euvieouvi", Version="2"'
            ),
        }
        self._timeout = httpx.Timeout(timeout, connect=min(timeout, 5.0))
        self._max_response_bytes = max_response_bytes
        self._client = client or httpx.Client(follow_redirects=False, trust_env=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get_json(
        self, path: str, *, params: Mapping[str, str | int | bool] | None = None
    ) -> dict[str, Any] | list[Any]:
        response = self._get(path, params=params)
        try:
            value = response.json()
        except ValueError as error:
            raise ConnectorResponseError("Jellyfin returned invalid JSON.") from error
        if not isinstance(value, (dict, list)):
            raise ConnectorResponseError("Jellyfin returned an unexpected JSON value.")
        return value

    def post_empty(
        self,
        path: str,
        *,
        params: Mapping[str, str | int | bool] | None = None,
    ) -> None:
        if not path.startswith("/") or path.startswith("//"):
            raise ConnectorConfigurationError("Jellyfin path must be server-local.")
        url = urljoin(self.base_url, path.lstrip("/"))
        try:
            response = self._client.post(
                url,
                params=params,
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as error:
            raise ConnectorTimeoutError("Jellyfin request timed out.") from error
        except httpx.RequestError as error:
            raise ConnectorConnectionError("Jellyfin request failed.") from error
        if response.status_code in {401, 403}:
            raise ConnectorAuthenticationError("Jellyfin rejected the API key.")
        if response.status_code == 404:
            raise ConnectorNotFoundError("Jellyfin resource was not found.")
        if response.status_code >= 400:
            raise ConnectorResponseError("Jellyfin returned an unsuccessful response.")

    def get_image(
        self,
        path: str,
        *,
        width: int,
        height: int,
    ) -> tuple[bytes, str]:
        response = self._get(
            path,
            params={"maxWidth": width, "maxHeight": height, "quality": 90},
        )
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ConnectorResponseError("Jellyfin returned an unsupported image.")
        return response.content, mime_type

    def resolve_user_id(self, name_or_id: str) -> str:
        candidate = name_or_id.strip()
        # GUID is 32 hex chars (with or without dashes) - use directly
        normalized = candidate.replace("-", "")
        if len(normalized) == 32 and all(c in "0123456789abcdefABCDEF" for c in normalized):
            return normalized.lower()
        # otherwise treat as username and lookup via /Users
        raw = self.get_json("/Users")
        if not isinstance(raw, list):
            raise ConnectorResponseError("Jellyfin user lookup failed.")
        lowered = candidate.casefold()
        for entry in raw:
            if isinstance(entry, dict) and str(entry.get("Name", "")).casefold() == lowered:
                user_id = entry.get("Id")
                if isinstance(user_id, str) and user_id.strip():
                    return user_id.strip().replace("-", "").lower()
        raise ConnectorNotFoundError(f"Jellyfin user '{candidate}' not found.")

    def _get(self, path: str, *, params: Mapping[str, str | int | bool] | None) -> httpx.Response:
        if not path.startswith("/") or path.startswith("//"):
            raise ConnectorConfigurationError("Jellyfin path must be server-local.")
        url = urljoin(self.base_url, path.lstrip("/"))
        try:
            response = self._client.get(
                url,
                params=params,
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as error:
            raise ConnectorTimeoutError("Jellyfin request timed out.") from error
        except httpx.RequestError as error:
            raise ConnectorConnectionError("Jellyfin request failed.") from error
        if response.status_code in {401, 403}:
            raise ConnectorAuthenticationError("Jellyfin rejected the API key.")
        if response.status_code == 404:
            raise ConnectorNotFoundError("Jellyfin resource was not found.")
        if response.status_code >= 400:
            raise ConnectorResponseError("Jellyfin returned an unsuccessful response.")
        if len(response.content) > self._max_response_bytes:
            raise ConnectorResponseError("Jellyfin response exceeded the size limit.")
        return response


def _normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConnectorConfigurationError("Invalid Jellyfin base URL.")
    path = parsed.path.rstrip("/") + "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
