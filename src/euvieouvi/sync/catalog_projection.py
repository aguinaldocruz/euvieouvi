"""Persistent canonical catalog projection used by the interactive UI."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, delete, func, insert, literal, select
from sqlalchemy.orm import Session, aliased

from euvieouvi.database.enums import ConnectorType, MediaKind
from euvieouvi.database.models import (
    CatalogEntry,
    CatalogEntryMember,
    CatalogProjectionDirtyItem,
    MediaFilePath,
    MediaIdentifier,
    MediaItem,
    Setting,
    Source,
    SourceMediaRef,
    WatchEvent,
    WatchState,
)

PROJECTION_STATE_KEY = "catalog.projection.state"
PROJECTION_VERSION = 4
_CLEAN_STATE = f"clean:v{PROJECTION_VERSION}"
_STABLE_PROVIDERS = ("tmdb", "tvdb", "imdb", "musicbrainz")
_PROVIDER_PRIORITY = {provider: position for position, provider in enumerate(_STABLE_PROVIDERS)}
_CATALOG_KINDS = {
    MediaKind.MOVIE,
    MediaKind.SHOW,
    MediaKind.EPISODE,
    MediaKind.ARTIST,
    MediaKind.ALBUM,
    MediaKind.TRACK,
}


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    entries: int
    members: int
    duplicate_groups: int
    mode: str = "full"


@dataclass(slots=True)
class _Stats:
    completion_count: int = 0
    first_played_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_played_at: datetime | None = None
    progress_ms: int = 0
    child_count: int = 0
    completed_child_count: int = 0
    available: bool = False
    available_plex: bool = False
    available_jellyfin: bool = False


def ensure_catalog_projection(session: Session) -> ProjectionResult | None:
    """Rebuild a missing or dirty projection before serving the catalog."""
    session.flush()
    state = session.get(Setting, PROJECTION_STATE_KEY)
    media_count = int(
        session.scalar(select(func.count(MediaItem.id)).where(MediaItem.kind.in_(_CATALOG_KINDS)))
        or 0
    )
    entry_count = int(session.scalar(select(func.count(CatalogEntry.media_item_id))) or 0)
    if state is not None and state.value == _CLEAN_STATE and (entry_count > 0 or media_count == 0):
        return None
    if state is not None and state.value == "activity" and entry_count > 0:
        return refresh_catalog_activity(session)
    return rebuild_catalog_projection(session)


def rebuild_catalog_projection(session: Session) -> ProjectionResult:
    """Atomically rebuild canonical groups and their precomputed playback facts."""
    session.flush()
    item_rows = session.execute(
        select(
            MediaItem.id,
            MediaItem.kind,
            MediaItem.title,
            MediaItem.year,
            MediaItem.parent_id,
            MediaItem.manual_match_tmdb_id,
        )
    ).all()
    item_by_id = {int(row.id): row for row in item_rows}
    catalog_ids = {media_id for media_id, row in item_by_id.items() if row.kind in _CATALOG_KINDS}
    parents = {
        media_id: int(row.parent_id) if row.parent_id is not None else None
        for media_id, row in item_by_id.items()
    }

    source_ids_by_item: dict[int, set[int]] = {}
    available_sources: dict[int, set[int]] = {}
    for media_id, source_id, available in session.execute(
        select(
            SourceMediaRef.media_item_id, SourceMediaRef.source_id, SourceMediaRef.available
        ).distinct()
    ):
        source_ids_by_item.setdefault(int(media_id), set()).add(int(source_id))
        if available:
            available_sources.setdefault(int(media_id), set()).add(int(source_id))

    union = _UnionFind(catalog_ids)
    paths_by_item: dict[int, set[str]] = {}
    for media_id, key in session.execute(
        select(SourceMediaRef.media_item_id, MediaFilePath.path_key)
        .join(MediaFilePath, MediaFilePath.reference_id == SourceMediaRef.id)
        .where(SourceMediaRef.available.is_(True))
    ):
        if media_id in catalog_ids:
            paths_by_item.setdefault(media_id, set()).add(key)

    def safe_identity_group(members: list[int]) -> bool:
        manual_ids = {
            item_by_id[value].manual_match_tmdb_id
            for value in members
            if item_by_id[value].manual_match_tmdb_id
        }
        if len(manual_ids) > 1:
            return False
        known = [paths_by_item[value] for value in members if value in paths_by_item]
        if any(
            not left & right for index, left in enumerate(known) for right in known[index + 1 :]
        ):
            return False
        return _safe_group(members, source_ids_by_item, available_sources)

    identifiers_by_item: dict[int, set[tuple[str, str]]] = {}
    identity_members: dict[tuple[MediaKind, str, str], list[int]] = {}
    for media_id, provider, external_id in session.execute(
        select(
            MediaIdentifier.media_item_id,
            MediaIdentifier.provider,
            MediaIdentifier.external_id,
        ).where(MediaIdentifier.provider.in_(_STABLE_PROVIDERS))
    ):
        media_id = int(media_id)
        if media_id not in catalog_ids:
            continue
        identity = (str(provider), str(external_id))
        identifiers_by_item.setdefault(media_id, set()).add(identity)
        identity_key = (item_by_id[media_id].kind, *identity)
        identity_members.setdefault(identity_key, []).append(media_id)
    for members in identity_members.values():
        if safe_identity_group(members):
            for media_id in members[1:]:
                union.join(members[0], media_id)

    fallback_members: dict[tuple[MediaKind, str, int | None], list[int]] = {}
    for media_id in sorted(catalog_ids):
        if identifiers_by_item.get(media_id):
            continue
        row = item_by_id[media_id]
        fallback = (row.kind, _fallback_identity(media_id, item_by_id), row.year)
        fallback_members.setdefault(fallback, []).append(media_id)
    for members in fallback_members.values():
        if safe_identity_group(members):
            for media_id in members[1:]:
                union.join(members[0], media_id)

    candidate_groups: dict[int, list[int]] = {}
    for media_id in sorted(catalog_ids):
        candidate_groups.setdefault(union.find(media_id), []).append(media_id)
    groups = [
        safe_members
        for members in candidate_groups.values()
        for safe_members in (
            [members]
            if len(members) == 1 or safe_identity_group(members)
            else [[media_id] for media_id in members]
        )
    ]

    plex_ids = set(
        int(value)
        for value in session.scalars(
            select(SourceMediaRef.media_item_id)
            .join(Source, Source.id == SourceMediaRef.source_id)
            .where(Source.connector_type == ConnectorType.PLEX)
            .distinct()
        )
    )
    canonical_by_item: dict[int, int] = {}
    group_key_by_canonical: dict[int, str] = {}
    used_group_keys: set[str] = set()
    for members in groups:
        canonical_id = min(
            members,
            key=lambda value: (
                not bool(item_by_id[value].manual_match_tmdb_id),
                value not in plex_ids,
                value,
            ),
        )
        for media_id in members:
            canonical_by_item[media_id] = canonical_id
        identities = sorted(
            {
                identity
                for media_id in members
                for identity in identifiers_by_item.get(media_id, ())
            },
            key=lambda value: (_PROVIDER_PRIORITY[value[0]], value[1]),
        )
        canonical = item_by_id[canonical_id]
        raw_key = (
            f"{canonical.kind.value}\0{identities[0][0]}\0{identities[0][1]}"
            if identities
            else (
                f"{canonical.kind.value}\0title\0{_fallback_identity(canonical_id, item_by_id)}"
                f"\0{canonical.year or ''}"
            )
        )
        group_key = f"v{PROJECTION_VERSION}:" + hashlib.sha256(raw_key.encode()).hexdigest()
        if group_key in used_group_keys:
            group_key = (
                f"v{PROJECTION_VERSION}:"
                + hashlib.sha256(f"{raw_key}\0item:{canonical_id}".encode()).hexdigest()
            )
        used_group_keys.add(group_key)
        group_key_by_canonical[canonical_id] = group_key

    stats = {canonical_id: _Stats() for canonical_id in group_key_by_canonical}
    activity_by_item: dict[int, datetime] = {}
    for media_id, count, first_played, last_played in session.execute(
        select(
            WatchEvent.media_item_id,
            func.count(WatchEvent.id),
            func.min(WatchEvent.watched_at),
            func.max(WatchEvent.watched_at),
        )
        .where(WatchEvent.completed.is_(True))
        .group_by(WatchEvent.media_item_id)
    ):
        media_id = int(media_id)
        entry_id = canonical_by_item.get(media_id)
        if entry_id is not None:
            entry = stats[entry_id]
            entry.completion_count += int(count or 0)
            entry.first_played_at = _earliest(entry.first_played_at, first_played)
            entry.last_completed_at = _latest(entry.last_completed_at, last_played)
        if last_played is not None:
            activity_by_item[media_id] = (
                _latest(activity_by_item.get(media_id), last_played) or last_played
            )

    state_activity = _state_playback_time()
    for media_id, completed_count, completed_last, progress_ms, activity_last in session.execute(
        select(
            WatchState.media_item_id,
            func.max(case((WatchState.completed.is_(True), WatchState.view_count), else_=0)),
            func.max(
                case(
                    (WatchState.completed.is_(True), WatchState.last_watched_at),
                    else_=None,
                )
            ),
            func.max(case((WatchState.completed.is_(False), WatchState.progress_ms), else_=0)),
            func.max(state_activity),
        ).group_by(WatchState.media_item_id)
    ):
        media_id = int(media_id)
        entry_id = canonical_by_item.get(media_id)
        if entry_id is not None:
            entry = stats[entry_id]
            entry.completion_count = max(entry.completion_count, int(completed_count or 0))
            entry.last_completed_at = _latest(entry.last_completed_at, completed_last)
            entry.progress_ms = max(entry.progress_ms, int(progress_ms or 0))
        if activity_last is not None:
            activity_by_item[media_id] = (
                _latest(activity_by_item.get(media_id), activity_last) or activity_last
            )

    for media_id, played_at in activity_by_item.items():
        current_id: int | None = media_id
        visited: set[int] = set()
        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            entry_id = canonical_by_item.get(current_id)
            if entry_id is not None:
                stats[entry_id].last_played_at = _latest(stats[entry_id].last_played_at, played_at)
            current_id = parents.get(current_id)

    available_items: set[int] = set()
    for media_id, connector_type in session.execute(
        select(SourceMediaRef.media_item_id, Source.connector_type)
        .join(Source, Source.id == SourceMediaRef.source_id)
        .where(SourceMediaRef.available.is_(True))
        .distinct()
    ):
        media_id = int(media_id)
        available_items.add(media_id)
        entry_id = canonical_by_item.get(media_id)
        if entry_id is None:
            continue
        entry = stats[entry_id]
        entry.available = True
        entry.available_plex = entry.available_plex or connector_type is ConnectorType.PLEX
        entry.available_jellyfin = (
            entry.available_jellyfin or connector_type is ConnectorType.JELLYFIN
        )

    children_by_show: dict[int, set[int]] = {}
    for media_id in available_items:
        media_row = item_by_id.get(media_id)
        if media_row is None or media_row.kind is not MediaKind.EPISODE:
            continue
        episode_id = canonical_by_item.get(media_id)
        show_id = _ancestor_of_kind(media_id, MediaKind.SHOW, item_by_id, parents)
        canonical_show_id = canonical_by_item.get(show_id) if show_id is not None else None
        if episode_id is not None and canonical_show_id is not None:
            children_by_show.setdefault(canonical_show_id, set()).add(episode_id)
    for show_id, episode_ids in children_by_show.items():
        stats[show_id].child_count = len(episode_ids)
        stats[show_id].completed_child_count = sum(
            stats[episode_id].completion_count > 0 for episode_id in episode_ids
        )

    now = datetime.now(UTC)
    session.execute(delete(CatalogEntryMember))
    session.execute(delete(CatalogEntry))
    session.execute(delete(CatalogProjectionDirtyItem))
    entry_values = [
        {
            "media_item_id": canonical_id,
            "group_key": group_key_by_canonical[canonical_id],
            "kind": item_by_id[canonical_id].kind.value,
            "completion_count": entry.completion_count,
            "first_played_at": entry.first_played_at,
            "last_completed_at": entry.last_completed_at,
            "last_played_at": entry.last_played_at,
            "progress_ms": entry.progress_ms,
            "child_count": entry.child_count,
            "completed_child_count": entry.completed_child_count,
            "available": entry.available,
            "available_plex": entry.available_plex,
            "available_jellyfin": entry.available_jellyfin,
            "updated_at": now,
        }
        for canonical_id, entry in stats.items()
    ]
    member_values = [
        {"media_item_id": media_id, "catalog_media_item_id": canonical_id}
        for media_id, canonical_id in canonical_by_item.items()
    ]
    _bulk_insert(session, CatalogEntry, entry_values)
    _bulk_insert(session, CatalogEntryMember, member_values)
    _mark_clean(session, now)
    duplicate_groups = sum(len(members) > 1 for members in groups)
    return ProjectionResult(len(entry_values), len(member_values), duplicate_groups)


def refresh_catalog_activity(session: Session) -> ProjectionResult:
    """Refresh only entries affected by changed watch events or states."""
    session.flush()
    dirty_count = int(
        session.scalar(select(func.count()).select_from(CatalogProjectionDirtyItem)) or 0
    )
    if not dirty_count:
        return rebuild_catalog_projection(session)

    ancestors = (
        select(
            MediaItem.id.label("media_item_id"),
            MediaItem.parent_id.label("parent_id"),
            literal(0).label("depth"),
        )
        .join(CatalogProjectionDirtyItem, CatalogProjectionDirtyItem.media_item_id == MediaItem.id)
        .cte("catalog_dirty_ancestors", recursive=True)
    )
    parent_item = aliased(MediaItem)
    ancestors = ancestors.union_all(
        select(
            parent_item.id,
            parent_item.parent_id,
            (ancestors.c.depth + 1).label("depth"),
        )
        .join(ancestors, parent_item.id == ancestors.c.parent_id)
        .where(ancestors.c.depth < 4)
    )
    impacted_query = (
        select(CatalogEntryMember.catalog_media_item_id.label("catalog_media_item_id"))
        .join(ancestors, ancestors.c.media_item_id == CatalogEntryMember.media_item_id)
        .distinct()
        .subquery("impacted_catalog_entries")
    )
    impacted_entries = set(session.scalars(select(impacted_query.c.catalog_media_item_id)))
    if not impacted_entries:
        session.execute(delete(CatalogProjectionDirtyItem))
        _mark_clean(session)
        return ProjectionResult(0, dirty_count, 0, "activity")

    tree = (
        select(
            MediaItem.id.label("media_item_id"),
            MediaItem.kind.label("kind"),
            MediaItem.parent_id.label("parent_id"),
            literal(0).label("depth"),
        )
        .join(CatalogEntryMember, CatalogEntryMember.media_item_id == MediaItem.id)
        .join(
            impacted_query,
            impacted_query.c.catalog_media_item_id == CatalogEntryMember.catalog_media_item_id,
        )
        .cte("catalog_activity_tree", recursive=True)
    )
    child_item = aliased(MediaItem)
    tree = tree.union_all(
        select(
            child_item.id,
            child_item.kind,
            child_item.parent_id,
            (tree.c.depth + 1).label("depth"),
        )
        .join(tree, child_item.parent_id == tree.c.media_item_id)
        .where(tree.c.depth < 4)
    )
    item_rows = session.execute(
        select(tree.c.media_item_id, tree.c.kind, tree.c.parent_id).distinct()
    ).all()
    item_by_id = {int(row.media_item_id): row for row in item_rows}
    parents = {
        media_id: int(row.parent_id) if row.parent_id is not None else None
        for media_id, row in item_by_id.items()
    }
    tree_ids_query = (
        select(tree.c.media_item_id.label("media_item_id"))
        .distinct()
        .subquery("catalog_activity_ids")
    )
    canonical_by_item = {
        int(media_id): int(catalog_id)
        for media_id, catalog_id in session.execute(
            select(
                CatalogEntryMember.media_item_id,
                CatalogEntryMember.catalog_media_item_id,
            ).join(
                tree_ids_query,
                tree_ids_query.c.media_item_id == CatalogEntryMember.media_item_id,
            )
        )
    }
    direct_ids = {
        media_id for media_id, entry_id in canonical_by_item.items() if entry_id in impacted_entries
    }
    stats = {entry_id: _Stats() for entry_id in impacted_entries}
    activity_by_item: dict[int, datetime] = {}
    for media_id, count, first_played, last_played in session.execute(
        select(
            WatchEvent.media_item_id,
            func.count(WatchEvent.id),
            func.min(WatchEvent.watched_at),
            func.max(WatchEvent.watched_at),
        )
        .join(tree_ids_query, tree_ids_query.c.media_item_id == WatchEvent.media_item_id)
        .where(
            WatchEvent.completed.is_(True),
        )
        .group_by(WatchEvent.media_item_id)
    ):
        media_id = int(media_id)
        entry_id = canonical_by_item.get(media_id)
        if entry_id in impacted_entries and media_id in direct_ids:
            entry = stats[entry_id]
            entry.completion_count += int(count or 0)
            entry.first_played_at = _earliest(entry.first_played_at, first_played)
            entry.last_completed_at = _latest(entry.last_completed_at, last_played)
        if last_played is not None:
            activity_by_item[media_id] = last_played

    state_activity = _state_playback_time()
    for media_id, completed_count, completed_last, progress_ms, activity_last in session.execute(
        select(
            WatchState.media_item_id,
            func.max(case((WatchState.completed.is_(True), WatchState.view_count), else_=0)),
            func.max(
                case(
                    (WatchState.completed.is_(True), WatchState.last_watched_at),
                    else_=None,
                )
            ),
            func.max(case((WatchState.completed.is_(False), WatchState.progress_ms), else_=0)),
            func.max(state_activity),
        )
        .join(tree_ids_query, tree_ids_query.c.media_item_id == WatchState.media_item_id)
        .group_by(WatchState.media_item_id)
    ):
        media_id = int(media_id)
        entry_id = canonical_by_item.get(media_id)
        if entry_id in impacted_entries and media_id in direct_ids:
            entry = stats[entry_id]
            entry.completion_count = max(entry.completion_count, int(completed_count or 0))
            entry.last_completed_at = _latest(entry.last_completed_at, completed_last)
            entry.progress_ms = max(entry.progress_ms, int(progress_ms or 0))
        if activity_last is not None:
            activity_by_item[media_id] = (
                _latest(activity_by_item.get(media_id), activity_last) or activity_last
            )

    for media_id, played_at in activity_by_item.items():
        activity_ancestor_id: int | None = media_id
        activity_seen: set[int] = set()
        while activity_ancestor_id is not None and activity_ancestor_id not in activity_seen:
            activity_seen.add(activity_ancestor_id)
            entry_id = canonical_by_item.get(activity_ancestor_id)
            if entry_id in impacted_entries:
                stats[entry_id].last_played_at = _latest(stats[entry_id].last_played_at, played_at)
            activity_ancestor_id = parents.get(activity_ancestor_id)

    available_episode_ids = set(
        int(value)
        for value in session.scalars(
            select(SourceMediaRef.media_item_id)
            .join(
                tree_ids_query,
                tree_ids_query.c.media_item_id == SourceMediaRef.media_item_id,
            )
            .join(MediaItem, MediaItem.id == SourceMediaRef.media_item_id)
            .where(
                SourceMediaRef.available.is_(True),
                MediaItem.kind == MediaKind.EPISODE,
            )
            .distinct()
        )
    )
    episodes_by_show: dict[int, set[int]] = {}
    for episode_id in available_episode_ids:
        show_id = _ancestor_of_kind(episode_id, MediaKind.SHOW, item_by_id, parents)
        show_entry_id = canonical_by_item.get(show_id) if show_id is not None else None
        episode_entry_id = canonical_by_item.get(episode_id)
        if (
            show_entry_id is not None
            and show_entry_id in impacted_entries
            and episode_entry_id is not None
        ):
            episodes_by_show.setdefault(show_entry_id, set()).add(episode_entry_id)
    episode_entry_ids = {
        episode_id for values in episodes_by_show.values() for episode_id in values
    }
    stored_episode_counts = {
        int(entry_id): int(completion_count)
        for batch in _batches(episode_entry_ids, 900)
        for entry_id, completion_count in session.execute(
            select(CatalogEntry.media_item_id, CatalogEntry.completion_count).where(
                CatalogEntry.media_item_id.in_(batch)
            )
        )
    }
    for show_id, episode_ids in episodes_by_show.items():
        stats[show_id].child_count = len(episode_ids)
        stats[show_id].completed_child_count = sum(
            (
                stats[episode_id].completion_count
                if episode_id in stats
                else stored_episode_counts.get(episode_id, 0)
            )
            > 0
            for episode_id in episode_ids
        )

    entries = {
        entry.media_item_id: entry
        for entry in session.scalars(
            select(CatalogEntry).join(
                impacted_query,
                impacted_query.c.catalog_media_item_id == CatalogEntry.media_item_id,
            )
        )
    }
    now = datetime.now(UTC)
    for entry_id, updated in stats.items():
        model_entry = entries[entry_id]
        model_entry.completion_count = updated.completion_count
        model_entry.first_played_at = updated.first_played_at
        model_entry.last_completed_at = updated.last_completed_at
        model_entry.last_played_at = updated.last_played_at
        model_entry.progress_ms = updated.progress_ms
        if model_entry.kind == MediaKind.SHOW.value:
            model_entry.child_count = updated.child_count
            model_entry.completed_child_count = updated.completed_child_count
        model_entry.updated_at = now
    session.execute(delete(CatalogProjectionDirtyItem))
    _mark_clean(session, now)
    return ProjectionResult(len(impacted_entries), dirty_count, 0, "activity")


class _UnionFind:
    def __init__(self, values: set[int]) -> None:
        self._parents = {value: value for value in values}

    def find(self, value: int) -> int:
        parent = self._parents[value]
        while parent != self._parents[parent]:
            self._parents[parent] = self._parents[self._parents[parent]]
            parent = self._parents[parent]
        self._parents[value] = parent
        return parent

    def join(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self._parents[max(left_root, right_root)] = min(left_root, right_root)


def _bulk_insert(
    session: Session,
    model: type[CatalogEntry] | type[CatalogEntryMember],
    values: list[dict[str, Any]],
) -> None:
    for offset in range(0, len(values), 2000):
        session.execute(insert(model), values[offset : offset + 2000])


def _batches(values: set[int], size: int) -> Iterator[tuple[int, ...]]:
    ordered = tuple(values)
    for offset in range(0, len(ordered), size):
        yield ordered[offset : offset + size]


def _mark_clean(session: Session, now: datetime | None = None) -> None:
    updated_at = now or datetime.now(UTC)
    state = session.get(Setting, PROJECTION_STATE_KEY)
    if state is None:
        session.add(Setting(key=PROJECTION_STATE_KEY, value=_CLEAN_STATE, updated_at=updated_at))
    else:
        state.value = _CLEAN_STATE
        state.updated_at = updated_at


def _ancestor_of_kind(
    media_id: int,
    kind: MediaKind,
    item_by_id: dict[int, Any],
    parents: dict[int, int | None],
) -> int | None:
    current_id: int | None = media_id
    visited: set[int] = set()
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        row = item_by_id.get(current_id)
        if row is not None and row.kind is kind:
            return current_id
        current_id = parents.get(current_id)
    return None


def _normalized_title(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return " ".join(
        "".join(char for char in decomposed if not unicodedata.combining(char)).split()
    ).casefold()


def _fallback_identity(media_id: int, item_by_id: dict[int, Any]) -> str:
    row = item_by_id[media_id]
    title = _normalized_title(str(row.title))
    if row.kind in {MediaKind.EPISODE, MediaKind.ALBUM, MediaKind.TRACK}:
        return f"parent:{row.parent_id or 0}\0{title}"
    return title


def _safe_group(
    members: list[int],
    source_ids_by_item: dict[int, set[int]],
    available_sources: dict[int, set[int]] | None = None,
) -> bool:
    if len(members) < 2:
        return True
    seen: set[int] = set()
    for media_id in members:
        sources = source_ids_by_item.get(media_id, set())
        if not sources:
            return False
        if available_sources is not None:
            sources = available_sources.get(media_id, set())
        if seen & sources:
            return False
        seen.update(sources)
    return True


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _state_playback_time() -> Any:
    """Only real playback dates affect ordering; collection time never does."""
    completed_at = case((WatchState.completed.is_(True), WatchState.last_watched_at), else_=None)
    return case(
        (WatchState.last_played_at.is_(None), completed_at),
        (completed_at.is_(None), WatchState.last_played_at),
        (WatchState.last_played_at > completed_at, WatchState.last_played_at),
        else_=completed_at,
    )


def _latest(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return left if _as_utc(left) >= _as_utc(right) else right


def _earliest(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return left if _as_utc(left) <= _as_utc(right) else right
