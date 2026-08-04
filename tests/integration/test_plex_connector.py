"""Contract tests for the database-independent Plex connector."""

from pathlib import Path

import httpx
import pytest

from euvieouvi.connectors.base import MediaConnector
from euvieouvi.connectors.dtos import (
    ExternalLibraryRef,
    ExternalLibraryType,
    ExternalMediaKind,
    PageRequest,
)
from euvieouvi.connectors.errors import ConnectorPaginationError, ConnectorResponseError
from euvieouvi.connectors.plex.client import PlexHttpClient
from euvieouvi.connectors.plex.connector import PlexConnector

FIXTURES = Path(__file__).parents[1] / "fixtures" / "plex"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_connector(handler: httpx.MockTransport) -> PlexConnector:
    http_client = PlexHttpClient(
        "http://plex.local:32400",
        "sanitized-fixture-token",
        application_version="2.0.0.dev0",
        client_identifier="contract-test",
        retries=0,
        client=httpx.Client(transport=handler),
    )
    return PlexConnector(http_client)


def test_connection_and_library_discovery_map_neutral_values() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        name = "connection.xml" if request.url.path == "/" else "libraries.xml"
        return httpx.Response(200, content=fixture(name), request=request)

    connector = make_connector(httpx.MockTransport(route))
    protocol_connector: MediaConnector = connector
    connection = connector.test_connection()
    libraries = connector.list_libraries()

    assert connection.server_name == "Test Plex"
    assert protocol_connector is connector
    assert connection.server_identifier == "server-fixture-001"
    assert connection.capabilities == frozenset({"history", "library"})
    assert [(item.external_id, item.media_type.value) for item in libraries] == [
        ("1", "movie"),
        ("2", "show"),
    ]
    assert connector.last_unsupported_libraries[0].source_type == "artist"
    assert connector.last_unsupported_libraries[0].reason == "unsupported_library_type"


def test_movie_page_maps_ids_state_and_defensive_pagination() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        assert request.url.params["X-Plex-Container-Start"] == "0"
        assert request.url.params["X-Plex-Container-Size"] == "2"
        return httpx.Response(200, content=fixture("movies.xml"), request=request)

    connector = make_connector(httpx.MockTransport(route))
    page = connector.get_media_page(
        ExternalLibraryRef("1", ExternalLibraryType.MOVIE),
        ExternalMediaKind.MOVIE,
        PageRequest(start=0, size=2),
    )

    assert page.has_more is True
    assert page.next_start == 2
    assert page.total_size == 4
    assert page.items[0].view_count == 2
    assert [(item.provider, item.external_id) for item in page.items[0].identifiers] == [
        ("imdb", "tt2543164"),
        ("tmdb", "329865"),
    ]
    assert page.items[1].view_count is None


def test_episode_json_maps_hierarchy_and_optional_fields() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        assert request.url.params["type"] == "4"
        return httpx.Response(
            200,
            content=fixture("episodes.json"),
            headers={"Content-Type": "application/json"},
            request=request,
        )

    page = make_connector(httpx.MockTransport(route)).get_media_page(
        ExternalLibraryRef("2", ExternalLibraryType.SHOW),
        ExternalMediaKind.EPISODE,
        PageRequest(size=200),
    )
    episode = page.items[0]
    assert episode.show_external_id == "7000"
    assert episode.show_title == "Futurama"
    assert (episode.season_number, episode.episode_number) == (1, 1)
    assert page.has_more is False


def test_history_maps_only_actual_occurrence() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        assert request.url.params["librarySectionID"] == "1"
        return httpx.Response(200, content=fixture("history.xml"), request=request)

    page = make_connector(httpx.MockTransport(route)).get_history_page(
        ExternalLibraryRef("1", ExternalLibraryType.MOVIE),
        None,
        PageRequest(),
    )
    event = page.items[0]
    assert event.source_event_id == "history-500"
    assert event.completed is True
    assert event.view_number is None


def test_history_skips_orphans_without_losing_raw_pagination() -> None:
    valid = "".join(
        f'<Video ratingKey="{index}" historyKey="/history/{index}" '
        f'viewedAt="{1_700_000_000 + index}" type="movie" title="Movie {index}" />'
        for index in range(178)
    )
    orphaned = "".join(
        f'<Video historyKey="/history/orphan-{index}" '
        f'viewedAt="{1_700_001_000 + index}" type="movie" title="Orphan {index}" />'
        for index in range(22)
    )
    content = (
        f'<MediaContainer offset="0" totalSize="400">{valid}{orphaned}</MediaContainer>'
    ).encode()

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, request=request)

    page = make_connector(httpx.MockTransport(route)).get_history_page(
        ExternalLibraryRef("1", ExternalLibraryType.MOVIE),
        None,
        PageRequest(size=200),
    )

    assert len(page.items) == 178
    assert page.size == 178
    assert page.next_start == 200
    assert page.has_more is True


def test_history_advances_across_a_fully_orphaned_page() -> None:
    requested_starts: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        start = request.url.params["X-Plex-Container-Start"]
        requested_starts.append(start)
        if start == "0":
            content = (
                b'<MediaContainer offset="0" totalSize="3">'
                b'<Video historyKey="/history/1" viewedAt="1700000001" type="movie" />'
                b'<Video historyKey="/history/2" viewedAt="1700000002" type="movie" />'
                b"</MediaContainer>"
            )
        else:
            content = (
                b'<MediaContainer offset="2" totalSize="3">'
                b'<Video ratingKey="9" historyKey="/history/3" '
                b'viewedAt="1700000003" type="movie" title="Valid" />'
                b"</MediaContainer>"
            )
        return httpx.Response(200, content=content, request=request)

    page = make_connector(httpx.MockTransport(route)).get_history_page(
        ExternalLibraryRef("1", ExternalLibraryType.MOVIE),
        None,
        PageRequest(size=2),
    )

    assert requested_starts == ["0", "2"]
    assert [item.media_external_id for item in page.items] == ["9"]
    assert page.start == 2
    assert page.next_start is None


def test_empty_page_and_incorrect_total_do_not_control_termination_alone() -> None:
    responses = [
        (
            b'<MediaContainer offset="0" totalSize="1">'
            b'<Video ratingKey="1" type="movie" title="A" /></MediaContainer>'
        ),
        b'<MediaContainer offset="1" totalSize="999" />',
    ]

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=responses.pop(0), request=request)

    connector = make_connector(httpx.MockTransport(route))
    library = ExternalLibraryRef("1", ExternalLibraryType.MOVIE)
    first = connector.get_media_page(library, ExternalMediaKind.MOVIE, PageRequest(size=1))
    last = connector.get_media_page(library, ExternalMediaKind.MOVIE, PageRequest(start=1, size=1))
    assert first.has_more is True
    assert last.items == ()
    assert last.has_more is False


def test_repeated_or_wrong_offset_page_is_rejected() -> None:
    content = (
        b'<MediaContainer offset="0">'
        b'<Video ratingKey="1" type="movie" title="A" /></MediaContainer>'
    )

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, request=request)

    connector = make_connector(httpx.MockTransport(route))
    library = ExternalLibraryRef("1", ExternalLibraryType.MOVIE)
    connector.get_media_page(library, ExternalMediaKind.MOVIE, PageRequest(size=1))
    with pytest.raises(ConnectorPaginationError, match="offset"):
        connector.get_media_page(library, ExternalMediaKind.MOVIE, PageRequest(start=1, size=1))


def test_repeated_content_with_apparently_valid_offset_is_rejected() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        offset = request.url.params["X-Plex-Container-Start"]
        content = (
            f'<MediaContainer offset="{offset}">'
            '<Video ratingKey="1" type="movie" title="A" /></MediaContainer>'
        ).encode()
        return httpx.Response(200, content=content, request=request)

    connector = make_connector(httpx.MockTransport(route))
    library = ExternalLibraryRef("1", ExternalLibraryType.MOVIE)
    connector.get_media_page(library, ExternalMediaKind.MOVIE, PageRequest(size=1))
    with pytest.raises(ConnectorPaginationError, match="repeated"):
        connector.get_media_page(library, ExternalMediaKind.MOVIE, PageRequest(start=1, size=1))


def test_invalid_payload_is_classified() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not xml or json", request=request)

    with pytest.raises(ConnectorResponseError, match="invalid response"):
        make_connector(httpx.MockTransport(route)).list_libraries()
