"""Plex HTTP boundary tests with no network access."""

import gzip
from collections.abc import Callable

import httpx
import pytest

from euvieouvi.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorConfigurationError,
    ConnectorConnectionError,
    ConnectorNotFoundError,
    ConnectorRateLimitError,
    ConnectorResponseError,
    ConnectorTimeoutError,
)
from euvieouvi.connectors.plex.client import PlexHttpClient, normalize_base_url


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    **options: object,
) -> PlexHttpClient:
    return PlexHttpClient(
        "http://plex.local:32400/",
        "sanitized-fixture-token",
        application_version="2.0.0.dev0",
        client_identifier="fixture-client",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        **options,  # type: ignore[arg-type]
    )


def test_authentication_uses_header_and_never_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Plex-Token"] == "sanitized-fixture-token"
        assert "token" not in str(request.url).lower()
        assert request.headers["X-Plex-Product"] == "euvieouvi"
        return httpx.Response(200, content=b"<MediaContainer />", request=request)

    payload = make_client(handler).get("/library/sections", params={"size": 10})
    assert payload.content == b"<MediaContainer />"


def test_compressed_response_is_not_decoded_twice() -> None:
    document = b'<MediaContainer friendlyName="Plex" machineIdentifier="server-1" />'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(gzip.compress(document))),
                "Content-Type": "application/xml",
            },
            content=gzip.compress(document),
            request=request,
        )

    payload = make_client(handler).get("/")

    assert payload.content == document
    assert payload.content_type == "application/xml"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://plex.local", "HTTP or HTTPS"),
        ("http://user:pass@plex.local", "credentials"),
        ("http://plex.local/path?token=secret", "query"),
        ("http://plex.local:99999", "invalid port"),
    ],
)
def test_base_url_validation(url: str, message: str) -> None:
    with pytest.raises(ConnectorConfigurationError, match=message):
        normalize_base_url(url)


def test_same_origin_redirect_is_allowed_but_cross_origin_is_blocked() -> None:
    def allowed(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"}, request=request)
        return httpx.Response(200, content=b"ok", request=request)

    assert make_client(allowed).get("/start").content == b"ok"

    def blocked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"Location": "http://other.local/steal"}, request=request
        )

    with pytest.raises(ConnectorResponseError, match="origin"):
        make_client(blocked).get("/start")


def test_timeout_is_retried_then_classified() -> None:
    attempts: list[int] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("fixture timeout", request=request)

    client = make_client(handler, retries=1, sleep=delays.append)
    with pytest.raises(ConnectorTimeoutError):
        client.get("/")
    assert len(attempts) == 2
    assert len(delays) == 1


def test_authentication_and_rate_limit_have_distinct_errors() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    with pytest.raises(ConnectorAuthenticationError):
        make_client(unauthorized, retries=0).get("/")

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"}, request=request)

    with pytest.raises(ConnectorRateLimitError) as captured:
        make_client(limited, retries=0).get("/")
    assert captured.value.retry_after_seconds == 7


def test_response_size_and_request_path_are_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"too large", request=request)

    with pytest.raises(ConnectorResponseError, match="size limit"):
        make_client(handler, max_response_bytes=2).get("/")
    with pytest.raises(ConnectorConfigurationError, match="path"):
        make_client(handler).get("//other.local/path")


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (404, ConnectorNotFoundError),
        (500, ConnectorConnectionError),
        (400, ConnectorResponseError),
    ],
)
def test_http_statuses_are_classified(status: int, error_type: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request)

    with pytest.raises(error_type):
        make_client(handler, retries=0).get("/")


def test_transport_failure_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fixture connection failure", request=request)

    with pytest.raises(ConnectorConnectionError):
        make_client(handler, retries=0).get("/")


def test_redirect_requires_location_and_obeys_limit() -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, request=request)

    with pytest.raises(ConnectorResponseError, match="omitted"):
        make_client(missing).get("/")

    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/again"}, request=request)

    with pytest.raises(ConnectorResponseError, match="limit"):
        make_client(redirect, max_redirects=0).get("/")


def test_invalid_client_limits_and_owned_client_close() -> None:
    with pytest.raises(ConnectorConfigurationError, match="token"):
        PlexHttpClient(
            "http://plex.local",
            " ",
            application_version="2",
            client_identifier="test",
        )
    with pytest.raises(ConnectorConfigurationError, match="timeouts"):
        PlexHttpClient(
            "http://plex.local",
            "token",
            application_version="2",
            client_identifier="test",
            read_timeout=0,
        )
    client = PlexHttpClient(
        "http://plex.local",
        "token",
        application_version="2",
        client_identifier="test",
    )
    client.close()
