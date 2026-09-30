"""Conservative physical-file identity with explicitly configured mount roots."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from euvieouvi.database.models import MediaFilePath, Setting, SourceMediaRef

ROOTS_KEY = "identity.path_roots"


def normalize_path(path: str) -> str | None:
    path = path.strip().replace("\\", "/")
    if not path or "\x00" in path or "://" in path:
        return None
    if not path.startswith("/") and not re.match(r"^[A-Za-z]:/", path):
        return None
    # Linux paths are case sensitive; do not collapse distinct files.
    return posixpath.normpath(path)


def canonical_path(path: str, roots: list[dict[str, str]]) -> str | None:
    normalized = normalize_path(path)
    if normalized is None:
        return None
    for mapping in sorted(roots, key=lambda value: len(value["source"]), reverse=True):
        source = mapping["source"].rstrip("/")
        if normalized == source or normalized.startswith(source + "/"):
            return posixpath.normpath(mapping["canonical"].rstrip("/") + normalized[len(source) :])
    return normalized


def load_roots(session: Session, source_id: int) -> list[dict[str, str]]:
    setting = session.get(Setting, ROOTS_KEY)
    values = json.loads(setting.value) if setting and setting.value else {}
    return list(values.get(str(source_id), []))


def path_keys(paths: tuple[str, ...], roots: list[dict[str, str]]) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for original in paths:
        canonical = canonical_path(original, roots)
        if canonical:
            result[hashlib.sha256(canonical.encode()).hexdigest()] = (original, canonical)
    return result


def save_paths(
    session: Session, reference: SourceMediaRef, keys: dict[str, tuple[str, str]]
) -> None:
    session.flush()
    existing = {
        row.path_key: row
        for row in session.scalars(
            select(MediaFilePath).where(MediaFilePath.reference_id == reference.id)
        )
    }
    for key, (original, canonical) in keys.items():
        row = existing.pop(key, None)
        if row is None:
            session.add(
                MediaFilePath(
                    reference_id=reference.id,
                    path_key=key,
                    original_path=original,
                    canonical_path=canonical,
                )
            )
        else:
            row.original_path, row.canonical_path = original, canonical
    for row in existing.values():
        session.delete(row)


def remap_paths(session: Session) -> int:
    """Reindex persisted evidence immediately after a mount configuration change."""
    count = 0
    roots_by_source: dict[int, list[dict[str, str]]] = {}
    for reference in session.scalars(
        select(SourceMediaRef).where(SourceMediaRef.id.in_(select(MediaFilePath.reference_id)))
    ):
        roots = roots_by_source.setdefault(
            reference.source_id, load_roots(session, reference.source_id)
        )
        paths = tuple(
            session.scalars(
                select(MediaFilePath.original_path).where(
                    MediaFilePath.reference_id == reference.id
                )
            )
        )
        save_paths(session, reference, path_keys(paths, roots))
        count += 1
    return count
