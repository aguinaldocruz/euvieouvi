"""User-confirmed identity corrections without changing playback history."""

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

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
from euvieouvi.enrichment.providers import TmdbMatch
from euvieouvi.sync.catalog_projection import PROJECTION_STATE_KEY
from euvieouvi.sync.catalog_reconcile import reconcile_matching_items


def apply_manual_match(session: Session, item: MediaItem, match: TmdbMatch) -> int:
    if item.kind not in {MediaKind.MOVIE, MediaKind.SHOW}:
        raise ValueError("Manual TMDB matching supports movies and shows only")
    audit_key = f"identity.manual_match.{item.id}"
    audit = session.get(Setting, audit_key)
    previous = {
        "title": item.title,
        "identifiers": [
            (provider, external_id)
            for provider, external_id in session.execute(
                select(MediaIdentifier.provider, MediaIdentifier.external_id).where(
                    MediaIdentifier.media_item_id == item.id
                )
            )
        ],
        "tmdb_id": match.external_id,
        "at": datetime.now(UTC).isoformat(),
    }
    if audit is None:
        session.add(Setting(key=audit_key, value=json.dumps([previous])))
    else:
        audit.value = json.dumps([*json.loads(audit.value), previous][-10:])
    item.manual_match_tmdb_id = match.external_id
    item.title, item.original_title, item.year = match.title, match.original_title, match.year
    item.sort_title = None
    item.summary, item.tagline = match.metadata.summary, match.metadata.tagline
    item.studio, item.audience_rating = match.metadata.studio, match.metadata.audience_rating
    session.execute(delete(MediaIdentifier).where(MediaIdentifier.media_item_id == item.id))
    for provider, external_id in match.identifiers:
        session.add(
            MediaIdentifier(media_item_id=item.id, provider=provider, external_id=external_id)
        )
    session.execute(delete(EnrichmentRecord).where(EnrichmentRecord.media_item_id == item.id))
    session.execute(delete(MediaGenre).where(MediaGenre.media_item_id == item.id))
    for name in match.metadata.genres:
        normalized = name.strip().casefold()
        genre = session.scalar(select(Genre).where(Genre.normalized_name == normalized))
        if genre is None:
            genre = Genre(name=name.strip(), normalized_name=normalized)
            session.add(genre)
            session.flush()
        session.add(MediaGenre(media_item_id=item.id, genre_id=genre.id))
    for image_type, url in (
        ("poster", match.metadata.poster_url),
        ("backdrop", match.backdrop_url),
    ):
        image = session.scalar(
            select(MediaImage).where(
                MediaImage.media_item_id == item.id, MediaImage.image_type == image_type
            )
        )
        if image is not None:
            session.delete(image)
            session.flush()
        if url:
            session.add(
                MediaImage(
                    media_item_id=item.id,
                    image_type=image_type,
                    provider="tmdb",
                    source_url=url,
                    cache_status="pending",
                )
            )
    projection = session.get(Setting, PROJECTION_STATE_KEY)
    if projection is None:
        session.add(Setting(key=PROJECTION_STATE_KEY, value="full"))
    else:
        projection.value = "full"
    session.flush()
    merged = reconcile_matching_items(
        session, item.kind, match.identifiers, preferred_media_id=item.id
    )
    logging.getLogger("euvieouvi").info(
        "Manual match reconciliation media=%s title=%r tmdb=%s merged_items=%s",
        item.id,
        item.title,
        match.external_id,
        merged,
    )
    return item.id
