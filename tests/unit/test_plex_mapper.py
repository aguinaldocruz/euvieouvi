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


def test_history_requires_real_timestamp() -> None:
    with pytest.raises(ConnectorResponseError, match="viewedAt"):
        map_watch_event({"ratingKey": "1"}, "1")
