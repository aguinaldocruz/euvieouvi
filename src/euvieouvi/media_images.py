"""Persistent filesystem cache for bounded artwork obtained from Plex."""

from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from euvieouvi.database.models import MediaImage

_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_MAX_EXTERNAL_BYTES = 5 * 1024 * 1024


class ImageConnector(Protocol):
    def fetch_image(self, source_path: str, *, width: int, height: int) -> tuple[bytes, str]: ...


def ensure_cached(
    image: MediaImage,
    connector: ImageConnector,
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
    if image.source_path is None:
        raise ValueError("Media cache entry omitted its source path")
    content, mime_type = connector.fetch_image(image.source_path, width=width, height=height)
    digest = hashlib.sha256(
        f"{image.source_id}:{image.media_item_id}:{image.image_type}:{image.source_path}".encode()
    ).hexdigest()[:32]
    return _store(image, cache_directory, content, mime_type, digest)


def ensure_external_cached(
    image: MediaImage,
    cache_directory: Path,
    *,
    client: httpx.Client | None = None,
) -> Path:
    """Cache one allowlisted external image with bounded redirects and response size."""
    cache_directory.mkdir(parents=True, exist_ok=True)
    if image.local_filename:
        existing = cache_directory / image.local_filename
        if existing.is_file():
            return existing
    if image.source_url is None or image.provider not in {"tmdb", "coverartarchive"}:
        raise ValueError("External cache entry is incomplete or unsupported")
    content, mime_type = _download_external(image.source_url, client=client)
    digest = hashlib.sha256(
        f"{image.provider}:{image.media_item_id}:{image.image_type}:{image.source_url}".encode()
    ).hexdigest()[:32]
    return _store(image, cache_directory, content, mime_type, digest)


def _store(
    image: MediaImage,
    cache_directory: Path,
    content: bytes,
    mime_type: str,
    digest: str,
) -> Path:
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


def _download_external(url: str, *, client: httpx.Client | None = None) -> tuple[bytes, str]:
    current = url
    owned = client is None
    http = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
    try:
        for _ in range(4):
            _validate_external_url(current)
            with http.stream(
                "GET", current, headers={"User-Agent": "euvieouvi/external-image"}
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("External image redirect omitted its destination")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                mime_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if mime_type not in _EXTENSIONS:
                    raise ValueError("External image returned an unsupported MIME type")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_EXTERNAL_BYTES:
                        raise ValueError("External image exceeded the size limit")
                    chunks.append(chunk)
                content = b"".join(chunks)
                if not content:
                    raise ValueError("External image was empty")
                return content, mime_type
        raise ValueError("External image redirect limit exceeded")
    except httpx.HTTPError as error:
        raise ValueError("External image request failed") from error
    finally:
        if owned:
            http.close()


def _validate_external_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("External image URL is not allowed")
    if host == "image.tmdb.org" and parsed.path.startswith("/t/p/w500/"):
        return
    if host == "coverartarchive.org" and parsed.path.startswith("/release/"):
        return
    if host == "archive.org" or host.endswith(".archive.org"):
        return
    raise ValueError("External image host is not allowed")
