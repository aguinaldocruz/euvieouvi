"""Exact external metadata provider contracts."""

import httpx
import pytest

from euvieouvi.enrichment.providers import (
    EnrichmentError,
    MusicBrainzClient,
    TmdbClient,
)

MBID = "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"


def test_tmdb_manual_search_and_exact_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/3/search/tv":
            assert request.url.params["query"] == "Dick Tracy"
            assert request.url.params["include_adult"] == "false"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 15119,
                            "name": "Dick Tracy",
                            "first_air_date": "1961-01-01",
                            "poster_path": "/poster.jpg",
                        }
                    ]
                },
            )
        assert request.url.path == "/3/tv/15119"
        assert request.url.params["append_to_response"] == "external_ids"
        return httpx.Response(
            200,
            json={
                "id": 15119,
                "name": "The Dick Tracy Show",
                "backdrop_path": "/backdrop.jpg",
                "external_ids": {"imdb_id": "tt0281433", "tvdb_id": 254049},
            },
        )

    client = TmdbClient(
        "token",
        client=httpx.Client(
            base_url="https://api.themoviedb.org", transport=httpx.MockTransport(handler)
        ),
    )
    results = client.search("tv", "Dick Tracy", language="pt-BR")
    assert results[0].year == 1961
    details = client.match_details("tv", "15119", language="en-US")
    assert details.identifiers == (("tmdb", "15119"), ("imdb", "tt0281433"), ("tvdb", "254049"))
    assert details.backdrop_url == "https://image.tmdb.org/t/p/w500/backdrop.jpg"


def test_manual_match_network_error_is_actionable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = TmdbClient(
        "token",
        client=httpx.Client(
            base_url="https://api.themoviedb.org", transport=httpx.MockTransport(handler)
        ),
    )
    with pytest.raises(EnrichmentError, match="request failed"):
        client.search("tv", "Test", language="en-US")


def test_tmdb_exact_lookup_maps_missing_field_candidates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/3/movie/329865"
        assert request.url.params["language"] == "pt-BR"
        return httpx.Response(
            200,
            json={
                "overview": "Visitantes chegam à Terra.",
                "tagline": "Por que eles estão aqui?",
                "vote_average": 8.2,
                "genres": [{"name": "Ficção científica"}],
                "production_companies": [{"name": "Paramount"}],
                "poster_path": "/arrival.jpg",
            },
            request=request,
        )

    http = httpx.Client(
        base_url="https://api.themoviedb.org", transport=httpx.MockTransport(handler)
    )
    value = TmdbClient("token", client=http).lookup("movie", "329865", language="pt-BR")
    assert value.summary == "Visitantes chegam à Terra."
    assert value.tagline == "Por que eles estão aqui?"
    assert value.studio == "Paramount"
    assert value.audience_rating == 8.2
    assert value.genres == ("Ficção científica",)
    assert value.poster_url == "https://image.tmdb.org/t/p/w500/arrival.jpg"
    assert value.poster_provider == "tmdb"


def test_tmdb_rejects_nonexact_id_and_not_found() -> None:
    http = httpx.Client(
        base_url="https://api.themoviedb.org",
        transport=httpx.MockTransport(lambda request: httpx.Response(404, request=request)),
    )
    client = TmdbClient("token", client=http)
    with pytest.raises(ValueError, match="exact"):
        client.lookup("movie", "Arrival", language="pt-BR")
    with pytest.raises(EnrichmentError, match="not found"):
        client.lookup("movie", "329865", language="pt-BR")


def test_tmdb_resolves_exact_external_show_identifier() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/3/find/455064"
        assert request.url.params["external_source"] == "tvdb_id"
        assert request.url.params["language"] == "pt-BR"
        return httpx.Response(
            200,
            json={"movie_results": [], "tv_results": [{"poster_path": "/neagley.jpg"}]},
            request=request,
        )

    http = httpx.Client(
        base_url="https://api.themoviedb.org", transport=httpx.MockTransport(handler)
    )
    value = TmdbClient("token", client=http).lookup_external(
        "tv", "tvdb", "455064", language="pt-BR"
    )
    assert value.poster_url == "https://image.tmdb.org/t/p/w500/neagley.jpg"


def test_tmdb_external_lookup_rejects_unsupported_or_mismatched_identifiers() -> None:
    client = TmdbClient(
        "token",
        client=httpx.Client(
            base_url="https://api.themoviedb.org",
            transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request)),
        ),
    )
    with pytest.raises(ValueError, match="external"):
        client.lookup_external("movie", "tvdb", "455064", language="en-US")
    with pytest.raises(ValueError, match="external"):
        client.lookup_external("tv", "imdb", "455064", language="en-US")


def test_musicbrainz_exact_lookup_respects_one_request_per_second() -> None:
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/ws/2/recording/{MBID}"
        assert request.url.params["inc"] == "genres+artist-credits+releases"
        return httpx.Response(
            200,
            json={
                "genres": [{"name": "Rock"}, {"name": "Pop"}],
                "releases": [{"id": "9dbb5ea9-118b-4203-b093-bc4b14b8aa16"}],
            },
            request=request,
        )

    http = httpx.Client(base_url="https://musicbrainz.org", transport=httpx.MockTransport(handler))
    client = MusicBrainzClient("euvieouvi/test", client=http, sleep=delays.append)
    value = client.lookup_recording(MBID)
    assert value.genres == ("Pop", "Rock")
    assert value.poster_url == (
        "https://coverartarchive.org/release/9dbb5ea9-118b-4203-b093-bc4b14b8aa16/front-500"
    )
    client.lookup_recording(MBID)
    assert delays == [1.0]
    with pytest.raises(ValueError, match="exact"):
        client.lookup_recording("not-an-mbid")

    release = client.lookup_release_artwork("9dbb5ea9-118b-4203-b093-bc4b14b8aa16")
    assert release.poster_url == (
        "https://coverartarchive.org/release/9dbb5ea9-118b-4203-b093-bc4b14b8aa16/front-500"
    )
