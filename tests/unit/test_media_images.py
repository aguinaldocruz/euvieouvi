"""Filesystem artwork cache behavior."""

from pathlib import Path
from typing import cast

from euvieouvi.connectors.plex.connector import PlexConnector
from euvieouvi.database.models import MediaImage
from euvieouvi.media_images import ensure_cached


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
