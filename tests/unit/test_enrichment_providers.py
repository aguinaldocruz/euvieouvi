"""Exact external metadata provider contracts."""

import httpx
import pytest

from euvieouvi.enrichment.providers import (
    EnrichmentError,
    MusicBrainzClient,
    TmdbClient,
)

MBID = "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"


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


def test_musicbrainz_exact_lookup_respects_one_request_per_second() -> None:
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/ws/2/recording/{MBID}"
        assert request.url.params["inc"] == "genres+artist-credits+releases"
        return httpx.Response(
            200,
            json={"genres": [{"name": "Rock"}, {"name": "Pop"}]},
            request=request,
        )

    http = httpx.Client(
        base_url="https://musicbrainz.org", transport=httpx.MockTransport(handler)
    )
    client = MusicBrainzClient("euvieouvi/test", client=http, sleep=delays.append)
    assert client.lookup_recording(MBID).genres == ("Pop", "Rock")
    client.lookup_recording(MBID)
    assert delays == [1.0]
    with pytest.raises(ValueError, match="exact"):
        client.lookup_recording("not-an-mbid")
