"""Plex implementation of the neutral MediaConnector protocol."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime

from euvieouvi.connectors.dtos import (
    ConnectionInfo,
    ExternalIdentifier,
    ExternalLibrary,
    ExternalLibraryRef,
    ExternalLibraryRejection,
    ExternalMediaItem,
    ExternalMediaKind,
    ExternalUser,
    ExternalWatchEvent,
    HistoryCheckpoint,
    Page,
    PageIssue,
    PageRequest,
)
from euvieouvi.connectors.errors import (
    ConnectorConfigurationError,
    ConnectorPaginationError,
    ConnectorResponseError,
)
from euvieouvi.connectors.plex.client import PlexHttpClient
from euvieouvi.connectors.plex.mapper import (
    map_account_discovery,
    map_connection,
    map_library_discovery,
    map_media_item,
    map_watch_event,
    parse_container,
)

_LOGGER = logging.getLogger(__name__)
_PARENT_METADATA_BATCH_SIZE = 100


class PlexConnector:
    """Collect Plex data and return only neutral immutable values."""

    def __init__(self, client: PlexHttpClient, user_id: str | None = None) -> None:
        self._client = client
        self._user_id = (user_id or "").strip()
        self._last_pages: dict[str, tuple[int, tuple[str, ...]]] = {}
        self._show_identifiers: dict[str, tuple[ExternalIdentifier, ...]] = {}
        self._show_metadata: dict[str, ExternalMediaItem] = {}
        self.last_unsupported_libraries: tuple[ExternalLibraryRejection, ...] = ()

    def close(self) -> None:
        self._client.close()

    def test_connection(self) -> ConnectionInfo:
        container, _ = parse_container(self._client.get("/"))
        return map_connection(container)

    def list_users(self) -> tuple[ExternalUser, ...]:
        _, items = parse_container(self._client.get("/accounts"))
        return map_account_discovery(items)

    def list_libraries(self) -> list[ExternalLibrary]:
        _, items = parse_container(self._client.get("/library/sections"))
        libraries, rejected = map_library_discovery(items)
        self.last_unsupported_libraries = rejected
        return libraries

    def mark_watched(self, external_id: str, *, watched_at: datetime | None = None) -> None:
        del watched_at  # Plex's scrobble endpoint does not accept a historical play date.
        if not external_id.strip():
            raise ConnectorConfigurationError("Plex media id must not be empty.")
        self._client.get(
            "/:/scrobble",
            params={"key": external_id, "identifier": "com.plexapp.plugins.library"},
        )

    def set_progress(self, external_id: str, progress_ms: int) -> None:
        if not external_id.strip():
            raise ConnectorConfigurationError("Plex media id must not be empty.")
        if progress_ms < 0:
            raise ConnectorConfigurationError("Plex progress must not be negative.")
        self._client.get(
            "/:/progress",
            params={
                "key": external_id,
                "time": progress_ms,
                "state": "stopped",
                "identifier": "com.plexapp.plugins.library",
            },
        )

    def get_media_item(self, external_id: str, library_external_id: str = "") -> ExternalMediaItem:
        """Fetch one item so webhook handling can verify Plex's stored watch state."""
        if not external_id.strip():
            raise ConnectorConfigurationError("Plex media id must not be empty.")
        payload = self._client.get(f"/library/metadata/{external_id}")
        _, items = parse_container(payload)
        matching = next(
            (item for item in items if str(item.get("ratingKey") or "") == external_id),
            None,
        )
        if matching is None:
            raise ConnectorResponseError("Plex metadata omitted the requested media item.")
        resolved_library_id = (
            library_external_id.strip() or str(matching.get("librarySectionID") or "").strip()
        )
        if not resolved_library_id:
            raise ConnectorResponseError("Plex metadata omitted the item's library identity.")
        return map_media_item(matching, resolved_library_id)

    def fetch_image(self, source_path: str, *, width: int, height: int) -> tuple[bytes, str]:
        """Fetch a bounded, Plex-resized image without exposing the server token."""
        if not source_path.startswith("/") or source_path.startswith("//"):
            raise ConnectorConfigurationError("Plex image path must be local to the server.")
        payload = self._client.get(
            "/photo/:/transcode",
            params={
                "url": source_path,
                "width": width,
                "height": height,
                "minSize": 1,
                "upscale": 1,
            },
        )
        mime_type = payload.content_type.split(";", 1)[0].strip().lower()
        if mime_type not in {"image/jpeg", "image/png", "image/webp"} or not payload.content:
            raise ConnectorResponseError("Plex returned an unsupported image response.")
        return payload.content, mime_type

    def get_media_page(
        self,
        library: ExternalLibraryRef,
        media_kind: ExternalMediaKind,
        page: PageRequest,
    ) -> Page[ExternalMediaItem]:
        if (
            (media_kind is ExternalMediaKind.MOVIE and library.media_type.value != "movie")
            or (media_kind is ExternalMediaKind.EPISODE and library.media_type.value != "show")
            or (media_kind is ExternalMediaKind.TRACK and library.media_type.value != "artist")
        ):
            raise ConnectorConfigurationError("Plex media kind does not match the library type.")
        params = self._page_params(page)
        if page.updated_since is not None:
            params["updatedAt>"] = int(page.updated_since.timestamp())
        # Plex does not consistently include the nested Guid elements on
        # library listings unless they are explicitly requested. Those ids
        # let persistence reconcile localized titles across media servers.
        params["includeGuids"] = 1
        if media_kind is ExternalMediaKind.EPISODE:
            params["type"] = 4
        elif media_kind is ExternalMediaKind.TRACK:
            params["type"] = 10
        elif media_kind is not ExternalMediaKind.MOVIE:
            raise ConnectorConfigurationError("Plex media paging supports movies or episodes.")
        payload = self._client.get(f"/library/sections/{library.external_id}/all", params=params)
        container, items = parse_container(payload)
        mapped_items: list[ExternalMediaItem] = []
        issues: list[PageIssue] = []
        for item in items:
            try:
                mapped_items.append(map_media_item(item, library.external_id))
            except Exception as error:
                issue = _mapping_issue(item, "catalog_mapping", error)
                issues.append(issue)
                _LOGGER.warning("Plex catalog item skipped: %s", issue.message, exc_info=True)
        if media_kind is ExternalMediaKind.EPISODE:
            self._load_show_identifiers(mapped_items, library.external_id)
            mapped_items = [
                replace(
                    item,
                    show_identifiers=self._show_identifiers.get(item.show_external_id or "", ()),
                    show_metadata=self._show_metadata.get(item.show_external_id or ""),
                )
                for item in mapped_items
            ]
        mapped = tuple(mapped_items)
        result = replace(
            self._page(container, mapped, page, consumed_size=len(items)),
            issues=tuple(issues),
        )
        self._remember_page(
            f"media:{library.external_id}:{media_kind.value}",
            page.start,
            tuple(item.external_id for item in mapped),
        )
        return result

    def _load_show_identifiers(
        self, items: list[ExternalMediaItem], library_external_id: str
    ) -> None:
        """Fetch each page's parent shows in batches so localized titles share identity."""
        missing = sorted(
            {
                item.show_external_id
                for item in items
                if item.show_external_id is not None
                and item.show_external_id not in self._show_identifiers
            }
        )
        for offset in range(0, len(missing), _PARENT_METADATA_BATCH_SIZE):
            batch = missing[offset : offset + _PARENT_METADATA_BATCH_SIZE]
            try:
                payload = self._client.get(
                    f"/library/metadata/{','.join(batch)}", params={"includeGuids": 1}
                )
                _, parents = parse_container(payload)
                returned: set[str] = set()
                for parent in parents:
                    try:
                        mapped = map_media_item(parent, library_external_id)
                    except Exception:
                        _LOGGER.warning(
                            "Plex parent show metadata could not be mapped: library=%s "
                            "external_id=%s",
                            library_external_id,
                            parent.get("ratingKey"),
                            exc_info=True,
                        )
                        continue
                    if mapped.kind is not ExternalMediaKind.SHOW:
                        continue
                    returned.add(mapped.external_id)
                    self._show_identifiers[mapped.external_id] = mapped.identifiers
                    self._show_metadata[mapped.external_id] = mapped
                for external_id in set(batch) - returned:
                    self._show_identifiers[external_id] = ()
            except Exception:
                _LOGGER.warning(
                    "Plex parent show identifier lookup failed: library=%s count=%d",
                    library_external_id,
                    len(batch),
                    exc_info=True,
                )
                for external_id in batch:
                    self._show_identifiers[external_id] = ()

    def get_history_page(
        self,
        library: ExternalLibraryRef,
        checkpoint: HistoryCheckpoint | None,
        page: PageRequest,
    ) -> Page[ExternalWatchEvent]:
        current_start = page.start
        while True:
            current_page = PageRequest(start=current_start, size=page.size)
            params = self._page_params(current_page)
            params["librarySectionID"] = library.external_id
            if self._user_id:
                params["accountID"] = self._user_id
            params["sort"] = "viewedAt:asc"
            if checkpoint is not None and checkpoint.watermark_at is not None:
                params["viewedAt>"] = int(checkpoint.watermark_at.timestamp())
            payload = self._client.get("/status/sessions/history/all", params=params)
            container, items = parse_container(payload)
            identified = tuple(item for item in items if _has_text(item.get("ratingKey")))
            skipped = len(items) - len(identified)
            if skipped:
                _LOGGER.warning(
                    "Plex history omitted media identity; skipped orphaned events "
                    "library=%s start=%d count=%d",
                    library.external_id,
                    current_start,
                    skipped,
                )
            mapped_events: list[ExternalWatchEvent] = []
            issues: list[PageIssue] = []
            for item in identified:
                try:
                    mapped_events.append(
                        replace(
                            map_watch_event(item, library.external_id),
                            playback_user=self._user_id or None,
                        )
                    )
                except Exception as error:
                    issue = _mapping_issue(item, "history_mapping", error)
                    issues.append(issue)
                    _LOGGER.warning("Plex history item skipped: %s", issue.message, exc_info=True)
            mapped = tuple(mapped_events)
            result = self._page(
                container,
                mapped,
                current_page,
                consumed_size=len(items),
            )
            result = replace(result, issues=tuple(issues))
            if result.items or result.next_start is None:
                break
            current_start = result.next_start
        self._remember_page(
            f"history:{library.external_id}",
            result.start,
            tuple(
                item.source_event_id or f"{item.media_external_id}:{item.watched_at.isoformat()}"
                for item in mapped
            ),
        )
        return result

    def _remember_page(self, stream: str, start: int, fingerprint: tuple[str, ...]) -> None:
        previous = self._last_pages.get(stream)
        if (
            fingerprint
            and previous is not None
            and previous[0] != start
            and previous[1] == fingerprint
        ):
            raise ConnectorPaginationError("Plex repeated a page without advancing its contents.")
        self._last_pages[stream] = (start, fingerprint)

    @staticmethod
    def _page_params(page: PageRequest) -> dict[str, str | int]:
        return {
            "X-Plex-Container-Start": page.start,
            "X-Plex-Container-Size": page.size,
        }

    @staticmethod
    def _page[ItemT](
        container: dict[str, object],
        items: tuple[ItemT, ...],
        request: PageRequest,
        *,
        consumed_size: int | None = None,
    ) -> Page[ItemT]:
        reported_start = _optional_int(container.get("offset"))
        if reported_start is not None and reported_start != request.start:
            raise ConnectorPaginationError("Plex returned a page without the requested offset.")
        total_size = _optional_int(container.get("totalSize"))
        effective_size = len(items)
        advance_size = effective_size if consumed_size is None else consumed_size
        if advance_size < effective_size or advance_size > request.size:
            raise ConnectorPaginationError("Plex returned an invalid page size.")
        next_start = request.start + advance_size if advance_size == request.size else None
        return Page(
            items=items,
            start=request.start,
            size=effective_size,
            total_size=total_size,
            next_start=next_start,
        )


def _mapping_issue(item: dict[str, object], category: str, error: Exception) -> PageIssue:
    external_id = str(item.get("ratingKey") or "").strip() or None
    title = str(item.get("title") or item.get("grandparentTitle") or "").strip() or None
    context = f"externo={external_id or '?'} · título={title or '?'}"
    return PageIssue(
        external_id,
        title,
        category,
        f"{context} · erro={type(error).__name__}: {error}"[:1000],
    )


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConnectorPaginationError("Plex returned invalid pagination metadata.")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ConnectorPaginationError("Plex returned invalid pagination metadata.") from error
    if result < 0:
        raise ConnectorPaginationError("Plex returned negative pagination metadata.")
    return result


def _has_text(value: object) -> bool:
    return (
        isinstance(value, (str, int)) and not isinstance(value, bool) and bool(str(value).strip())
    )
