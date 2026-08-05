"""Pure mapping from bounded Plex payloads to neutral DTOs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any
from xml.etree import ElementTree

from euvieouvi.connectors.dtos import (
    ConnectionInfo,
    ExternalIdentifier,
    ExternalLibrary,
    ExternalLibraryRejection,
    ExternalLibraryType,
    ExternalMediaItem,
    ExternalMediaKind,
    ExternalWatchEvent,
)
from euvieouvi.connectors.errors import ConnectorResponseError
from euvieouvi.connectors.plex.client import PlexPayload


def parse_container(payload: PlexPayload) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse a Plex XML or JSON MediaContainer without leaking its shape outward."""
    content = payload.content.lstrip()
    try:
        if "json" in payload.content_type.lower() or content.startswith(b"{"):
            document = json.loads(payload.content)
            container = document.get("MediaContainer", document)
            if not isinstance(container, dict):
                raise TypeError
            json_items = container.get("Metadata", container.get("Directory", []))
            if isinstance(json_items, dict):
                json_items = [json_items]
            if not isinstance(json_items, list):
                raise TypeError
            return dict(container), [dict(item) for item in json_items if isinstance(item, dict)]
        if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
            raise ConnectorResponseError("Plex XML declarations are not supported.")
        root = ElementTree.fromstring(payload.content)
    except (ElementTree.ParseError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise ConnectorResponseError("Plex returned an invalid response document.") from error
    mapped_items: list[dict[str, Any]] = []
    for element in root:
        item: dict[str, Any] = dict(element.attrib)
        item["_tag"] = element.tag
        for child in element:
            item.setdefault(child.tag, []).append(dict(child.attrib))
        mapped_items.append(item)
    return dict(root.attrib), mapped_items


def map_connection(container: Mapping[str, Any]) -> ConnectionInfo:
    capabilities = frozenset(
        part.strip() for part in str(container.get("capabilities", "")).split(",") if part.strip()
    )
    return ConnectionInfo(
        server_name=_required(container, "friendlyName"),
        server_identifier=_required(container, "machineIdentifier"),
        server_version=_optional_text(container.get("version")),
        authenticated=True,
        capabilities=capabilities,
    )


def map_library_discovery(
    items: Sequence[Mapping[str, Any]],
) -> tuple[list[ExternalLibrary], tuple[ExternalLibraryRejection, ...]]:
    libraries: list[ExternalLibrary] = []
    rejected: list[ExternalLibraryRejection] = []
    for item in items:
        raw_type = _optional_text(item.get("type"))
        if raw_type not in {"movie", "show", "artist"}:
            rejected.append(
                ExternalLibraryRejection(
                    external_id=_required(item, "key"),
                    name=_required(item, "title"),
                    source_type=raw_type or "unknown",
                    reason="unsupported_library_type",
                )
            )
            continue
        libraries.append(
            ExternalLibrary(
                external_id=_required(item, "key"),
                name=_required(item, "title"),
                media_type=ExternalLibraryType(raw_type),
                available=True,
                source_updated_at=_timestamp(item.get("updatedAt")),
            )
        )
    return libraries, tuple(rejected)


def map_media_item(item: Mapping[str, Any], library_external_id: str) -> ExternalMediaItem:
    raw_kind = _required(item, "type")
    try:
        kind = ExternalMediaKind(raw_kind)
    except ValueError as error:
        raise ConnectorResponseError(f"Unsupported Plex media type: {raw_kind}.") from error
    external_id = _required(item, "ratingKey")
    title = _optional_text(item.get("title"))
    if title is None:
        if kind is not ExternalMediaKind.TRACK:
            raise ConnectorResponseError("Plex response omitted required field title.")
        title = f"Faixa sem título ({external_id})"
    artist_external_id = _optional_text(item.get("grandparentRatingKey"))
    album_external_id = _optional_text(item.get("parentRatingKey"))
    if kind is ExternalMediaKind.TRACK and album_external_id is None:
        album_external_id = f"{artist_external_id or library_external_id}:album:unknown"
    return ExternalMediaItem(
        external_id=external_id,
        external_key=_optional_text(item.get("key")),
        library_external_id=library_external_id,
        kind=kind,
        title=title,
        original_title=_optional_text(item.get("originalTitle")),
        year=_integer(item.get("year")),
        show_external_id=_optional_text(item.get("grandparentRatingKey")),
        show_title=_optional_text(item.get("grandparentTitle")),
        season_external_id=_optional_text(item.get("parentRatingKey")),
        season_number=_integer(item.get("parentIndex")),
        episode_number=_integer(item.get("index")),
        artist_external_id=artist_external_id,
        artist_title=_optional_text(item.get("grandparentTitle")),
        album_external_id=album_external_id,
        album_title=(
            _optional_text(item.get("parentTitle"))
            or ("Álbum desconhecido" if kind is ExternalMediaKind.TRACK else None)
        ),
        disc_number=_integer(item.get("parentIndex")) if raw_kind == "track" else None,
        track_number=_integer(item.get("index")) if raw_kind == "track" else None,
        duration_ms=_integer(item.get("duration")),
        originally_available_on=_date(item.get("originallyAvailableAt")),
        summary=_optional_text(item.get("summary")),
        tagline=_optional_text(item.get("tagline")),
        studio=_optional_text(item.get("studio")),
        content_rating=_optional_text(item.get("contentRating")),
        audience_rating=_number(item.get("audienceRating", item.get("rating"))),
        genres=_tag_values(item.get("Genre")),
        added_at=_timestamp(item.get("addedAt")),
        thumb_path=_optional_text(item.get("thumb")),
        art_path=_optional_text(item.get("art")),
        artist_thumb_path=_optional_text(item.get("grandparentThumb")),
        album_thumb_path=_optional_text(item.get("parentThumb")),
        identifiers=_identifiers(item.get("Guid")),
        updated_at=_timestamp(item.get("updatedAt")),
        last_viewed_at=_timestamp(item.get("lastViewedAt")),
        view_count=_integer(item.get("viewCount")),
        view_offset_ms=_integer(item.get("viewOffset")),
    )


def map_watch_event(item: Mapping[str, Any], library_external_id: str) -> ExternalWatchEvent:
    duration = _integer(item.get("duration"))
    progress = _integer(item.get("viewOffset"))
    view_count = _integer(item.get("viewCount"))
    view_number = _integer(item.get("viewIndex", item.get("viewNumber")))
    completed = bool(view_count and view_count > 0)
    if duration and progress is not None:
        completed = progress / duration >= 0.9
    return ExternalWatchEvent(
        source_event_id=_optional_text(item.get("historyKey")),
        media_external_id=_required(item, "ratingKey"),
        library_external_id=library_external_id,
        watched_at=_required_timestamp(item.get("viewedAt")),
        completed=completed,
        progress_ms=progress,
        duration_ms=duration,
        view_number=view_number,
    )


def _identifiers(value: Any) -> tuple[ExternalIdentifier, ...]:
    if not isinstance(value, list):
        return ()
    identifiers: list[ExternalIdentifier] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        raw_id = _optional_text(item.get("id"))
        if raw_id is None or "://" not in raw_id:
            continue
        provider, external_id = raw_id.split("://", 1)
        if provider and external_id:
            identifiers.append(
                ExternalIdentifier(provider=provider.lower(), external_id=external_id)
            )
    return tuple(identifiers)


def _required(item: Mapping[str, Any], key: str) -> str:
    value = _optional_text(item.get(key))
    if value is None:
        raise ConnectorResponseError(f"Plex response omitted required field {key}.")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ConnectorResponseError("Plex response contained an invalid integer.") from error
    if result < 0:
        raise ConnectorResponseError("Plex response contained a negative integer.")
    return result


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ConnectorResponseError("Plex response contained an invalid number.") from error


def _tag_values(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    values = {
        normalized
        for item in value
        if isinstance(item, Mapping)
        and (normalized := _optional_text(item.get("tag"))) is not None
    }
    return tuple(sorted(values, key=str.casefold))


def _timestamp(value: Any) -> datetime | None:
    integer = _integer(value)
    return datetime.fromtimestamp(integer, UTC) if integer is not None else None


def _required_timestamp(value: Any) -> datetime:
    result = _timestamp(value)
    if result is None:
        raise ConnectorResponseError("Plex history omitted viewedAt.")
    return result


def _date(value: Any) -> date | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError as error:
        raise ConnectorResponseError("Plex response contained an invalid date.") from error
