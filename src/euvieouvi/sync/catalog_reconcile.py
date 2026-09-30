"""Conservative transactional reconciliation of duplicate catalog identities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import or_, select, text, tuple_
from sqlalchemy.orm import Session

from euvieouvi.connectors.embedded_identifiers import parse_embedded_identifiers
from euvieouvi.database.enums import ConnectorType, MediaKind
from euvieouvi.database.models import (
    MediaFilePath,
    MediaIdentifier,
    MediaItem,
    Setting,
    Source,
    SourceMediaRef,
)
from euvieouvi.sync.catalog_projection import PROJECTION_STATE_KEY

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
    embedded_items: int
    identifiers_recovered: int
    titles_normalized: int
    identifier_conflicts: int


def reconcile_catalog(
    session: Session, *, dry_run: bool = True, report: Callable[[str], None] | None = None
) -> ReconcileResult:
    """Merge only unambiguous cross-source identities in one transaction."""
    embedded, recovered, normalized, conflicts = _recover_embedded_identifiers(session, report)
    session.flush()
    physical_groups = _physical_groups(session)
    if report is not None:
        report(
            f"Conciliação por arquivo físico: {len(physical_groups)} grupo(s) "
            "confirmado(s) por caminho completo"
        )
    physical_ids = set(physical_groups)
    groups = physical_groups + tuple(
        ids for ids in _duplicate_groups(session) if ids not in physical_ids
    )
    merged = hierarchy = skipped = 0
    for group_index, ids in enumerate(groups):
        live = [session.get(MediaItem, media_id) for media_id in ids]
        items = [item for item in live if item is not None]
        if len(items) < 2:
            continue
        if not _safe_sources(session, items, physical=group_index < len(physical_groups)):
            skipped += 1
            if report is not None:
                report(f"Grupo ignorado por propriedade ambígua de fontes: IDs={ids}")
            continue
        canonical = _canonical(session, items)
        for duplicate in items:
            if duplicate.id == canonical.id:
                continue
            if report is not None:
                report(
                    f"Reconciliando {duplicate.kind.value} ID={duplicate.id} "
                    f"título={duplicate.title!r} → ID={canonical.id} título={canonical.title!r}"
                )
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
    if merged or recovered or normalized:
        _mark_projection_stale(session)
    result = ReconcileResult(
        groups_found=len(groups),
        items_merged=merged,
        hierarchy_merged=hierarchy,
        skipped=skipped,
        identifierless=int(identifierless),
        dry_run=dry_run,
        embedded_items=embedded,
        identifiers_recovered=recovered,
        titles_normalized=normalized,
        identifier_conflicts=conflicts,
    )
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
    if merged:
        _mark_projection_stale(session)
    session.commit()
    return merged


def reconcile_matching_items(
    session: Session,
    kind: MediaKind,
    identifiers: tuple[tuple[str, str], ...],
    *,
    preferred_media_id: int | None = None,
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
    canonical = next(
        (item for item in items if item.id == preferred_media_id),
        None,
    ) or _canonical(session, items)
    merged = 0
    for duplicate in items:
        if duplicate.id == canonical.id:
            continue
        merged += _merge_hierarchy(session, canonical, duplicate)
        _merge_item(session, canonical, duplicate)
        merged += 1
    if merged:
        _mark_projection_stale(session)
    return merged


def _recover_embedded_identifiers(
    session: Session, report: Callable[[str], None] | None = None
) -> tuple[int, int, int, int]:
    """Backfill strict provider tokens from old movie/show titles before grouping."""
    items = session.scalars(
        select(MediaItem).where(
            MediaItem.kind.in_([MediaKind.MOVIE, MediaKind.SHOW]),
            or_(
                MediaItem.title.ilike("%tmdb%"),
                MediaItem.title.ilike("%tvdb%"),
                MediaItem.title.ilike("%imdb%"),
            ),
        )
    ).all()
    embedded_items = recovered = normalized = conflicts = 0
    for item in items:
        metadata = parse_embedded_identifiers(item.title)
        if not metadata.identifiers:
            continue
        embedded_items += 1
        existing = {
            (provider, external_id)
            for provider, external_id in session.execute(
                select(MediaIdentifier.provider, MediaIdentifier.external_id).where(
                    MediaIdentifier.media_item_id == item.id,
                    MediaIdentifier.provider.in_(_STABLE_PROVIDERS),
                )
            )
        }
        missing = [
            value
            for value in metadata.identifiers
            if (value.provider, value.external_id) not in existing
        ]
        if missing and existing:
            conflicts += 1
            if report is not None:
                report(
                    f"Token ignorado: ID={item.id} título={item.title!r}; "
                    "IDs estruturados prevalecem"
                )
            continue
        if report is not None:
            parsed_ids = [(value.provider, value.external_id) for value in metadata.identifiers]
            report(
                f"Token oficial: ID={item.id} título={item.title!r}; identificadores={parsed_ids}"
            )
        if metadata.title != item.title:
            item.title = metadata.title
            normalized += 1
        for value in missing:
            session.add(
                MediaIdentifier(
                    media_item_id=item.id,
                    provider=value.provider,
                    external_id=value.external_id,
                )
            )
            recovered += 1
    return embedded_items, recovered, normalized, conflicts


def _mark_projection_stale(session: Session) -> None:
    state = session.get(Setting, PROJECTION_STATE_KEY)
    if state is None:
        session.add(Setting(key=PROJECTION_STATE_KEY, value="full"))
    else:
        state.value = "full"


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


def reconcile_physical_item(session: Session, media_item_id: int) -> int:
    """Repair only groups implicated by this item's exact files, without committing."""
    merged = 0
    for ids in _physical_groups(session, media_item_id=media_item_id):
        items = [item for value in ids if (item := session.get(MediaItem, value)) is not None]
        if len(items) < 2 or not _safe_sources(session, items, physical=True):
            continue
        canonical = _canonical(session, items)
        for duplicate in items:
            if duplicate.id != canonical.id:
                merged += _merge_hierarchy(session, canonical, duplicate)
                _merge_item(session, canonical, duplicate)
                merged += 1
    if merged:
        _mark_projection_stale(session)
    return merged


def _physical_groups(
    session: Session, *, media_item_id: int | None = None
) -> tuple[tuple[int, ...], ...]:
    # Show identity is inferred from shared episode files, not from the name
    # of a directory. Only compatible season/episode coordinates participate.
    filter_sql = (
        ""
        if media_item_id is None
        else """
        AND f.path_key IN (SELECT pf.path_key FROM media_file_paths pf
            JOIN source_media_refs pr ON pr.id=pf.reference_id WHERE pr.media_item_id=:media_id)
    """
    )
    rows = session.execute(
        text(
            """
        SELECT f.path_key, m.kind, m.season_number, m.episode_number,
               group_concat(DISTINCT CASE WHEN m.kind='episode'
                   THEN season.parent_id ELSE m.id END) AS ids
        FROM media_file_paths f
        JOIN source_media_refs r ON r.id=f.reference_id AND r.available=1
        JOIN sources src ON src.id=r.source_id AND src.enabled=1
        JOIN media_items m ON m.id=r.media_item_id
        LEFT JOIN media_items season ON season.id=m.parent_id AND season.kind='season'
        WHERE m.kind IN ('movie','episode','track')
        """
            + filter_sql
            + """
        GROUP BY f.path_key,m.kind,m.season_number,m.episode_number
        HAVING count(DISTINCT r.source_id)>1 AND count(DISTINCT
            CASE WHEN m.kind='episode' THEN season.parent_id ELSE m.id END)>1
        """
        ),
        {"media_id": media_item_id},
    ).all()
    # Do not join separate, contradictory file groups through transitive guesses.
    groups = {tuple(sorted(int(value) for value in str(row.ids).split(","))) for row in rows}
    return tuple(sorted(groups))


def _split_show_has_file_anchor(session: Session, items: list[MediaItem], source_id: int) -> bool:
    """Allow a server's split shows only when every file maps to one other show."""
    rows = (
        session.execute(
            text("""
        SELECT season.parent_id AS show_id, r.source_id,
               count(DISTINCT r.id) AS total,
               count(DISTINCT CASE WHEN f.id IS NOT NULL THEN r.id END) AS mapped,
               group_concat(DISTINCT f.path_key) AS paths
        FROM media_items episode JOIN media_items season ON season.id=episode.parent_id
        JOIN source_media_refs r ON r.media_item_id=episode.id AND r.available=1
        LEFT JOIN media_file_paths f ON f.reference_id=r.id
        WHERE episode.kind='episode' AND season.parent_id IN (
            SELECT id FROM media_items WHERE id IN (:first_id,:second_id))
        GROUP BY season.parent_id,r.source_id
    """),
            {"first_id": items[0].id, "second_id": items[1].id},
        ).all()
        if len(items) == 2
        else []
    )
    own = [row for row in rows if row.source_id == source_id]
    if len(own) != 2 or any(row.total != row.mapped or not row.paths for row in own):
        return False
    own_paths = set().union(*(set(str(row.paths).split(",")) for row in own))
    for anchor in rows:
        if anchor.source_id == source_id or not anchor.paths or anchor.total != anchor.mapped:
            continue
        # The independent source must map all these files to exactly one show.
        if sum(row.source_id == anchor.source_id for row in rows) != 1:
            continue
        if own_paths <= set(str(anchor.paths).split(",")):
            return True
    return False


def _safe_sources(session: Session, items: list[MediaItem], *, physical: bool = False) -> bool:
    manual_ids = {item.manual_match_tmdb_id for item in items if item.manual_match_tmdb_id}
    if len(manual_ids) > 1:
        return False
    if items and items[0].kind in {MediaKind.MOVIE, MediaKind.TRACK}:
        paths: dict[int, set[str]] = {}
        for media_id, key in session.execute(
            select(SourceMediaRef.media_item_id, MediaFilePath.path_key)
            .join(MediaFilePath, MediaFilePath.reference_id == SourceMediaRef.id)
            .where(
                SourceMediaRef.media_item_id.in_([item.id for item in items]),
                SourceMediaRef.available.is_(True),
            )
        ):
            paths.setdefault(media_id, set()).add(key)
        known = list(paths.values())
        if any(
            not left & right for index, left in enumerate(known) for right in known[index + 1 :]
        ):
            return False
    ownership: list[dict[int, bool]] = []
    for item in items:
        values: dict[int, bool] = {}
        for source_id, available in session.execute(
            select(SourceMediaRef.source_id, SourceMediaRef.available).where(
                SourceMediaRef.media_item_id == item.id
            )
        ):
            source_id = int(source_id)
            values[source_id] = values.get(source_id, False) or bool(available)
        ownership.append(values)
    if not any(ownership):
        return False
    source_ids = {source_id for values in ownership for source_id in values}
    for source_id in source_ids:
        # Two live rows from the same server can be intentionally distinct
        # editions. Identifier-only remnants and unavailable predecessor rows
        # are safe to fold into the one live owner.
        if sum(bool(values.get(source_id)) for values in ownership) > 1:
            if (
                physical
                and items[0].kind is MediaKind.SHOW
                and _split_show_has_file_anchor(session, items, source_id)
            ):
                continue
            return False
    return True


def _canonical(session: Session, items: list[MediaItem]) -> MediaItem:
    plex_refs = session.execute(
        select(SourceMediaRef.media_item_id, SourceMediaRef.available)
        .join(Source, Source.id == SourceMediaRef.source_id)
        .where(
            SourceMediaRef.media_item_id.in_([item.id for item in items]),
            Source.connector_type == ConnectorType.PLEX,
        )
    )
    plex_ids: set[int] = set()
    active_plex_ids: set[int] = set()
    for media_item_id, available in plex_refs:
        plex_ids.add(int(media_item_id))
        if available:
            active_plex_ids.add(int(media_item_id))
    return min(
        items,
        key=lambda item: (
            not bool(item.manual_match_tmdb_id),
            item.id not in active_plex_ids,
            item.id not in plex_ids,
            item.id,
        ),
    )


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
        if not candidates:
            candidate_ids = session.scalars(
                text("""
                SELECT DISTINCT wr.media_item_id FROM media_file_paths wf
                JOIN source_media_refs wr ON wr.id=wf.reference_id AND wr.available=1
                JOIN media_items w ON w.id=wr.media_item_id AND w.parent_id=:parent_id
                JOIN media_file_paths lf ON lf.path_key=wf.path_key
                JOIN source_media_refs lr ON lr.id=lf.reference_id AND lr.available=1
                WHERE lr.media_item_id=:loser_id AND lr.source_id!=wr.source_id
                  AND w.season_number=:season AND w.episode_number=:episode
            """),
                {
                    "parent_id": winner.id,
                    "loser_id": episode.id,
                    "season": episode.season_number,
                    "episode": episode.episode_number,
                },
            ).all()
            candidates = [
                item for value in candidate_ids if (item := session.get(MediaItem, value))
            ]
        if not candidates and episode.episode_number is not None:
            coordinate_candidates = session.scalars(
                select(MediaItem).where(
                    MediaItem.parent_id == winner.id,
                    MediaItem.kind == MediaKind.EPISODE,
                    MediaItem.episode_number == episode.episode_number,
                )
            ).all()
            if len(coordinate_candidates) == 1:
                candidate = coordinate_candidates[0]
                identities = {
                    media_id: {
                        (provider, external_id)
                        for provider, external_id in session.execute(
                            select(MediaIdentifier.provider, MediaIdentifier.external_id).where(
                                MediaIdentifier.media_item_id == media_id,
                                MediaIdentifier.provider.in_(_STABLE_PROVIDERS),
                            )
                        )
                    }
                    for media_id in (candidate.id, episode.id)
                }
                if not identities[candidate.id] or not identities[episode.id]:
                    candidates = [candidate]
        if len(candidates) == 1 and _safe_sources(session, [candidates[0], episode]):
            _merge_item(session, candidates[0], episode)
            merged += 1
        else:
            episode.parent_id = winner.id
    return merged


def _merge_item(session: Session, winner: MediaItem, loser: MediaItem) -> None:
    _fill_missing(winner, loser)
    session.execute(
        text("""
        DELETE FROM media_identifiers WHERE media_item_id=:loser
          AND provider IN ('tmdb','tvdb','imdb','musicbrainz')
          AND EXISTS (SELECT 1 FROM media_identifiers w WHERE w.media_item_id=:winner
            AND w.provider=media_identifiers.provider
            AND w.external_id!=media_identifiers.external_id)
    """),
        {"loser": loser.id, "winner": winner.id},
    )
    if winner.manual_match_tmdb_id:
        # Old metadata cannot reintroduce identifiers the user explicitly fixed.
        session.execute(
            text("DELETE FROM media_identifiers WHERE media_item_id=:loser"), {"loser": loser.id}
        )
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
              last_watched_at=coalesce(
                max(w.last_watched_at,l.last_watched_at),w.last_watched_at,l.last_watched_at
              ),
              last_played_at=coalesce(
                max(w.last_played_at,l.last_played_at),w.last_played_at,l.last_played_at
              ),
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
