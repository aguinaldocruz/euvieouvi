"""Optional enrichment persistence and Plex precedence."""

from flask import Flask
from sqlalchemy import select

from euvieouvi.database.enums import MediaKind
from euvieouvi.database.models import (
    EnrichmentRecord,
    Genre,
    MediaIdentifier,
    MediaItem,
    Setting,
)
from euvieouvi.enrichment import service
from euvieouvi.enrichment.providers import EnrichedMetadata, EnrichmentError
from euvieouvi.extensions import db


class FakeTmdb:
    def __init__(self, token: str) -> None:
        assert token == "tmdb-token"

    def lookup(self, media_type: str, external_id: str, *, language: str) -> EnrichedMetadata:
        assert (media_type, external_id, language) == ("movie", "329865", "pt-BR")
        return EnrichedMetadata(
            summary="External summary must not replace Plex.",
            tagline="Por que eles estão aqui?",
            studio="External studio",
            audience_rating=8.2,
            genres=("Science Fiction",),
        )

    def close(self) -> None:
        pass


class FakeMusicBrainz:
    def __init__(self, user_agent: str) -> None:
        assert user_agent.startswith("euvieouvi/")

    def lookup_recording(self, mbid: str) -> EnrichedMetadata:
        if mbid.endswith("bad"):
            raise EnrichmentError("fixture failure")
        return EnrichedMetadata(genres=("Rock",))

    def close(self) -> None:
        pass


def test_enrichment_only_fills_missing_fields_and_is_idempotent(
    app: Flask, monkeypatch: object
) -> None:
    monkeypatch.setattr(service, "TmdbClient", FakeTmdb)  # type: ignore[attr-defined]
    with app.app_context():
        movie = MediaItem(kind=MediaKind.MOVIE, title="Arrival", summary="Plex summary")
        db.session.add(movie)
        db.session.flush()
        db.session.add_all(
            [
                MediaIdentifier(media_item_id=movie.id, provider="tmdb", external_id="329865"),
                Setting(key="metadata.tmdb.enabled", value="true"),
                Setting(key="metadata.tmdb.token", value="tmdb-token"),
                Setting(key="metadata.language", value="pt-BR"),
            ]
        )
        db.session.commit()

        first = service.enrich_catalog(app)
        second = service.enrich_catalog(app)
        db.session.refresh(movie)
        assert first == {"processed": 1, "updated": 1, "failed": 0}
        assert second == {"processed": 0, "updated": 0, "failed": 0}
        assert movie.summary == "Plex summary"
        assert movie.tagline == "Por que eles estão aqui?"
        assert movie.studio == "External studio"
        assert movie.audience_rating == 8.2
        assert db.session.scalar(select(Genre.name)) == "Science Fiction"
        record = db.session.scalar(select(EnrichmentRecord))
        assert record is not None and record.status == "succeeded" and record.attempts == 1


def test_musicbrainz_enrichment_and_failure_are_audited(
    app: Flask, monkeypatch: object
) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        service, "MusicBrainzClient", FakeMusicBrainz
    )
    with app.app_context():
        good = MediaItem(kind=MediaKind.TRACK, title="Good")
        bad = MediaItem(kind=MediaKind.TRACK, title="Bad")
        db.session.add_all([good, bad])
        db.session.flush()
        db.session.add_all(
            [
                MediaIdentifier(
                    media_item_id=good.id,
                    provider="mbid",
                    external_id="b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d",
                ),
                MediaIdentifier(media_item_id=bad.id, provider="mbid", external_id="fixture-bad"),
                Setting(key="metadata.musicbrainz.enabled", value="true"),
            ]
        )
        db.session.commit()
        result = service.enrich_catalog(app)
        assert result == {"processed": 2, "updated": 1, "failed": 1}
        records = db.session.scalars(select(EnrichmentRecord).order_by(EnrichmentRecord.id)).all()
        assert [record.status for record in records] == ["succeeded", "failed"]
        assert records[1].message == "fixture failure"
