"""Durability guarantees for instant asynchronous updates."""

from datetime import UTC, datetime

from flask import Flask

from euvieouvi.database.enums import ConnectorType, LibraryMediaType, MediaKind
from euvieouvi.database.models import AsyncTask, Library, MediaItem, Source, SourceMediaRef
from euvieouvi.extensions import db
from euvieouvi.sync.async_tasks import AsyncTaskExecutor, enqueue_watch_update, recover_async_tasks


def test_watch_update_enqueue_is_durable_and_idempotent(app: Flask) -> None:
    watched_at = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)
    with app.app_context():
        enqueue_watch_update(
            db.session(), source_id=1, external_id="movie-1", watched_at=watched_at
        )
        enqueue_watch_update(
            db.session(), source_id=1, external_id="movie-1", watched_at=watched_at
        )
        db.session.commit()

        task = db.session.query(AsyncTask).one()
        assert task.status == "pending" and task.attempts == 0
        task.status = "processing"
        db.session.commit()
        assert recover_async_tasks() == 1
        assert db.session.get(AsyncTask, task.id).status == "pending"  # type: ignore[union-attr]


def test_watch_update_without_cross_server_match_is_completed_as_noop(app: Flask) -> None:
    watched_at = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    with app.app_context():
        source = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex",
            secret="token",
            enabled=True,
        )
        db.session.add(source)
        db.session.flush()
        library = Library(
            source_id=source.id,
            external_id="music",
            name="Music",
            media_type=LibraryMediaType.ARTIST,
            enabled=True,
            available=True,
            discovered_at=watched_at,
            last_seen_at=watched_at,
        )
        item = MediaItem(kind=MediaKind.TRACK, title="Unmatched track")
        db.session.add_all([library, item])
        db.session.flush()
        db.session.add(
            SourceMediaRef(
                source_id=source.id,
                library_id=library.id,
                media_item_id=item.id,
                external_id="track-1",
                last_seen_at=watched_at,
                available=True,
            )
        )
        enqueue_watch_update(
            db.session(), source_id=source.id, external_id="track-1", watched_at=watched_at
        )
        db.session.commit()
        task_id = db.session.query(AsyncTask.id).scalar()

        succeeded, changed, failure = AsyncTaskExecutor(app)._run_one(task_id)

        assert (succeeded, changed, failure) == (True, False, None)
        assert db.session.get(AsyncTask, task_id) is None


def test_async_failure_detail_identifies_task_and_media(app: Flask) -> None:
    watched_at = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    with app.app_context():
        enqueue_watch_update(
            db.session(), source_id=999, external_id="missing-1", watched_at=watched_at
        )
        db.session.commit()
        task_id = db.session.query(AsyncTask.id).scalar()

        succeeded, changed, failure = AsyncTaskExecutor(app)._run_one(task_id)

        assert succeeded is False and changed is False
        assert failure is not None
        assert f"task={task_id}" in failure
        assert "origem=999" in failure and "mídia=missing-1" in failure
        assert "LookupError: watch update source is not available" in failure
