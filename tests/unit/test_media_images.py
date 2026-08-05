"""Filesystem artwork cache behavior."""

from pathlib import Path
from typing import cast

import httpx
import pytest

from euvieouvi.connectors.plex.connector import PlexConnector
from euvieouvi.database.models import MediaImage
from euvieouvi.media_images import ensure_cached, ensure_external_cached


class ImageConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def fetch_image(self, source_path: str, *, width: int, height: int) -> tuple[bytes, str]:
        self.calls.append((source_path, width, height))
        return b"small-jpeg", "image/jpeg"


def test_image_is_written_atomically_and_reused(tmp_path: Path) -> None:
    image = MediaImage(
        media_item_id=10,
        source_id=1,
        image_type="poster",
        source_path="/library/metadata/10/thumb",
        cache_status="pending",
    )
    connector = ImageConnector()
    cache = tmp_path / "images"
    first = ensure_cached(
        image,
        cast(PlexConnector, connector),
        cache,
        width=300,
        height=450,
    )
    second = ensure_cached(
        image,
        cast(PlexConnector, connector),
        cache,
        width=300,
        height=450,
    )
    assert first == second
    assert first.read_bytes() == b"small-jpeg"
    assert image.cache_status == "cached"
    assert image.mime_type == "image/jpeg"
    assert image.cached_at is not None
    assert connector.calls == [("/library/metadata/10/thumb", 300, 450)]


def test_allowlisted_external_image_is_cached(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "image.tmdb.org"
        return httpx.Response(
            200,
            content=b"external-jpeg",
            headers={"Content-Type": "image/jpeg"},
            request=request,
        )

    image = MediaImage(
        media_item_id=11,
        source_id=None,
        image_type="poster",
        provider="tmdb",
        source_url="https://image.tmdb.org/t/p/w500/poster.jpg",
        cache_status="pending",
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    path = ensure_external_cached(image, tmp_path / "images", client=http)
    assert path.read_bytes() == b"external-jpeg"
    assert image.cache_status == "cached"


def test_external_image_blocks_unapproved_redirect(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://evil.example/poster.jpg"},
            request=request,
        )

    image = MediaImage(
        media_item_id=12,
        source_id=None,
        image_type="poster",
        provider="coverartarchive",
        source_url=(
            "https://coverartarchive.org/release/9dbb5ea9-118b-4203-b093-bc4b14b8aa16/front-500"
        ),
        cache_status="pending",
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="host"):
        ensure_external_cached(image, tmp_path / "images", client=http)


def test_cover_art_archive_allows_valid_archive_redirect(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "coverartarchive.org":
            return httpx.Response(
                302,
                headers={"Location": "https://ia800.example.archive.org/file/front.jpg"},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"cover",
            headers={"Content-Type": "image/jpeg"},
            request=request,
        )

    image = MediaImage(
        media_item_id=13,
        source_id=None,
        image_type="poster",
        provider="coverartarchive",
        source_url=(
            "https://coverartarchive.org/release/9dbb5ea9-118b-4203-b093-bc4b14b8aa16/front-500"
        ),
        cache_status="pending",
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    path = ensure_external_cached(image, tmp_path / "images", client=http)
    assert path.read_bytes() == b"cover"


def test_external_cache_rejects_incomplete_entry(tmp_path: Path) -> None:
    image = MediaImage(
        media_item_id=14,
        source_id=None,
        image_type="poster",
        provider="unknown",
        source_url="https://example.com/image.jpg",
        cache_status="pending",
    )
    with pytest.raises(ValueError, match="incomplete"):
        ensure_external_cached(image, tmp_path / "images")
