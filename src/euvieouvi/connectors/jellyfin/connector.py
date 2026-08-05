"""Jellyfin implementation of the media connector protocol."""

from __future__ import annotations

from typing import Any

from euvieouvi.connectors.dtos import (
    ConnectionInfo,
    ExternalLibrary,
    ExternalLibraryRef,
    ExternalMediaItem,
    ExternalMediaKind,
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

    def get_media_page(
        self,
        library: ExternalLibraryRef,
        media_kind: ExternalMediaKind,
        page: PageRequest,
    ) -> Page[ExternalMediaItem]:
        raw = self._items(library, media_kind, page, played_only=False)
        mapped = tuple(map_item(value, library.external_id) for value in raw["items"])
        return Page(
            mapped,
            page.start,
            len(mapped),
            total_size=raw["total"],
            next_start=page.start + len(mapped)
            if page.start + len(mapped) < raw["total"]
            else None,
        )

    def get_history_page(
        self,
        library: ExternalLibraryRef,
        checkpoint: HistoryCheckpoint | None,
        page: PageRequest,
    ) -> Page[ExternalWatchEvent]:
        del checkpoint
        kind = {
            "movie": ExternalMediaKind.MOVIE,
            "show": ExternalMediaKind.EPISODE,
            "artist": ExternalMediaKind.TRACK,
        }[library.media_type.value]
        raw = self._items(library, kind, page, played_only=True)
        mapped = tuple(
            event
            for value in raw["items"]
            if (event := map_history_item(value, library.external_id)) is not None
        )
        return Page(
            mapped,
            page.start,
            len(mapped),
            total_size=raw["total"],
            next_start=page.start + len(raw["items"])
            if page.start + len(raw["items"]) < raw["total"]
            else None,
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
        raw = self._client.get_json(f"/Users/{self._user_id}/Items", params=params)
        if not isinstance(raw, dict) or not isinstance(raw.get("Items"), list):
            raise ConnectorResponseError("Jellyfin item response was invalid.")
        items = [value for value in raw["Items"] if isinstance(value, dict)]
        total = raw.get("TotalRecordCount")
        return {"items": items, "total": total if isinstance(total, int) else len(items)}
