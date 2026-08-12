"""Map Jellyfin JSON objects into connector-neutral DTOs."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from euvieouvi.connectors.dtos import (
    ExternalIdentifier,
    ExternalLibrary,
    ExternalLibraryType,
    ExternalMediaItem,
    ExternalMediaKind,
    ExternalWatchEvent,
)
from euvieouvi.connectors.errors import ConnectorResponseError

_LIBRARY_TYPES = {
    "movies": ExternalLibraryType.MOVIE,
    "tvshows": ExternalLibraryType.SHOW,
    "music": ExternalLibraryType.ARTIST,
}
_ITEM_TYPES = {
    "Movie": ExternalMediaKind.MOVIE,
    "Episode": ExternalMediaKind.EPISODE,
    "Audio": ExternalMediaKind.TRACK,
}


def map_libraries(items: list[Any]) -> list[ExternalLibrary]:
    result: list[ExternalLibrary] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        media_type = _LIBRARY_TYPES.get(str(raw.get("CollectionType", "")).lower())
        external_id = _text(raw.get("Id"))
        name = _text(raw.get("Name"))
        if media_type is not None and external_id and name:
            result.append(ExternalLibrary(external_id, name, media_type))
    return result


def map_item(raw: dict[str, Any], library_id: str) -> ExternalMediaItem:
    raw_type = _text(raw.get("Type"))
    kind = _ITEM_TYPES.get(raw_type or "")
    if kind is None:
        raise ConnectorResponseError("Unsupported Jellyfin media type.")
    external_id = _required(raw, "Id")
    title = _required(raw, "Name")
    raw_user_data = raw.get("UserData")
    user_data: dict[str, Any] = raw_user_data if isinstance(raw_user_data, dict) else {}
    run_ticks = _integer(raw.get("RunTimeTicks"))
    raw_image_tags = raw.get("ImageTags")
    image_tags: dict[str, Any] = raw_image_tags if isinstance(raw_image_tags, dict) else {}
    primary = f"/Items/{external_id}/Images/Primary" if image_tags.get("Primary") else None
    genres = tuple(
        value for value in raw.get("Genres", []) if isinstance(value, str) and value.strip()
    )
    series_id = _text(raw.get("SeriesId"))
    season_id = _text(raw.get("SeasonId"))
    album_id = _text(raw.get("AlbumId"))
    album_artists = raw.get("AlbumArtists")
    artist_id = None
    artist_name = _text(raw.get("AlbumArtist"))
    if isinstance(album_artists, list) and album_artists and isinstance(album_artists[0], dict):
        artist_id = _text(album_artists[0].get("Id"))
        artist_name = _text(album_artists[0].get("Name")) or artist_name
    if kind is ExternalMediaKind.TRACK:
        artist_id = artist_id or f"{library_id}:artist:unknown"
        artist_name = artist_name or "Artista desconhecido"
        album_id = album_id or f"{artist_id}:album:unknown"
    return ExternalMediaItem(
        external_id=external_id,
        external_key=f"/Items/{external_id}",
        library_external_id=library_id,
        kind=kind,
        title=title,
        original_title=_text(raw.get("OriginalTitle")),
        year=_integer(raw.get("ProductionYear")),
        show_external_id=series_id,
        show_title=_text(raw.get("SeriesName")),
        show_identifiers=_identifiers(raw.get("_SeriesProviderIds")),
        season_external_id=season_id,
        season_number=_integer(raw.get("ParentIndexNumber")),
        episode_number=_integer(raw.get("IndexNumber")),
        artist_external_id=artist_id,
        artist_title=artist_name,
        album_external_id=album_id,
        album_title=(
            _text(raw.get("Album"))
            or ("Álbum desconhecido" if kind is ExternalMediaKind.TRACK else None)
        ),
        disc_number=(
            _integer(raw.get("ParentIndexNumber")) if kind is ExternalMediaKind.TRACK else None
        ),
        track_number=_integer(raw.get("IndexNumber")) if kind is ExternalMediaKind.TRACK else None,
        duration_ms=run_ticks // 10_000 if run_ticks is not None else None,
        originally_available_on=_date(raw.get("PremiereDate")),
        summary=_text(raw.get("Overview")),
        tagline=_text(raw.get("Taglines", [None])[0]) if raw.get("Taglines") else None,
        studio=_first_name(raw.get("Studios")),
        content_rating=_text(raw.get("OfficialRating")),
        audience_rating=_number(raw.get("CommunityRating")),
        genres=genres,
        added_at=_timestamp(raw.get("DateCreated")),
        thumb_path=primary,
        artist_thumb_path=(
            f"/Items/{series_id or artist_id}/Images/Primary" if series_id or artist_id else None
        ),
        album_thumb_path=(
            f"/Items/{season_id or album_id}/Images/Primary" if season_id or album_id else None
        ),
        identifiers=_identifiers(raw.get("ProviderIds")),
        updated_at=_timestamp(raw.get("DateLastMediaAdded")),
        last_viewed_at=_timestamp(user_data.get("LastPlayedDate")),
        view_count=_integer(user_data.get("PlayCount")),
        view_offset_ms=_milliseconds(user_data.get("PlaybackPositionTicks")),
    )


def map_history_item(raw: dict[str, Any], library_id: str) -> ExternalWatchEvent | None:
    item = map_item(raw, library_id)
    if item.last_viewed_at is None or not item.view_count:
        return None
    return ExternalWatchEvent(
        media_external_id=item.external_id,
        library_external_id=library_id,
        watched_at=item.last_viewed_at,
        completed=True,
        source_event_id=f"jellyfin:{item.external_id}:{item.last_viewed_at.isoformat()}",
        duration_ms=item.duration_ms,
        view_number=item.view_count,
    )


def _identifiers(raw: Any) -> tuple[ExternalIdentifier, ...]:
    if not isinstance(raw, dict):
        return ()
    aliases = {
        "imdb": "imdb",
        "tmdb": "tmdb",
        "tvdb": "tvdb",
        "musicbrainztrack": "mbid",
        "musicbrainzalbum": "mbid-release",
        "musicbrainzartist": "mbid-artist",
    }
    values = []
    for key, value in raw.items():
        provider = aliases.get(str(key).casefold())
        external_id = _text(value)
        if provider and external_id:
            values.append(ExternalIdentifier(provider, external_id))
    return tuple(values)


def _required(raw: dict[str, Any], key: str) -> str:
    value = _text(raw.get(key))
    if value is None:
        raise ConnectorResponseError(f"Jellyfin omitted required field {key}.")
    return value


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _milliseconds(value: Any) -> int | None:
    ticks = _integer(value)
    return ticks // 10_000 if ticks is not None else None


def _timestamp(value: Any) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _date(value: Any) -> date | None:
    parsed = _timestamp(value)
    return parsed.date() if parsed else None


def _first_name(value: Any) -> str | None:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return _text(value[0].get("Name"))
    return None
