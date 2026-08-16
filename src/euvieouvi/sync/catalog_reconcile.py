"""Conservative transactional reconciliation of duplicate catalog identities."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text, tuple_
from sqlalchemy.orm import Session

from euvieouvi.database.enums import ConnectorType, MediaKind
from euvieouvi.database.models import MediaIdentifier, MediaItem, Source, SourceMediaRef

_STABLE_PROVIDERS = ("tmdb", "tvdb", "imdb", "musicbrainz")
_KINDS = (MediaKind.SHOW, MediaKind.MOVIE, MediaKind.ARTIST, MediaKind.ALBUM, MediaKind.TRACK)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    groups_found: int
    items_merged: int
    hierarchy_merged: int
    skipped: int
    identifierless: int
    dry_run: bool


def reconcile_catalog(session: Session, *, dry_run: bool = True) -> ReconcileResult:
    """Merge only unambiguous cross-source identities in one transaction."""
    groups = _duplicate_groups(session)
    merged = hierarchy = skipped = 0
    for ids in groups:
        live = [session.get(MediaItem, media_id) for media_id in ids]
        items = [item for item in live if item is not None]
        if len(items) < 2 or not _safe_sources(session, items):
            skipped += 1
            continue
        canonical = _canonical(session, items)
        for duplicate in items:
            if duplicate.id == canonical.id:
                continue
            hierarchy += _merge_hierarchy(session, canonical, duplicate)
            _merge_item(session, canonical, duplicate)
            merged += 1
    identifierless = (
        session.scalar(
            text(
                """
            SELECT count(*) FROM media_items m
            WHERE m.kind IN ('show','movie','artist','album','track')
              AND EXISTS (SELECT 1 FROM source_media_refs r WHERE r.media_item_id=m.id)
              AND NOT EXISTS (
                SELECT 1 FROM media_identifiers i
                WHERE i.media_item_id=m.id
                  AND i.provider IN ('tmdb','tvdb','imdb','musicbrainz')
              )
            """
            )
        )
        or 0
    )
    result = ReconcileResult(len(groups), merged, hierarchy, skipped, int(identifierless), dry_run)
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return result


def merge_confirmed_items(session: Session, media_ids: tuple[int, ...]) -> int:
    """Merge a human-reviewed same-kind group, retaining all safety constraints."""
    items = [item for item in (session.get(MediaItem, value) for value in media_ids) if item]
    if len(items) != len(media_ids) or len(items) < 2:
        raise ValueError("Confirmed merge items are missing.")
    if len({item.kind for item in items}) != 1:
        raise ValueError("Confirmed merge items must have the same media kind.")
    if not _safe_sources(session, items):
        raise ValueError("Confirmed merge items have overlapping or missing source ownership.")
    canonical = _canonical(session, items)
    merged = 0
    for duplicate in items:
        if duplicate.id == canonical.id:
            continue
        merged += _merge_hierarchy(session, canonical, duplicate)
        _merge_item(session, canonical, duplicate)
        merged += 1
    session.commit()
    return merged


def reconcile_matching_items(
    session: Session,
    kind: MediaKind,
    identifiers: tuple[tuple[str, str], ...],
) -> int:
    """Atomically merge one safe duplicate group implicated by incoming media."""
    stable = tuple(
        (provider, external_id)
        for provider, external_id in identifiers
        if provider in _STABLE_PROVIDERS and external_id
    )
    if not stable:
        return 0
    ids = set(
        session.scalars(
            select(MediaIdentifier.media_item_id)
            .join(MediaItem, MediaItem.id == MediaIdentifier.media_item_id)
            .where(
                MediaItem.kind == kind,
                tuple_(MediaIdentifier.provider, MediaIdentifier.external_id).in_(stable),
            )
        )
    )
    items = [item for item in (session.get(MediaItem, value) for value in ids) if item]
    if len(items) < 2 or not _safe_sources(session, items):
        return 0
    canonical = _canonical(session, items)
    merged = 0
    for duplicate in items:
        if duplicate.id == canonical.id:
            continue
        merged += _merge_hierarchy(session, canonical, duplicate)
        _merge_item(session, canonical, duplicate)
        merged += 1
    return merged


def _duplicate_groups(session: Session) -> tuple[tuple[int, ...], ...]:
    rows = session.execute(
        text(
            """
            SELECT i.provider, i.external_id, m.kind, group_concat(DISTINCT m.id) AS ids
            FROM media_identifiers i
            JOIN media_items m ON m.id=i.media_item_id
            WHERE i.provider IN ('tmdb','tvdb','imdb','musicbrainz')
              AND m.kind IN ('show','movie','artist','album','track')
            GROUP BY i.provider,i.external_id,m.kind
            HAVING count(DISTINCT m.id)>1
            """
        )
    ).all()
    parents: dict[int, int] = {}

    def find(value: int) -> int:
        parents.setdefault(value, value)
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    for row in rows:
        ids = [int(value) for value in str(row.ids).split(",")]
        root = find(ids[0])
        for media_id in ids[1:]:
            other = find(media_id)
            if root != other:
                parents[other] = root
    groups: dict[int, set[int]] = {}
    for media_id in parents:
        groups.setdefault(find(media_id), set()).add(media_id)
    return tuple(tuple(sorted(group)) for group in groups.values() if len(group) > 1)


def _safe_sources(session: Session, items: list[MediaItem]) -> bool:
    source_sets = [
        set(
            session.scalars(
                select(SourceMediaRef.source_id).where(SourceMediaRef.media_item_id == item.id)
            )
        )
        for item in items
    ]
    if any(not values for values in source_sets):
        return False
    seen: set[int] = set()
    for values in source_sets:
        if seen & values:
            return False
        seen.update(values)
    return True


def _canonical(session: Session, items: list[MediaItem]) -> MediaItem:
    plex_ids = set(
        session.scalars(
            select(SourceMediaRef.media_item_id)
            .join(Source, Source.id == SourceMediaRef.source_id)
            .where(
                SourceMediaRef.media_item_id.in_([item.id for item in items]),
                Source.connector_type == ConnectorType.PLEX,
            )
        )
    )
    return min(items, key=lambda item: (item.id not in plex_ids, item.id))


def _merge_hierarchy(session: Session, winner: MediaItem, loser: MediaItem) -> int:
    if winner.kind is not MediaKind.SHOW:
        return 0
    merged = 0
    loser_seasons = session.scalars(
        select(MediaItem).where(MediaItem.parent_id == loser.id, MediaItem.kind == MediaKind.SEASON)
    ).all()
    for season in loser_seasons:
        candidates = session.scalars(
            select(MediaItem).where(
                MediaItem.parent_id == winner.id,
                MediaItem.kind == MediaKind.SEASON,
                MediaItem.season_number == season.season_number,
            )
        ).all()
        if len(candidates) == 1:
            merged += _merge_season(session, candidates[0], season)
            _merge_item(session, candidates[0], season)
            merged += 1
        elif not candidates:
            season.parent_id = winner.id
    return merged


def _merge_season(session: Session, winner: MediaItem, loser: MediaItem) -> int:
    merged = 0
    loser_episodes = session.scalars(
        select(MediaItem).where(
            MediaItem.parent_id == loser.id, MediaItem.kind == MediaKind.EPISODE
        )
    ).all()
    for episode in loser_episodes:
        candidate_ids = session.scalars(
            text(
                """
                SELECT DISTINCT w.id FROM media_items w
                JOIN media_identifiers wi ON wi.media_item_id=w.id
                JOIN media_identifiers li
                  ON li.provider=wi.provider AND li.external_id=wi.external_id
                WHERE w.parent_id=:parent_id AND li.media_item_id=:loser_id
                """
            ),
            {"parent_id": winner.id, "loser_id": episode.id},
        ).all()
        candidates = [
            item for item in (session.get(MediaItem, value) for value in candidate_ids) if item
        ]
        if len(candidates) == 1 and _safe_sources(session, [candidates[0], episode]):
            _merge_item(session, candidates[0], episode)
            merged += 1
        else:
            episode.parent_id = winner.id
    return merged


def _merge_item(session: Session, winner: MediaItem, loser: MediaItem) -> None:
    _fill_missing(winner, loser)
    for table, conflict in (
        ("media_genres", "genre_id"),
        ("media_identifiers", "provider,external_id"),
        ("media_images", "image_type"),
        ("enrichment_records", "provider"),
    ):
        session.execute(
            text(
                f"DELETE FROM {table} WHERE media_item_id=:loser AND EXISTS ("
                f"SELECT 1 FROM {table} w WHERE w.media_item_id=:winner AND "
                + " AND ".join(f"w.{column}= {table}.{column}" for column in conflict.split(","))
                + ")"
            ),
            {"winner": winner.id, "loser": loser.id},
        )
        session.execute(
            text(f"UPDATE {table} SET media_item_id=:winner WHERE media_item_id=:loser"),
            {"winner": winner.id, "loser": loser.id},
        )
    _merge_watch_states(session, winner.id, loser.id)
    for table in ("watch_events", "source_media_refs"):
        session.execute(
            text(f"UPDATE {table} SET media_item_id=:winner WHERE media_item_id=:loser"),
            {"winner": winner.id, "loser": loser.id},
        )
    session.execute(
        text("UPDATE media_items SET parent_id=:winner WHERE parent_id=:loser"),
        {"winner": winner.id, "loser": loser.id},
    )
    session.delete(loser)
    session.flush()


def _merge_watch_states(session: Session, winner: int, loser: int) -> None:
    session.execute(
        text(
            """
            UPDATE watch_states AS w SET
              view_count=max(w.view_count,l.view_count),
              completed=(w.completed OR l.completed),
              last_watched_at=max(w.last_watched_at,l.last_watched_at),
              observed_at=max(w.observed_at,l.observed_at)
            FROM watch_states AS l
            WHERE w.media_item_id=:winner AND l.media_item_id=:loser
              AND w.source_id=l.source_id
            """
        ),
        {"winner": winner, "loser": loser},
    )
    session.execute(
        text(
            "DELETE FROM watch_states WHERE media_item_id=:loser AND source_id IN "
            "(SELECT source_id FROM watch_states WHERE media_item_id=:winner)"
        ),
        {"winner": winner, "loser": loser},
    )
    session.execute(
        text("UPDATE watch_states SET media_item_id=:winner WHERE media_item_id=:loser"),
        {"winner": winner, "loser": loser},
    )


def _fill_missing(winner: MediaItem, loser: MediaItem) -> None:
    for field in (
        "original_title",
        "sort_title",
        "year",
        "duration_ms",
        "originally_available_on",
        "summary",
        "tagline",
        "studio",
        "content_rating",
        "audience_rating",
        "source_added_at",
    ):
        if getattr(winner, field) is None:
            setattr(winner, field, getattr(loser, field))
