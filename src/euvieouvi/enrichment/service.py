"""Apply optional external metadata without replacing Plex facts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version

from flask import Flask
from sqlalchemy import and_, case, exists, func, or_, select, update

from euvieouvi.database.enums import MediaKind
from euvieouvi.database.models import (
    EnrichmentRecord,
    Genre,
    MediaGenre,
    MediaIdentifier,
    MediaImage,
    MediaItem,
    Setting,
)
from euvieouvi.enrichment.providers import (
    EnrichedMetadata,
    EnrichmentError,
    EnrichmentNotFoundError,
    MusicBrainzClient,
    TmdbClient,
)
from euvieouvi.extensions import db


def enrich_catalog(
    app: Flask,
    *,
    limit: int | None = None,
    progress: Callable[[dict[str, int]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Enrich all eligible exact identifiers, or a bounded batch when requested."""
    settings = {item.key: item.value for item in db.session.scalars(select(Setting))}
    language = settings.get("metadata.language", "pt-BR")
    tmdb = None
    musicbrainz = None
    if settings.get("metadata.tmdb.enabled") == "true" and settings.get("metadata.tmdb.token"):
        tmdb = TmdbClient(settings["metadata.tmdb.token"])
    if settings.get("metadata.musicbrainz.enabled") == "true":
        musicbrainz = MusicBrainzClient(f"euvieouvi/{version('euvieouvi')}")
    counters = {"processed": 0, "updated": 0, "failed": 0}
    db.session.execute(
        update(EnrichmentRecord)
        .where(
            EnrichmentRecord.status == "failed",
            EnrichmentRecord.message.in_(
                ("MusicBrainz recording was not found", "TMDB item was not found")
            ),
        )
        .values(status="not_found")
    )
    try:
        # Fetch only IDs to avoid DetachedInstanceError after commit
        # (holding ORM objects across commit expires them)
        missing_metadata = or_(
            MediaItem.summary.is_(None),
            MediaItem.tagline.is_(None),
            MediaItem.studio.is_(None),
            MediaItem.audience_rating.is_(None),
        )
        has_poster = exists(
            select(MediaImage.id).where(
                MediaImage.media_item_id == MediaItem.id,
                MediaImage.image_type == "poster",
            )
        )
        eligible_candidates = []
        if tmdb is not None:
            eligible_candidates.append(
                and_(
                    MediaIdentifier.provider == "tmdb",
                    MediaItem.kind.in_([MediaKind.MOVIE, MediaKind.SHOW]),
                    or_(missing_metadata, ~has_poster),
                )
            )
        if musicbrainz is not None:
            eligible_candidates.append(
                and_(
                    MediaIdentifier.provider == "mbid",
                    MediaItem.kind == MediaKind.TRACK,
                    ~has_poster,
                )
            )
        if not eligible_candidates:
            if progress is not None:
                progress({**counters, "total": 0, "percent": 100})
            _save_summary(counters)
            db.session.commit()
            return counters

        candidate_query = (
            select(MediaIdentifier.id)
            .join(MediaItem, MediaItem.id == MediaIdentifier.media_item_id)
            .outerjoin(
                EnrichmentRecord,
                and_(
                    EnrichmentRecord.media_item_id == MediaIdentifier.media_item_id,
                    EnrichmentRecord.provider == MediaIdentifier.provider,
                ),
            )
            .where(
                or_(*eligible_candidates),
                or_(
                    EnrichmentRecord.id.is_(None),
                    EnrichmentRecord.status.not_in(("succeeded", "not_found")),
                ),
            )
            .order_by(
                case((EnrichmentRecord.id.is_(None), 0), else_=1),
                EnrichmentRecord.checked_at,
                MediaItem.id,
                MediaIdentifier.id,
            )
        )
        if limit is not None:
            candidate_query = candidate_query.limit(limit)
        total = (
            db.session.scalar(
                select(func.count()).select_from(candidate_query.order_by(None).subquery())
            )
            or 0
        )
        if progress is not None:
            progress({**counters, "total": total, "percent": 100 if total == 0 else 0})
        candidate_ids: list[int] = list(db.session.scalars(candidate_query).all())
        for identifier_id in candidate_ids:
            if cancelled is not None and cancelled():
                break
            if limit is not None and counters["processed"] >= limit:
                break
            identifier = db.session.get(MediaIdentifier, identifier_id)
            if identifier is None:
                continue
            item = db.session.get(MediaItem, identifier.media_item_id)
            if item is None:
                continue
            provider = identifier.provider
            if (
                provider == "tmdb"
                and tmdb is not None
                and item.kind
                in {
                    MediaKind.MOVIE,
                    MediaKind.SHOW,
                }
            ):
                lookup_kind = "tmdb"
            elif provider == "mbid" and musicbrainz is not None and item.kind is MediaKind.TRACK:
                lookup_kind = "musicbrainz"
            else:
                continue
            record = db.session.scalar(
                select(EnrichmentRecord).where(
                    EnrichmentRecord.media_item_id == item.id,
                    EnrichmentRecord.provider == provider,
                )
            )
            if record is not None and record.status == "succeeded":
                continue
            if record is None:
                record = EnrichmentRecord(
                    media_item_id=item.id,
                    provider=provider,
                    status="pending",
                    attempts=0,
                )
                db.session.add(record)
            counters["processed"] += 1
            record.attempts += 1
            record.checked_at = datetime.now(UTC)
            try:
                if lookup_kind == "tmdb":
                    assert tmdb is not None
                    media_type = "movie" if item.kind is MediaKind.MOVIE else "tv"
                    metadata = tmdb.lookup(media_type, identifier.external_id, language=language)
                    if language == "pt-BR" and _needs_language_fallback(metadata):
                        try:
                            fallback = tmdb.lookup(
                                media_type, identifier.external_id, language="en-US"
                            )
                        except EnrichmentError:
                            pass
                        else:
                            metadata = _merge_metadata(metadata, fallback)
                else:
                    assert musicbrainz is not None
                    metadata = musicbrainz.lookup_recording(identifier.external_id)
                changed = _apply(item, metadata)
                record.status = "succeeded"
                record.message = None
                counters["updated"] += int(changed)
            except EnrichmentNotFoundError as error:
                record.status = "not_found"
                record.message = str(error)[:500]
                counters["failed"] += 1
            except (EnrichmentError, ValueError) as error:
                record.status = "failed"
                record.message = str(error)[:500]
                counters["failed"] += 1
            db.session.commit()
            if progress is not None:
                progress(
                    {
                        **counters,
                        "total": total,
                        "percent": min(100, counters["processed"] * 100 // total),
                    }
                )
    finally:
        if tmdb is not None:
            tmdb.close()
        if musicbrainz is not None:
            musicbrainz.close()
    _save_summary(counters)
    db.session.commit()
    app.logger.info("metadata enrichment finished", extra=counters)
    return counters


def _needs_language_fallback(metadata: EnrichedMetadata) -> bool:
    return metadata.summary is None or metadata.tagline is None


def _merge_metadata(primary: EnrichedMetadata, fallback: EnrichedMetadata) -> EnrichedMetadata:
    return EnrichedMetadata(
        summary=primary.summary or fallback.summary,
        tagline=primary.tagline or fallback.tagline,
        studio=primary.studio or fallback.studio,
        audience_rating=(
            primary.audience_rating
            if primary.audience_rating is not None
            else fallback.audience_rating
        ),
        genres=primary.genres or fallback.genres,
        poster_url=primary.poster_url or fallback.poster_url,
        poster_provider=primary.poster_provider or fallback.poster_provider,
    )


def _apply(item: MediaItem, metadata: EnrichedMetadata) -> bool:
    changed = False
    for field in ("summary", "tagline", "studio", "audience_rating"):
        if getattr(item, field) is None and (value := getattr(metadata, field)) is not None:
            setattr(item, field, value)
            changed = True
    for name in metadata.genres:
        normalized = " ".join(name.split()).casefold()
        genre = db.session.scalar(select(Genre).where(Genre.normalized_name == normalized))
        if genre is None:
            genre = Genre(name=" ".join(name.split()), normalized_name=normalized)
            db.session.add(genre)
            db.session.flush()
        link = db.session.scalar(
            select(MediaGenre).where(
                MediaGenre.media_item_id == item.id,
                MediaGenre.genre_id == genre.id,
            )
        )
        if link is None:
            db.session.add(MediaGenre(media_item_id=item.id, genre_id=genre.id))
            changed = True
    if metadata.poster_url and metadata.poster_provider:
        image = db.session.scalar(
            select(MediaImage).where(
                MediaImage.media_item_id == item.id,
                MediaImage.image_type == "poster",
            )
        )
        if image is None:
            db.session.add(
                MediaImage(
                    media_item_id=item.id,
                    source_id=None,
                    image_type="poster",
                    provider=metadata.poster_provider,
                    source_url=metadata.poster_url,
                    cache_status="pending",
                )
            )
            changed = True
    return changed


def _save_summary(counters: dict[str, int]) -> None:
    value = ",".join(f"{key}={number}" for key, number in counters.items())
    setting = db.session.get(Setting, "metadata.last_summary")
    if setting is None:
        db.session.add(Setting(key="metadata.last_summary", value=value))
    else:
        setting.value = value
