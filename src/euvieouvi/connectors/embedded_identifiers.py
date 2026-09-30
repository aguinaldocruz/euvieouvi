"""Parse Jellyfin-supported provider identifiers embedded in media names."""

from __future__ import annotations

import re
from dataclasses import dataclass

from euvieouvi.connectors.dtos import ExternalIdentifier

_IDENTIFIER = re.compile(
    r"(?P<open>[\[\{\(])\s*"
    r"(?P<provider>tmdb(?:id)?|tvdb(?:id)?|imdb(?:id)?)\s*[-=]\s*"
    r"(?P<external_id>tt[0-9]+|[0-9]+)\s*"
    r"(?P<close>[\]\}\)])",
    re.IGNORECASE,
)
_CLOSING = {"[": "]", "{": "}", "(": ")"}
_PROVIDERS = {
    "tmdb": "tmdb",
    "tmdbid": "tmdb",
    "tvdb": "tvdb",
    "tvdbid": "tvdb",
    "imdb": "imdb",
    "imdbid": "imdb",
}


@dataclass(frozen=True, slots=True)
class EmbeddedMetadata:
    title: str
    identifiers: tuple[ExternalIdentifier, ...]


def parse_embedded_identifiers(title: str) -> EmbeddedMetadata:
    """Return a clean title and only strictly valid, bracketed provider IDs."""
    identifiers: list[ExternalIdentifier] = []
    spans: list[tuple[int, int]] = []
    seen: set[tuple[str, str]] = set()
    for match in _IDENTIFIER.finditer(title):
        if _CLOSING[match.group("open")] != match.group("close"):
            continue
        provider = _PROVIDERS[match.group("provider").casefold()]
        external_id = match.group("external_id").casefold()
        if provider == "imdb":
            if not external_id.startswith("tt"):
                continue
        elif external_id.startswith("tt") or int(external_id) <= 0:
            continue
        identity = (provider, external_id)
        if identity not in seen:
            identifiers.append(ExternalIdentifier(provider, external_id))
            seen.add(identity)
        spans.append(match.span())
    if not spans:
        return EmbeddedMetadata(title, ())
    for provider in {value.provider for value in identifiers}:
        if sum(value.provider == provider for value in identifiers) > 1:
            return EmbeddedMetadata(title, ())
    parts: list[str] = []
    offset = 0
    for start, end in spans:
        parts.append(title[offset:start])
        offset = end
    parts.append(title[offset:])
    cleaned = " ".join("".join(parts).split()).strip(" -")
    return EmbeddedMetadata(cleaned or title, tuple(identifiers))
