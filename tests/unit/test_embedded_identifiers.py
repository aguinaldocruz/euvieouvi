"""Strict parsing of metadata-provider IDs embedded in Jellyfin names."""

import pytest

from euvieouvi.connectors.embedded_identifiers import parse_embedded_identifiers


@pytest.mark.parametrize(
    ("title", "cleaned", "identifiers"),
    [
        ("Localized [tmdb-15119]", "Localized", (("tmdb", "15119"),)),
        ("Series {tvdbid=254049}", "Series", (("tvdb", "254049"),)),
        ("Movie (imdbid-tt0281433)", "Movie", (("imdb", "tt0281433"),)),
        (
            "Series [tmdbid-15119] [tvdb-254049]",
            "Series",
            (("tmdb", "15119"), ("tvdb", "254049")),
        ),
    ],
)
def test_parses_supported_provider_tokens(
    title: str, cleaned: str, identifiers: tuple[tuple[str, str], ...]
) -> None:
    result = parse_embedded_identifiers(title)
    assert result.title == cleaned
    assert tuple((value.provider, value.external_id) for value in result.identifiers) == identifiers


@pytest.mark.parametrize(
    "title",
    [
        "Documentary about tmdb-15119",
        "Mismatched [tmdb-15119}",
        "Invalid [tmdb-0]",
        "Invalid [imdb-15119]",
        "Unknown [trakt-15054]",
        "Conflicting [tmdb-15119] [tmdb-42]",
    ],
)
def test_rejects_ambiguous_or_invalid_tokens(title: str) -> None:
    result = parse_embedded_identifiers(title)
    assert result.title == title
    assert result.identifiers == ()
