"""Plex implementation of the neutral MediaConnector protocol."""

from __future__ import annotations

from euvieouvi.connectors.dtos import (
    ConnectionInfo,
    ExternalLibrary,
    ExternalLibraryRef,
    ExternalLibraryRejection,
    ExternalMediaItem,
    ExternalMediaKind,
    ExternalWatchEvent,
    HistoryCheckpoint,
    Page,
    PageRequest,
)
from euvieouvi.connectors.errors import ConnectorConfigurationError, ConnectorPaginationError
from euvieouvi.connectors.plex.client import PlexHttpClient
from euvieouvi.connectors.plex.mapper import (
    map_connection,
    map_library_discovery,
    map_media_item,
    map_watch_event,
    parse_container,
)


class PlexConnector:
    """Collect Plex data and return only neutral immutable values."""

    def __init__(self, client: PlexHttpClient) -> None:
        self._client = client
        self._last_pages: dict[str, tuple[int, tuple[str, ...]]] = {}
        self.last_unsupported_libraries: tuple[ExternalLibraryRejection, ...] = ()

    def test_connection(self) -> ConnectionInfo:
        container, _ = parse_container(self._client.get("/"))
        return map_connection(container)

    def list_libraries(self) -> list[ExternalLibrary]:
        _, items = parse_container(self._client.get("/library/sections"))
        libraries, rejected = map_library_discovery(items)
        self.last_unsupported_libraries = rejected
        return libraries

    def get_media_page(
        self,
        library: ExternalLibraryRef,
        media_kind: ExternalMediaKind,
        page: PageRequest,
    ) -> Page[ExternalMediaItem]:
        if (media_kind is ExternalMediaKind.MOVIE and library.media_type.value != "movie") or (
            media_kind is ExternalMediaKind.EPISODE and library.media_type.value != "show"
        ):
            raise ConnectorConfigurationError("Plex media kind does not match the library type.")
        params = self._page_params(page)
        if media_kind is ExternalMediaKind.EPISODE:
            params["type"] = 4
        elif media_kind is not ExternalMediaKind.MOVIE:
            raise ConnectorConfigurationError("Plex media paging supports movies or episodes.")
        payload = self._client.get(f"/library/sections/{library.external_id}/all", params=params)
        container, items = parse_container(payload)
        mapped = tuple(map_media_item(item, library.external_id) for item in items)
        result = self._page(container, mapped, page)
        self._remember_page(
            f"media:{library.external_id}:{media_kind.value}",
            page.start,
            tuple(item.external_id for item in mapped),
        )
        return result

    def get_history_page(
        self,
        library: ExternalLibraryRef,
        checkpoint: HistoryCheckpoint | None,
        page: PageRequest,
    ) -> Page[ExternalWatchEvent]:
        del checkpoint  # A full defensive scan remains correct until a reliable filter is adopted.
        params = self._page_params(page)
        params["librarySectionID"] = library.external_id
        params["sort"] = "viewedAt:asc"
        payload = self._client.get("/status/sessions/history/all", params=params)
        container, items = parse_container(payload)
        mapped = tuple(map_watch_event(item, library.external_id) for item in items)
        result = self._page(container, mapped, page)
        self._remember_page(
            f"history:{library.external_id}",
            page.start,
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
        container: dict[str, object], items: tuple[ItemT, ...], request: PageRequest
    ) -> Page[ItemT]:
        reported_start = _optional_int(container.get("offset"))
        if reported_start is not None and reported_start != request.start:
            raise ConnectorPaginationError("Plex returned a page without the requested offset.")
        total_size = _optional_int(container.get("totalSize"))
        effective_size = len(items)
        next_start = request.start + effective_size if effective_size == request.size else None
        return Page(
            items=items,
            start=request.start,
            size=effective_size,
            total_size=total_size,
            next_start=next_start,
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
