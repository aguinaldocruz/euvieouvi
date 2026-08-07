"""Integration tests for migrations, integrity and transaction boundaries."""

from datetime import UTC, datetime

from flask import Flask
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from euvieouvi.database.enums import ConnectorType, MediaKind
from euvieouvi.database.models import MediaItem, Source, WatchEvent
from euvieouvi.database.schema import database_status
from euvieouvi.database.unit_of_work import UnitOfWork
from euvieouvi.extensions import db


def make_source(name: str = "Local Plex") -> Source:
    return Source(
        connector_type=ConnectorType.PLEX,
        name=name,
        base_url="http://plex.local:32400",
        secret="fixture-secret",
        enabled=True,
    )


def test_initial_migration_creates_expected_schema(app: Flask) -> None:
    with app.app_context():
        tables = set(inspect(db.engine).get_table_names())
        assert database_status() == (True, True)

    assert {
        "alembic_version",
        "sources",
        "libraries",
        "media_items",
        "source_media_refs",
        "media_identifiers",
        "watch_events",
        "watch_states",
        "sync_runs",
        "sync_run_libraries",
        "sync_checkpoints",
        "sync_errors",
        "settings",
    } <= tables


def test_history_indexes_support_large_playback_collections(app: Flask) -> None:
    with app.app_context():
        indexes = {index["name"] for index in inspect(db.engine).get_indexes("watch_events")}

    assert {
        "ix_watch_events_completed_id",
        "ix_watch_events_media_completed_watched",
        "ix_watch_events_watched_id",
    } <= indexes


def test_sqlite_pragmas_are_active(app: Flask) -> None:
    with app.app_context(), db.engine.connect() as connection:
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        synchronous = connection.execute(text("PRAGMA synchronous")).scalar_one()
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()

    assert foreign_keys == 1
    assert str(journal_mode).lower() == "wal"
    assert synchronous == 1
    assert busy_timeout == 5000


def test_repository_lookup_supports_idempotent_ingestion(app: Flask) -> None:
    with app.app_context():
        session = db.session()
        with UnitOfWork(session) as work:
            work.sources.add(make_source())
            work.commit()

        with UnitOfWork(session) as work:
            existing = work.sources.by_name("Local Plex")
            if existing is None:
                work.sources.add(make_source())
            work.commit()

        assert len(UnitOfWork(session).sources.list()) == 1


def test_uncommitted_unit_of_work_rolls_back(app: Flask) -> None:
    with app.app_context():
        session = db.session()
        with UnitOfWork(session) as work:
            work.sources.add(make_source("Rolled Back"))

        assert UnitOfWork(session).sources.by_name("Rolled Back") is None


def test_foreign_key_and_dedup_constraints_are_enforced(app: Flask) -> None:
    now = datetime.now(UTC)
    with app.app_context():
        source = make_source()
        item = MediaItem(kind=MediaKind.MOVIE, title="Example")
        db.session.add_all([source, item])
        db.session.commit()
        first = WatchEvent(
            media_item_id=item.id,
            source_id=source.id,
            dedup_key="same-event",
            watched_at=now,
            completed=True,
        )
        duplicate = WatchEvent(
            media_item_id=item.id,
            source_id=source.id,
            dedup_key="same-event",
            watched_at=now,
            completed=True,
        )
        db.session.add_all([first, duplicate])
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
        else:
            raise AssertionError("Duplicate watch event was accepted.")

        invalid_item = MediaItem(kind=MediaKind.SEASON, parent_id=999999, title="Invalid")
        db.session.add(invalid_item)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
        else:
            raise AssertionError("Invalid foreign key was accepted.")


def test_checkpoint_like_write_does_not_survive_rollback(app: Flask) -> None:
    with app.app_context():
        session = db.session()
        work = UnitOfWork(session)
        work.sources.add(make_source("Transient"))
        work.rollback()
        assert work.sources.by_name("Transient") is None
