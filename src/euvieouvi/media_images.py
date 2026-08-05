"""Persistent filesystem cache for bounded artwork obtained from Plex."""

from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from euvieouvi.connectors.plex.connector import PlexConnector
from euvieouvi.database.models import MediaImage

_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def ensure_cached(
    image: MediaImage,
    connector: PlexConnector,
    cache_directory: Path,
    *,
    width: int,
    height: int,
) -> Path:
    """Return an existing cache file or download it using an atomic replacement."""
    cache_directory.mkdir(parents=True, exist_ok=True)
    if image.local_filename:
        existing = cache_directory / image.local_filename
        if existing.is_file():
            return existing
    content, mime_type = connector.fetch_image(image.source_path, width=width, height=height)
    digest = hashlib.sha256(
        f"{image.source_id}:{image.media_item_id}:{image.image_type}:{image.source_path}".encode()
    ).hexdigest()[:32]
    filename = f"{digest}{_EXTENSIONS[mime_type]}"
    destination = cache_directory / filename
    descriptor, temporary_name = tempfile.mkstemp(prefix=".image-", dir=cache_directory)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise
    image.local_filename = filename
    image.mime_type = mime_type
    image.cache_status = "cached"
    image.cached_at = datetime.now(UTC)
    return destination
