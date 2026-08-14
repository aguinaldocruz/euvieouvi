"""Jellyfin boundary, mapping, and pagination tests without network access."""

from datetime import UTC, datetime

import httpx
import pytest
from flask import Flask

from euvieouvi.api.runtime import LocalSyncExecutor, connector_for
from euvieouvi.connectors.dtos import (
    ExternalLibraryRef,
    ExternalLibraryType,
    ExternalMediaKind,
    PageRequest,
)
from euvieouvi.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorConfigurationError,
    ConnectorConnectionError,
    ConnectorNotFoundError,
    ConnectorResponseError,
    ConnectorTimeoutError,
)
from euvieouvi.connectors.jellyfin.client import JellyfinHttpClient
from euvieouvi.connectors.jellyfin.connector import JellyfinConnector
from euvieouvi.connectors.jellyfin.mapper import map_history_item, map_item, map_libraries
from euvieouvi.database.enums import ConnectorType
from euvieouvi.database.models import Source


def make_client(handler: httpx.MockTransport) -> JellyfinHttpClient:
    return JellyfinHttpClient(
        "http://jellyfin.local/base",
        "test-api-key",
        client=httpx.Client(transport=handler),
    )


def test_connector_discovers_reads_history_and_images() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Emby-Token"] == "test-api-key"
        if request.url.path.endswith("/System/Info"):
            return httpx.Response(
                200,
                json={"ServerName": "Casa", "Id": "jf-1", "Version": "10.11"},
                request=request,
            )
        if request.url.path.endswith("/Users/user-1/Views"):
            return httpx.Response(
                200,
                json={"Items": [{"Id": "lib-1", "Name": "Filmes", "CollectionType": "movies"}]},
                request=request,
            )
        if request.url.path.endswith("/Users/user-1/Items"):
            assert request.url.params["ParentId"] == "lib-1"
            return httpx.Response(
                200,
                json={
                    "TotalRecordCount": 1,
                    "Items": [
                        {
                            "Id": "movie-1",
                            "Type": "Movie",
                            "Name": "Arrival",
                            "RunTimeTicks": 7_000_000,
                            "ProviderIds": {"Imdb": "tt2543164"},
                            "UserData": {
                                "PlayCount": 2,
                                "LastPlayedDate": "2026-08-05T18:00:00Z",
                            },
                            "ImageTags": {"Primary": "tag"},
                        }
                    ],
                },
                request=request,
            )
        if request.url.path.endswith("/Items/movie-1/Images/Primary"):
            return httpx.Response(
                200, content=b"image", headers={"content-type": "image/jpeg"}, request=request
            )
        raise AssertionError(str(request.url))

    connector = JellyfinConnector(make_client(httpx.MockTransport(handler)), "user-1")
    assert connector.test_connection().server_identifier == "jf-1"
    assert connector.list_libraries()[0].media_type is ExternalLibraryType.MOVIE
    library = ExternalLibraryRef("lib-1", ExternalLibraryType.MOVIE)
    media = connector.get_media_page(library, ExternalMediaKind.MOVIE, PageRequest(size=10))
    assert media.items[0].title == "Arrival" and media.items[0].duration_ms == 700
    history = connector.get_history_page(library, None, PageRequest(size=10))
    assert history.items[0].completed and history.items[0].view_number == 2
    assert connector.fetch_image("/Items/movie-1/Images/Primary", width=300, height=450) == (
        b"image",
        "image/jpeg",
    )
    connector.close()


def test_mark_watched_posts_played_item_for_configured_user() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/Users/user-1/PlayedItems/movie-1")
        return httpx.Response(204, request=request)

    connector = JellyfinConnector(make_client(httpx.MockTransport(handler)), "user-1")
    connector.mark_watched("movie-1")


def test_connector_fetches_and_caches_parent_series_provider_ids() -> None:
    series_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal series_requests
        if request.url.path.endswith("/Users/user-1/Items/show-1"):
            series_requests += 1
            return httpx.Response(
                200,
                json={"ProviderIds": {"Imdb": "tt0213327", "Tmdb": "2985"}},
                request=request,
            )
        if request.url.path.endswith("/Users/user-1/Items"):
            return httpx.Response(
                200,
                json={
                    "TotalRecordCount": 2,
                    "Items": [
                        {
                            "Id": f"episode-{number}",
                            "Type": "Episode",
                            "Name": f"Episode {number}",
                            "SeriesId": "show-1",
                            "SeriesName": "Andrômeda",
                            "ParentIndexNumber": 1,
                            "IndexNumber": number,
                        }
                        for number in (1, 2)
                    ],
                },
                request=request,
            )
        raise AssertionError(str(request.url))

    connector = JellyfinConnector(make_client(httpx.MockTransport(handler)), "user-1")
    page = connector.get_media_page(
        ExternalLibraryRef("shows", ExternalLibraryType.SHOW),
        ExternalMediaKind.EPISODE,
        PageRequest(size=10),
    )
    assert series_requests == 1
    assert page.items[0].show_identifiers[0].external_id == "tt0213327"
    assert page.items[1].show_identifiers[1].external_id == "2985"


def test_music_and_episode_mapping_with_fallbacks() -> None:
    track = map_item(
        {
            "Id": "track-1",
            "Type": "Audio",
            "Name": "Song",
            "IndexNumber": 3,
            "ParentIndexNumber": 1,
            "ProviderIds": {"MusicBrainzTrack": "mb-track"},
            "UserData": {"PlaybackPositionTicks": 20_000},
        },
        "music",
    )
    assert track.artist_title == "Artista desconhecido"
    assert track.album_title == "Álbum desconhecido"
    assert track.view_offset_ms == 2 and track.identifiers[0].provider == "mbid"
    episode = map_item(
        {
            "Id": "episode-1",
            "Type": "Episode",
            "Name": "Pilot",
            "SeriesId": "show-1",
            "SeriesName": "Show",
            "SeasonId": "season-1",
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
            "PremiereDate": "2026-08-05T00:00:00Z",
            "DateCreated": "2026-08-05T12:00:00Z",
            "Genres": ["Drama", 1],
        },
        "shows",
    )
    assert episode.show_title == "Show"
    assert episode.originally_available_on is not None
    assert episode.originally_available_on.isoformat() == "2026-08-05"
    assert map_history_item({"Id": "m", "Type": "Movie", "Name": "M"}, "movies") is None
    assert map_libraries([None, {"CollectionType": "books"}]) == []
    with pytest.raises(ConnectorResponseError):
        map_item({"Id": "x", "Type": "BoxSet", "Name": "X"}, "lib")
    with pytest.raises(ConnectorResponseError):
        map_item({"Id": "x", "Type": "Movie", "Name": " "}, "lib")


def test_media_pagination_advances_past_invalid_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "TotalRecordCount": 3,
                "Items": [
                    {"Id": "bad", "Type": "Episode", "Name": "Missing numbers"},
                    {"Id": "movie-1", "Type": "Movie", "Name": "Arrival"},
                ],
            },
            request=request,
        )

    connector = JellyfinConnector(make_client(httpx.MockTransport(handler)), "user-1")
    page = connector.get_media_page(
        ExternalLibraryRef("lib-1", ExternalLibraryType.MOVIE),
        ExternalMediaKind.MOVIE,
        PageRequest(size=2),
    )

    assert [item.external_id for item in page.items] == ["movie-1"]
    assert page.next_start == 2


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ConnectorAuthenticationError),
        (404, ConnectorNotFoundError),
        (500, ConnectorResponseError),
    ],
)
def test_client_classifies_statuses(status: int, error: type[Exception]) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, request=request))
    with pytest.raises(error):
        make_client(transport).get_json("/System/Info")


def test_client_rejects_invalid_responses_and_transport_errors() -> None:
    def invalid(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=request)

    with pytest.raises(ConnectorResponseError, match="invalid JSON"):
        make_client(httpx.MockTransport(invalid)).get_json("/x")
    with pytest.raises(ConnectorConfigurationError):
        make_client(httpx.MockTransport(invalid)).get_json("//invalid")
    with pytest.raises(ConnectorConfigurationError):
        JellyfinHttpClient("ftp://invalid", "key")
    with pytest.raises(ConnectorConfigurationError):
        JellyfinHttpClient("http://jellyfin.local", " ")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(ConnectorTimeoutError):
        make_client(httpx.MockTransport(timeout)).get_json("/x")

    def connection(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed", request=request)

    with pytest.raises(ConnectorConnectionError):
        make_client(httpx.MockTransport(connection)).get_json("/x")


def test_mapper_normalizes_timestamp() -> None:
    event = map_history_item(
        {
            "Id": "m",
            "Type": "Movie",
            "Name": "M",
            "UserData": {"PlayCount": 1, "LastPlayedDate": "2026-08-05T18:00:00-03:00"},
        },
        "movies",
    )
    assert event is not None and event.watched_at == datetime(2026, 8, 5, 21, tzinfo=UTC)


def test_runtime_builds_jellyfin_connector_and_rejects_bad_credentials() -> None:
    source = Source(
        connector_type=ConnectorType.JELLYFIN,
        name="Jellyfin",
        base_url="http://jellyfin.local",
        secret='{"api_key":"key","user_id":"user"}',
        enabled=True,
    )
    connector = connector_for(source)
    assert isinstance(connector, JellyfinConnector)
    connector.close()
    source.secret = "{}"
    with pytest.raises(ValueError, match="credentials"):
        connector_for(source)


def test_executor_rejects_empty_or_unknown_sources(app: Flask) -> None:
    executor = LocalSyncExecutor(app)
    with app.app_context():
        with pytest.raises(LookupError, match="Source not found"):
            executor.submit(999)
        assert executor.cancel(999) is False
