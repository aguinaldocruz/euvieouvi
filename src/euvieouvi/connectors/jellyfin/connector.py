"""Jellyfin implementation of the media connector protocol."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from euvieouvi.connectors.dtos import (
    ConnectionInfo,
    ExternalLibrary,
    ExternalLibraryRef,
    ExternalMediaItem,
    ExternalMediaKind,
    ExternalUser,
    ExternalWatchEvent,
    HistoryCheckpoint,
    Page,
    PageRequest,
)
from euvieouvi.connectors.errors import ConnectorResponseError
from euvieouvi.connectors.jellyfin.client import JellyfinHttpClient
from euvieouvi.connectors.jellyfin.mapper import (
    map_history_item,
    map_item,
    map_libraries,
)

_INCLUDE_TYPES = {
    ExternalMediaKind.MOVIE: "Movie",
    ExternalMediaKind.EPISODE: "Episode",
    ExternalMediaKind.TRACK: "Audio",
}
_FIELDS = ",".join(
    [
        "ProviderIds",
        "Overview",
        "Genres",
        "Studios",
        "Taglines",
        "DateCreated",
        "DateLastSaved",
        "MediaSources",
        "ParentId",
    ]
)


class JellyfinConnector:
    def __init__(self, client: JellyfinHttpClient, user_id: str) -> None:
        if not user_id.strip():
            raise ValueError("Jellyfin user id must not be empty")
        self._client = client
        self._user_id = user_id.strip()
        self._series_provider_ids: dict[str, Any] = {}

    def close(self) -> None:
        self._client.close()

    def test_connection(self) -> ConnectionInfo:
        raw = self._client.get_json("/System/Info")
        if not isinstance(raw, dict):
            raise ConnectorResponseError("Jellyfin system information was invalid.")
        return ConnectionInfo(
            str(raw.get("ServerName") or "Jellyfin"),
            str(raw.get("Id") or raw.get("ServerId") or "jellyfin"),
            True,
            str(raw.get("Version")) if raw.get("Version") else None,
            frozenset({"libraries", "history", "images", "webhooks"}),
        )

    def list_libraries(self) -> list[ExternalLibrary]:
        raw = self._client.get_json(f"/Users/{self._user_id}/Views")
        if not isinstance(raw, dict) or not isinstance(raw.get("Items"), list):
            raise ConnectorResponseError("Jellyfin library response was invalid.")
        return map_libraries(raw["Items"])

    def list_users(self) -> tuple[ExternalUser, ...]:
        raw = self._client.get_json("/Users")
        if not isinstance(raw, list):
            raise ConnectorResponseError("Jellyfin user response was invalid.")
        users: list[ExternalUser] = []
        for value in raw:
            if not isinstance(value, dict):
                continue
            external_id = str(value.get("Id") or "").strip().replace("-", "").lower()
            name = str(value.get("Name") or "").strip()
            if external_id and name:
                users.append(ExternalUser(external_id, name))
        return tuple(sorted(users, key=lambda user: user.name.casefold()))

    def get_media_page(
        self,
        library: ExternalLibraryRef,
        media_kind: ExternalMediaKind,
        page: PageRequest,
    ) -> Page[ExternalMediaItem]:
        raw = self._items(library, media_kind, page, played_only=False)
        mapped = tuple(self._map_valid_items(raw["items"], library.external_id))
        return Page(
            mapped,
            page.start,
            len(mapped),
            total_size=raw["total"],
            next_start=page.start + len(raw["items"])
            if page.start + len(raw["items"]) < raw["total"]
            else None,
        )

    def get_history_page(
        self,
        library: ExternalLibraryRef,
        checkpoint: HistoryCheckpoint | None,
        page: PageRequest,
    ) -> Page[ExternalWatchEvent]:
        kind = {
            "movie": ExternalMediaKind.MOVIE,
            "show": ExternalMediaKind.EPISODE,
            "artist": ExternalMediaKind.TRACK,
        }[library.media_type.value]
        effective_page = replace(
            page,
            updated_since=checkpoint.watermark_at if checkpoint is not None else None,
        )
        raw = self._items(
            library, kind, effective_page, played_only=True, updated_for_user=True
        )
        mapped = tuple(self._map_valid_history(raw["items"], library.external_id))
        return Page(
            mapped,
            page.start,
            len(mapped),
            total_size=raw["total"],
            next_start=page.start + len(raw["items"])
            if page.start + len(raw["items"]) < raw["total"]
            else None,
        )

    def mark_watched(
        self, external_id: str, *, watched_at: datetime | None = None
    ) -> None:
        if not external_id.strip():
            raise ValueError("Jellyfin media id must not be empty")
        params = None
        if watched_at is not None:
            params = {
                "datePlayed": watched_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
            }
        self._client.post_empty(
            f"/Users/{self._user_id}/PlayedItems/{external_id}", params=params
        )

    def fetch_image(self, source_path: str, *, width: int, height: int) -> tuple[bytes, str]:
        return self._client.get_image(source_path, width=width, height=height)

    def _items(
        self,
        library: ExternalLibraryRef,
        kind: ExternalMediaKind,
        page: PageRequest,
        *,
        played_only: bool,
        updated_for_user: bool = False,
    ) -> dict[str, Any]:
        include_type = _INCLUDE_TYPES.get(kind)
        if include_type is None:
            raise ConnectorResponseError("Jellyfin media kind is unsupported.")
        params: dict[str, str | int | bool] = {
            "ParentId": library.external_id,
            "Recursive": True,
            "IncludeItemTypes": include_type,
            "Fields": _FIELDS,
            "EnableUserData": True,
            "StartIndex": page.start,
            "Limit": page.size,
            "SortBy": "SortName",
            "SortOrder": "Ascending",
        }
        if played_only:
            params["IsPlayed"] = True
            params["SortBy"] = "DatePlayed"
            params["SortOrder"] = "Ascending"
        if page.updated_since is not None:
            params[
                "MinDateLastSavedForUser" if updated_for_user else "MinDateLastSaved"
            ] = page.updated_since.isoformat().replace("+00:00", "Z")
        raw = self._client.get_json(f"/Users/{self._user_id}/Items", params=params)
        if not isinstance(raw, dict) or not isinstance(raw.get("Items"), list):
            raise ConnectorResponseError("Jellyfin item response was invalid.")
        items = [value for value in raw["Items"] if isinstance(value, dict)]
        total = raw.get("TotalRecordCount")
        return {"items": items, "total": total if isinstance(total, int) else len(items)}

    def _map_valid_items(
        self, items: list[dict[str, Any]], library_id: str
    ) -> Iterator[ExternalMediaItem]:
        for value in items:
            try:
                yield map_item(self._with_series_provider_ids(value), library_id)
            except (ConnectorResponseError, ValueError):
                continue

    def _with_series_provider_ids(self, value: dict[str, Any]) -> dict[str, Any]:
        series_id = value.get("SeriesId")
        if not isinstance(series_id, str) or not series_id.strip():
            return value
        if series_id not in self._series_provider_ids:
            raw = self._client.get_json(f"/Users/{self._user_id}/Items/{series_id}")
            self._series_provider_ids[series_id] = (
                raw.get("ProviderIds") if isinstance(raw, dict) else None
            )
        enriched = dict(value)
        enriched["_SeriesProviderIds"] = self._series_provider_ids[series_id]
        return enriched

    def _map_valid_history(
        self, items: list[dict[str, Any]], library_id: str
    ) -> Iterator[ExternalWatchEvent]:
        for value in items:
            try:
                event = map_history_item(value, library_id)
            except (ConnectorResponseError, ValueError):
                continue
            if event is not None:
                yield replace(event, playback_user=self._user_id)
