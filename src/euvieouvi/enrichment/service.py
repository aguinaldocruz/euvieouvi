"""Apply optional external metadata without replacing Plex facts."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import version

from flask import Flask
from sqlalchemy import select

from euvieouvi.database.enums import MediaKind
from euvieouvi.database.models import (
    EnrichmentRecord,
    Genre,
    MediaGenre,
    MediaIdentifier,
    MediaItem,
    Setting,
)
from euvieouvi.enrichment.providers import (
    EnrichedMetadata,
    EnrichmentError,
    MusicBrainzClient,
    TmdbClient,
)
from euvieouvi.extensions import db


def enrich_catalog(app: Flask, *, limit: int = 100) -> dict[str, int]:
    """Enrich a bounded batch selected only by exact external identifiers."""
    settings = {item.key: item.value for item in db.session.scalars(select(Setting))}
    language = settings.get("metadata.language", "pt-BR")
    tmdb = None
    musicbrainz = None
    if settings.get("metadata.tmdb.enabled") == "true" and settings.get("metadata.tmdb.token"):
        tmdb = TmdbClient(settings["metadata.tmdb.token"])
    if settings.get("metadata.musicbrainz.enabled") == "true":
        musicbrainz = MusicBrainzClient(f"euvieouvi/{version('euvieouvi')}")
    counters = {"processed": 0, "updated": 0, "failed": 0}
    try:
        candidates = db.session.execute(
            select(MediaItem, MediaIdentifier)
            .join(MediaIdentifier, MediaIdentifier.media_item_id == MediaItem.id)
            .where(MediaIdentifier.provider.in_(["tmdb", "mbid"]))
            .order_by(MediaItem.id)
            .limit(limit * 3)
        ).all()
        for item, identifier in candidates:
            if counters["processed"] >= limit:
                break
            provider = identifier.provider
            if provider == "tmdb" and tmdb is not None and item.kind in {
                MediaKind.MOVIE,
                MediaKind.SHOW,
            }:
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
                    metadata = tmdb.lookup(
                        "movie" if item.kind is MediaKind.MOVIE else "tv",
                        identifier.external_id,
                        language=language,
                    )
                else:
                    assert musicbrainz is not None
                    metadata = musicbrainz.lookup_recording(identifier.external_id)
                changed = _apply(item, metadata)
                record.status = "succeeded"
                record.message = None
                counters["updated"] += int(changed)
            except (EnrichmentError, ValueError) as error:
                record.status = "failed"
                record.message = str(error)[:500]
                counters["failed"] += 1
            db.session.commit()
    finally:
        if tmdb is not None:
            tmdb.close()
        if musicbrainz is not None:
            musicbrainz.close()
    _save_summary(counters)
    db.session.commit()
    app.logger.info("metadata enrichment finished", extra=counters)
    return counters


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
    return changed


def _save_summary(counters: dict[str, int]) -> None:
    value = ",".join(f"{key}={number}" for key, number in counters.items())
    setting = db.session.get(Setting, "metadata.last_summary")
    if setting is None:
        db.session.add(Setting(key="metadata.last_summary", value=value))
    else:
        setting.value = value
