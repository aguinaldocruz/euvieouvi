"""Authenticated Plex HTTP client with bounded redirects and retries."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from euvieouvi.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorConfigurationError,
    ConnectorConnectionError,
    ConnectorNotFoundError,
    ConnectorRateLimitError,
    ConnectorResponseError,
    ConnectorTimeoutError,
)

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_RETRYABLE_STATUSES = {429, 502, 503, 504}


@dataclass(frozen=True, slots=True)
class PlexPayload:
    content: bytes
    content_type: str


class PlexHttpClient:
    """Small synchronous client that never places the Plex token in a URL."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        application_version: str,
        client_identifier: str,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        retries: int = 2,
        max_redirects: int = 2,
        max_response_bytes: int = 10 * 1024 * 1024,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        if not token.strip():
            raise ConnectorConfigurationError("Plex token must not be empty.")
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ConnectorConfigurationError("Plex timeouts must be positive.")
        if not 0 <= retries <= 5 or not 0 <= max_redirects <= 5:
            raise ConnectorConfigurationError("Plex retry and redirect limits are invalid.")
        self._headers = {
            "Accept": "application/xml, application/json;q=0.9",
            "X-Plex-Token": token,
            "X-Plex-Product": "euvieouvi",
            "X-Plex-Version": application_version,
            "X-Plex-Client-Identifier": client_identifier,
        }
        self._timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._retries = retries
        self._max_redirects = max_redirects
        self._max_response_bytes = max_response_bytes
        self._client = client or httpx.Client(follow_redirects=False, trust_env=False)
        self._owns_client = client is None
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get(self, path: str, *, params: Mapping[str, str | int] | None = None) -> PlexPayload:
        url = self._build_url(path)
        for attempt in range(self._retries + 1):
            try:
                response = self._request_with_redirects(url, params=params)
            except httpx.TimeoutException as error:
                if attempt < self._retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise ConnectorTimeoutError("Plex request timed out.") from error
            except httpx.RequestError as error:
                if attempt < self._retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise ConnectorConnectionError("Plex request failed.") from error

            if response.status_code in _RETRYABLE_STATUSES and attempt < self._retries:
                self._sleep(self._retry_delay(response, attempt))
                continue
            self._raise_for_status(response)
            if len(response.content) > self._max_response_bytes:
                raise ConnectorResponseError("Plex response exceeded the configured size limit.")
            return PlexPayload(
                content=response.content,
                content_type=response.headers.get("content-type", ""),
            )
        raise ConnectorConnectionError("Plex request exhausted retries.")

    def _request_with_redirects(
        self, url: str, *, params: Mapping[str, str | int] | None
    ) -> httpx.Response:
        current_url = url
        current_params = params
        for redirect_count in range(self._max_redirects + 1):
            response = self._send_bounded(current_url, params=current_params)
            if response.status_code not in _REDIRECT_STATUSES:
                return response
            if redirect_count == self._max_redirects:
                raise ConnectorResponseError("Plex redirect limit exceeded.")
            location = response.headers.get("location")
            if not location:
                raise ConnectorResponseError("Plex redirect omitted its destination.")
            redirected_url = urljoin(current_url, location)
            if not _same_origin(self.base_url, redirected_url):
                raise ConnectorResponseError("Plex redirect changed the configured origin.")
            current_url = redirected_url
            current_params = None
        raise ConnectorResponseError("Plex redirect handling failed.")

    def _send_bounded(self, url: str, *, params: Mapping[str, str | int] | None) -> httpx.Response:
        with self._client.stream(
            "GET",
            url,
            params=params,
            headers=self._headers,
            timeout=self._timeout,
            follow_redirects=False,
        ) as response:
            if response.status_code in _REDIRECT_STATUSES:
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    request=response.request,
                )
            chunks: list[bytes] = []
            total_bytes = 0
            for chunk in response.iter_bytes():
                total_bytes += len(chunk)
                if total_bytes > self._max_response_bytes:
                    raise ConnectorResponseError(
                        "Plex response exceeded the configured size limit."
                    )
                chunks.append(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                request=response.request,
            )

    def _build_url(self, path: str) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise ConnectorConfigurationError("Plex request path must be absolute and local.")
        url = urljoin(f"{self.base_url}/", path.lstrip("/"))
        if not _same_origin(self.base_url, url):
            raise ConnectorConfigurationError("Plex request path changed the configured origin.")
        return url

    @staticmethod
    def _backoff(attempt: int) -> float:
        jitter = float(random.uniform(0, 0.1))
        return float(min(0.25 * (2**attempt) + jitter, 2.0))

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = _parse_retry_after(response.headers.get("retry-after"))
        return retry_after if retry_after is not None else self._backoff(attempt)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status in {401, 403}:
            raise ConnectorAuthenticationError("Plex authentication was rejected.")
        if status == 404:
            raise ConnectorNotFoundError("Plex resource was not found.")
        if status == 429:
            raise ConnectorRateLimitError(
                "Plex rate limit was reached.",
                retry_after_seconds=_parse_retry_after(response.headers.get("retry-after")),
            )
        if status >= 500:
            raise ConnectorConnectionError(f"Plex returned server status {status}.")
        if status < 200 or status >= 300:
            raise ConnectorResponseError(f"Plex returned unexpected status {status}.")


def normalize_base_url(value: str) -> str:
    """Normalize an operator-supplied Plex origin without weakening private-network use."""
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as error:
        raise ConnectorConfigurationError("Plex URL contains an invalid port.") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConnectorConfigurationError("Plex URL must use HTTP or HTTPS and include a host.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConnectorConfigurationError(
            "Plex URL must not contain credentials, query, or fragment."
        )
    if port is not None and not 1 <= port <= 65535:
        raise ConnectorConfigurationError("Plex URL contains an invalid port.")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _same_origin(base_url: str, candidate: str) -> bool:
    base = urlsplit(base_url)
    other = urlsplit(candidate)
    return (base.scheme, base.hostname, base.port) == (other.scheme, other.hostname, other.port)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return min(max(result, 0.0), 60.0)
