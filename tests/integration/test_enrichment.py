"""Optional enrichment persistence and Plex precedence."""

from flask import Flask
from sqlalchemy import select

from euvieouvi.database.enums import MediaKind
from euvieouvi.database.models import (
    EnrichmentRecord,
    Genre,
    MediaIdentifier,
    MediaImage,
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
            poster_url="https://image.tmdb.org/t/p/w500/arrival.jpg",
            poster_provider="tmdb",
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
        image = db.session.scalar(select(MediaImage))
        assert image is not None and image.provider == "tmdb" and image.source_id is None


def test_musicbrainz_enrichment_and_failure_are_audited(app: Flask, monkeypatch: object) -> None:
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


def test_succeeded_records_do_not_starve_later_enrichment_candidates(
    app: Flask, monkeypatch: object
) -> None:
    monkeypatch.setattr(service, "TmdbClient", FakeTmdb)  # type: ignore[attr-defined]
    with app.app_context():
        db.session.add_all(
            [
                Setting(key="metadata.tmdb.enabled", value="true"),
                Setting(key="metadata.tmdb.token", value="tmdb-token"),
                Setting(key="metadata.language", value="pt-BR"),
            ]
        )
        for index in range(3):
            item = MediaItem(kind=MediaKind.MOVIE, title=f"Already enriched {index}")
            db.session.add(item)
            db.session.flush()
            db.session.add_all(
                [
                    MediaIdentifier(
                        media_item_id=item.id, provider="tmdb", external_id=str(index)
                    ),
                    EnrichmentRecord(
                        media_item_id=item.id,
                        provider="tmdb",
                        status="succeeded",
                        attempts=1,
                    ),
                ]
            )
        target = MediaItem(kind=MediaKind.MOVIE, title="Arrival")
        db.session.add(target)
        db.session.flush()
        db.session.add(
            MediaIdentifier(media_item_id=target.id, provider="tmdb", external_id="329865")
        )
        db.session.commit()

        result = service.enrich_catalog(app, limit=1)

        assert result == {"processed": 1, "updated": 1, "failed": 0}
        assert db.session.scalar(
            select(MediaImage).where(MediaImage.media_item_id == target.id)
        ) is not None


def test_default_enrichment_processes_more_than_one_hundred_candidates(
    app: Flask, monkeypatch: object
) -> None:
    class AnyTmdb:
        def __init__(self, token: str) -> None:
            assert token == "tmdb-token"

        def lookup(
            self, media_type: str, external_id: str, *, language: str
        ) -> EnrichedMetadata:
            assert media_type == "movie"
            return EnrichedMetadata(summary=f"Summary {external_id}")

        def close(self) -> None:
            pass

    monkeypatch.setattr(service, "TmdbClient", AnyTmdb)  # type: ignore[attr-defined]
    with app.app_context():
        db.session.add_all(
            [
                Setting(key="metadata.tmdb.enabled", value="true"),
                Setting(key="metadata.tmdb.token", value="tmdb-token"),
            ]
        )
        for index in range(105):
            item = MediaItem(kind=MediaKind.MOVIE, title=f"Movie {index}")
            db.session.add(item)
            db.session.flush()
            db.session.add(
                MediaIdentifier(
                    media_item_id=item.id,
                    provider="tmdb",
                    external_id=str(index),
                )
            )
        db.session.commit()
        progress_updates: list[dict[str, int]] = []

        result = service.enrich_catalog(app, progress=progress_updates.append)

        assert result == {"processed": 105, "updated": 105, "failed": 0}
        assert len(db.session.scalars(select(EnrichmentRecord)).all()) == 105
        assert progress_updates[0] == {
            "processed": 0, "updated": 0, "failed": 0,
            "total": 105, "percent": 0,
        }
        assert progress_updates[-1]["percent"] == 100
