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


@dataclass(frozen=True, slots=True)
class EnrichedMetadata:
    summary: str | None = None
    tagline: str | None = None
    studio: str | None = None
    audience_rating: float | None = None
    genres: tuple[str, ...] = ()
    poster_url: str | None = None
    poster_provider: str | None = None


class TmdbClient:
    def __init__(self, token: str, *, client: httpx.Client | None = None) -> None:
        if not token.strip():
            raise ValueError("TMDB token is required")
        self._client = client or httpx.Client(
            base_url="https://api.themoviedb.org",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=20,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None

    def lookup(self, media_type: str, external_id: str, *, language: str) -> EnrichedMetadata:
        if media_type not in {"movie", "tv"} or not external_id.isdigit():
            raise ValueError("Invalid exact TMDB lookup")
        response = self._client.get(f"/3/{media_type}/{external_id}", params={"language": language})
        if response.status_code == 404:
            raise EnrichmentError("TMDB item was not found")
        try:
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise EnrichmentError("TMDB request failed") from error
        companies = data.get("production_companies")
        studio = None
        if isinstance(companies, list) and companies and isinstance(companies[0], dict):
            studio = _text(companies[0].get("name"))
        return EnrichedMetadata(
            summary=_text(data.get("overview")),
            tagline=_text(data.get("tagline")),
            studio=studio,
            audience_rating=_number(data.get("vote_average")),
            genres=_names(data.get("genres")),
            poster_url=_tmdb_poster(data.get("poster_path")),
            poster_provider="tmdb" if _tmdb_poster(data.get("poster_path")) else None,
        )

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
            raise EnrichmentError("MusicBrainz recording was not found")
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

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


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
