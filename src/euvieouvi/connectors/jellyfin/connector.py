"""Jellyfin implementation of the media connector protocol."""

from __future__ import annotations

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
    PageIssue,
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
        "Path",
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

    def get_media_item(self, external_id: str, library_external_id: str) -> ExternalMediaItem:
        """Fetch one item so a webhook can recover a missing catalog reference."""
        if not external_id.strip():
            raise ValueError("Jellyfin media id must not be empty")
        raw = self._client.get_json(f"/Users/{self._user_id}/Items/{external_id}")
        if not isinstance(raw, dict):
            raise ConnectorResponseError("Jellyfin item response was invalid.")
        enriched, _ = self._with_series_provider_ids(raw)
        return map_item(enriched, library_external_id)

    def get_media_page(
        self,
        library: ExternalLibraryRef,
        media_kind: ExternalMediaKind,
        page: PageRequest,
    ) -> Page[ExternalMediaItem]:
        raw = self._items(library, media_kind, page, played_only=False)
        mapped, issues = self._map_valid_items(raw["items"], library.external_id)
        return Page(
            mapped,
            page.start,
            len(mapped),
            total_size=raw["total"],
            next_start=page.start + len(raw["items"])
            if page.start + len(raw["items"]) < raw["total"]
            else None,
            issues=issues,
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
        raw = self._items(library, kind, effective_page, played_only=True, updated_for_user=True)
        observed_at = datetime.now(UTC)
        mapped, issues = self._map_valid_history(
            raw["items"], library.external_id, observed_at=observed_at
        )
        watermark = checkpoint.watermark_at if checkpoint is not None else None
        recent = tuple(
            event for event in mapped if watermark is None or event.watched_at >= watermark
        )
        reached_watermark = watermark is not None and any(
            event.watched_at < watermark for event in mapped
        )
        return Page(
            recent,
            page.start,
            len(recent),
            total_size=raw["total"],
            next_start=page.start + len(raw["items"])
            if page.start + len(raw["items"]) < raw["total"] and not reached_watermark
            else None,
            issues=issues,
        )

    def mark_watched(self, external_id: str, *, watched_at: datetime | None = None) -> None:
        if not external_id.strip():
            raise ValueError("Jellyfin media id must not be empty")
        params = None
        if watched_at is not None:
            params = {"datePlayed": watched_at.astimezone(UTC).isoformat().replace("+00:00", "Z")}
        self._client.post_empty(f"/Users/{self._user_id}/PlayedItems/{external_id}", params=params)

    def set_progress(self, external_id: str, progress_ms: int) -> None:
        if not external_id.strip():
            raise ValueError("Jellyfin media id must not be empty")
        if progress_ms < 0:
            raise ValueError("Jellyfin progress must not be negative")
        self._client.post_json(
            f"/UserItems/{external_id}/UserData",
            {"PlaybackPositionTicks": progress_ms * 10_000, "Played": False},
            params={"userId": self._user_id},
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
            params["SortOrder"] = "Descending" if updated_for_user else "Ascending"
        if page.updated_since is not None and not updated_for_user:
            params["MinDateLastSaved"] = page.updated_since.isoformat().replace("+00:00", "Z")
        raw = self._client.get_json(f"/Users/{self._user_id}/Items", params=params)
        if not isinstance(raw, dict) or not isinstance(raw.get("Items"), list):
            raise ConnectorResponseError("Jellyfin item response was invalid.")
        items = [value for value in raw["Items"] if isinstance(value, dict)]
        total = raw.get("TotalRecordCount")
        return {"items": items, "total": total if isinstance(total, int) else len(items)}

    def _map_valid_items(
        self, items: list[dict[str, Any]], library_id: str
    ) -> tuple[tuple[ExternalMediaItem, ...], tuple[PageIssue, ...]]:
        mapped: list[ExternalMediaItem] = []
        issues: list[PageIssue] = []
        for value in items:
            try:
                enriched, enrichment_issue = self._with_series_provider_ids(value)
                if enrichment_issue is not None:
                    issues.append(enrichment_issue)
                mapped.append(map_item(enriched, library_id))
            except Exception as error:
                issues.append(_mapping_issue(value, "catalog_mapping", error))
        return tuple(mapped), tuple(issues)

    def _with_series_provider_ids(
        self, value: dict[str, Any]
    ) -> tuple[dict[str, Any], PageIssue | None]:
        series_id = value.get("SeriesId")
        if not isinstance(series_id, str) or not series_id.strip():
            return value, None
        issue = None
        if series_id not in self._series_provider_ids:
            try:
                raw = self._client.get_json(f"/Users/{self._user_id}/Items/{series_id}")
                self._series_provider_ids[series_id] = (
                    raw.get("ProviderIds") if isinstance(raw, dict) else None
                )
            except Exception as error:
                self._series_provider_ids[series_id] = None
                issue = _mapping_issue(value, "series_identifier_lookup", error)
        enriched = dict(value)
        enriched["_SeriesProviderIds"] = self._series_provider_ids[series_id]
        return enriched, issue

    def _map_valid_history(
        self,
        items: list[dict[str, Any]],
        library_id: str,
        *,
        observed_at: datetime,
    ) -> tuple[tuple[ExternalWatchEvent, ...], tuple[PageIssue, ...]]:
        mapped: list[ExternalWatchEvent] = []
        issues: list[PageIssue] = []
        for value in items:
            try:
                event = map_history_item(value, library_id, fallback_watched_at=observed_at)
            except Exception as error:
                issues.append(_mapping_issue(value, "history_mapping", error))
                continue
            if event is not None:
                mapped.append(replace(event, playback_user=self._user_id))
        return tuple(mapped), tuple(issues)


def _mapping_issue(value: dict[str, Any], category: str, error: Exception) -> PageIssue:
    external_id = str(value.get("Id") or "").strip() or None
    title = str(value.get("Name") or value.get("SeriesName") or "").strip() or None
    context = f"externo={external_id or '?'} · título={title or '?'}"
    return PageIssue(
        external_id,
        title,
        category,
        f"{context} · erro={type(error).__name__}: {error}"[:1000],
    )
