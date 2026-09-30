"""Exact-identifier clients for optional metadata enrichment."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

_MBID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class EnrichmentError(Exception):
    pass


class EnrichmentNotFoundError(EnrichmentError):
    """The exact external identifier is permanently absent at the provider."""


@dataclass(frozen=True, slots=True)
class EnrichedMetadata:
    summary: str | None = None
    tagline: str | None = None
    studio: str | None = None
    audience_rating: float | None = None
    genres: tuple[str, ...] = ()
    poster_url: str | None = None
    poster_provider: str | None = None


@dataclass(frozen=True, slots=True)
class TmdbMatch:
    external_id: str
    title: str
    original_title: str | None
    year: int | None
    metadata: EnrichedMetadata
    backdrop_url: str | None = None
    identifiers: tuple[tuple[str, str], ...] = ()


class TmdbClient:
    def __init__(self, token: str, *, client: httpx.Client | None = None) -> None:
        if not token.strip():
            raise ValueError("TMDB token is required")
        token = token.strip()
        # Support both v3 api_key (32 hex) and v4 Bearer JWT (ey...)
        if len(token) == 32 and all(c in "0123456789abcdefABCDEF" for c in token):
            headers = {"Accept": "application/json"}
            params_extra: dict[str, str] = {"api_key": token}
        else:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            params_extra = {}
        self._token = token
        self._params_extra = params_extra
        self._client = client or httpx.Client(
            base_url="https://api.themoviedb.org",
            headers=headers,
            timeout=20,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._client.get(path, params={**params, **self._params_extra})
            if response.status_code == 404:
                raise EnrichmentNotFoundError("TMDB item was not found")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("TMDB response is not an object")
            return data
        except (httpx.HTTPError, ValueError) as error:
            raise EnrichmentError("TMDB request failed") from error

    def search(self, media_type: str, query: str, *, language: str) -> tuple[TmdbMatch, ...]:
        if media_type not in {"movie", "tv"} or not 1 <= len(query.strip()) <= 200:
            raise ValueError("Invalid TMDB search")
        data = self._get(
            f"/3/search/{media_type}",
            {"query": query.strip(), "language": language, "include_adult": "false", "page": "1"},
        )
        results = data.get("results")
        return (
            tuple(
                _tmdb_match(value)
                for value in results[:20]
                if isinstance(value, dict) and isinstance(value.get("id"), int) and value["id"] > 0
            )
            if isinstance(results, list)
            else ()
        )

    def match_details(self, media_type: str, external_id: str, *, language: str) -> TmdbMatch:
        if media_type not in {"movie", "tv"} or not external_id.isdigit() or int(external_id) <= 0:
            raise ValueError("Invalid exact TMDB match")
        match = _tmdb_match(
            self._get(
                f"/3/{media_type}/{external_id}",
                {"language": language, "append_to_response": "external_ids"},
            )
        )
        if match.external_id != str(int(external_id)):
            raise EnrichmentError("TMDB returned a different identity")
        return match

    def lookup(self, media_type: str, external_id: str, *, language: str) -> EnrichedMetadata:
        if media_type not in {"movie", "tv"} or not external_id.isdigit():
            raise ValueError("Invalid exact TMDB lookup")
        params: dict[str, str] = {"language": language}
        params.update(self._params_extra)
        response = self._client.get(f"/3/{media_type}/{external_id}", params=params)
        if response.status_code == 404:
            raise EnrichmentNotFoundError("TMDB item was not found")
        try:
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise EnrichmentError("TMDB request failed") from error
        return _tmdb_metadata(data)

    def lookup_external(
        self,
        media_type: str,
        provider: str,
        external_id: str,
        *,
        language: str,
    ) -> EnrichedMetadata:
        """Resolve one exact IMDb/TheTVDB identifier through TMDB's Find API."""
        sources = {"imdb": "imdb_id", "tvdb": "tvdb_id"}
        if media_type not in {"movie", "tv"} or provider not in sources:
            raise ValueError("Invalid exact TMDB external lookup")
        if provider == "imdb" and not re.fullmatch(r"tt\d+", external_id):
            raise ValueError("Invalid exact TMDB external lookup")
        if provider == "tvdb" and (media_type != "tv" or not external_id.isdigit()):
            raise ValueError("Invalid exact TMDB external lookup")
        params: dict[str, str] = {
            "external_source": sources[provider],
            "language": language,
        }
        params.update(self._params_extra)
        response = self._client.get(f"/3/find/{external_id}", params=params)
        try:
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise EnrichmentError("TMDB external-ID request failed") from error
        values = data.get("movie_results" if media_type == "movie" else "tv_results")
        if not isinstance(values, list) or not values or not isinstance(values[0], dict):
            raise EnrichmentNotFoundError("TMDB external identifier was not found")
        return _tmdb_metadata(values[0])

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class MusicBrainzClient:
    def __init__(
        self,
        user_agent: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("MusicBrainz User-Agent is required")
        self._client = client or httpx.Client(
            base_url="https://musicbrainz.org",
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=20,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._sleep = sleep
        self._used = False

    def lookup_recording(self, mbid: str) -> EnrichedMetadata:
        if not _MBID.fullmatch(mbid):
            raise ValueError("Invalid exact MusicBrainz recording MBID")
        if self._used:
            self._sleep(1.0)
        response = self._client.get(
            f"/ws/2/recording/{mbid}",
            params={"inc": "genres+artist-credits+releases", "fmt": "json"},
        )
        self._used = True
        if response.status_code == 404:
            raise EnrichmentNotFoundError("MusicBrainz recording was not found")
        try:
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise EnrichmentError("MusicBrainz request failed") from error
        cover_url = _cover_art_url(data.get("releases"))
        return EnrichedMetadata(
            genres=_names(data.get("genres")),
            poster_url=cover_url,
            poster_provider="coverartarchive" if cover_url else None,
        )

    def lookup_release_artwork(self, mbid: str) -> EnrichedMetadata:
        """Return the deterministic Cover Art Archive URL for an exact release MBID."""
        if not _MBID.fullmatch(mbid):
            raise ValueError("Invalid exact MusicBrainz release MBID")
        return EnrichedMetadata(
            poster_url=f"https://coverartarchive.org/release/{mbid}/front-500",
            poster_provider="coverartarchive",
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _tmdb_match(data: dict[str, Any]) -> TmdbMatch:
    title = _text(data.get("title")) or _text(data.get("name"))
    if not title or not isinstance(data.get("id"), int) or data["id"] <= 0:
        raise EnrichmentError("TMDB match omitted identity or title")
    released = _text(data.get("release_date")) or _text(data.get("first_air_date")) or ""
    year = int(released[:4]) if re.match(r"^\d{4}-", released) else None
    external = data.get("external_ids")
    identifiers = [("tmdb", str(data["id"]))]
    if isinstance(external, dict):
        imdb = _text(external.get("imdb_id"))
        if imdb and re.fullmatch(r"tt\d+", imdb):
            identifiers.append(("imdb", imdb))
        tvdb = external.get("tvdb_id")
        if isinstance(tvdb, int) and not isinstance(tvdb, bool) and tvdb > 0:
            identifiers.append(("tvdb", str(tvdb)))
    return TmdbMatch(
        str(data["id"]),
        title,
        _text(data.get("original_title")) or _text(data.get("original_name")),
        year,
        _tmdb_metadata(data),
        _tmdb_poster(data.get("backdrop_path")),
        tuple(identifiers),
    )


def _tmdb_metadata(data: dict[str, Any]) -> EnrichedMetadata:
    companies = data.get("production_companies")
    studio = None
    if isinstance(companies, list) and companies and isinstance(companies[0], dict):
        studio = _text(companies[0].get("name"))
    poster_url = _tmdb_poster(data.get("poster_path"))
    return EnrichedMetadata(
        summary=_text(data.get("overview")),
        tagline=_text(data.get("tagline")),
        studio=studio,
        audience_rating=_number(data.get("vote_average")),
        genres=_names(data.get("genres")),
        poster_url=poster_url,
        poster_provider="tmdb" if poster_url else None,
    )


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result = {
        name
        for item in value
        if isinstance(item, dict) and (name := _text(item.get("name"))) is not None
    }
    return tuple(sorted(result, key=str.casefold))


def _tmdb_poster(value: Any) -> str | None:
    path = _text(value)
    if path is None or not path.startswith("/") or ".." in path:
        return None
    return f"https://image.tmdb.org/t/p/w500{path}"


def _cover_art_url(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    for release in value:
        if isinstance(release, dict) and isinstance(release.get("id"), str):
            release_id = release["id"]
            if _MBID.fullmatch(release_id):
                return f"https://coverartarchive.org/release/{release_id}/front-500"
    return None
