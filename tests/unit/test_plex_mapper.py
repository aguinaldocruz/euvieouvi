"""Failure-mode tests for Plex payload mapping."""

import pytest

from euvieouvi.connectors.errors import ConnectorResponseError
from euvieouvi.connectors.plex.client import PlexPayload
from euvieouvi.connectors.plex.mapper import map_media_item, map_watch_event, parse_container


def test_document_declarations_and_invalid_json_are_rejected() -> None:
    with pytest.raises(ConnectorResponseError, match="declarations"):
        parse_container(PlexPayload(b"<!DOCTYPE sample><MediaContainer />", "application/xml"))
    with pytest.raises(ConnectorResponseError, match="invalid response"):
        parse_container(PlexPayload(b'{"MediaContainer": []}', "application/json"))


def test_invalid_media_fields_are_classified() -> None:
    with pytest.raises(ConnectorResponseError, match="Unsupported"):
        map_media_item({"ratingKey": "1", "type": "clip", "title": "A"}, "1")
    with pytest.raises(ConnectorResponseError, match="invalid integer"):
        map_media_item(
            {"ratingKey": "1", "type": "movie", "title": "A", "duration": "invalid"},
            "1",
        )
    with pytest.raises(ConnectorResponseError, match="negative"):
        map_media_item(
            {"ratingKey": "1", "type": "movie", "title": "A", "duration": "-1"},
            "1",
        )
    with pytest.raises(ConnectorResponseError, match="invalid date"):
        map_media_item(
            {
                "ratingKey": "1",
                "type": "movie",
                "title": "A",
                "originallyAvailableAt": "not-a-date",
            },
            "1",
        )


def test_orphan_track_uses_stable_fallback_labels_and_album_identity() -> None:
    track = map_media_item(
        {
            "ratingKey": "127830",
            "type": "track",
            "title": "",
            "grandparentRatingKey": "artist-1",
            "grandparentTitle": "The Black Crowes",
            "parentRatingKey": "album-1",
            "parentIndex": "1",
            "duration": "470",
        },
        "3",
    )
    assert track.title == "Faixa sem título (127830)"
    assert track.album_external_id == "album-1"
    assert track.album_title == "Álbum desconhecido"


def test_track_without_album_identity_uses_library_scoped_synthetic_identity() -> None:
    track = map_media_item(
        {
            "ratingKey": "track-1",
            "type": "track",
            "title": "Twice As Hard",
            "grandparentRatingKey": "artist-1",
            "grandparentTitle": "The Black Crowes",
        },
        "3",
    )
    assert track.album_external_id == "artist-1:album:unknown"
    assert track.album_title == "Álbum desconhecido"


def test_history_requires_real_timestamp() -> None:
    with pytest.raises(ConnectorResponseError, match="viewedAt"):
        map_watch_event({"ratingKey": "1"}, "1")
