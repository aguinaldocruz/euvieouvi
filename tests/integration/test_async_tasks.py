"""Durability guarantees for instant asynchronous updates."""

from datetime import UTC, datetime

from flask import Flask

from euvieouvi.database.models import AsyncTask
from euvieouvi.extensions import db
from euvieouvi.sync.async_tasks import enqueue_watch_update, recover_async_tasks


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
