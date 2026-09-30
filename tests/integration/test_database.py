"""Integration tests for migrations, integrity and transaction boundaries."""

from datetime import UTC, datetime

import pytest
from flask import Flask
from flask_migrate import downgrade, upgrade  # type: ignore[import-untyped]
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from euvieouvi.database.enums import ConnectorType, MediaKind, SyncStatus, SyncTrigger
from euvieouvi.database.models import (
    AsyncTask,
    JobRun,
    MediaItem,
    Setting,
    Source,
    SyncRun,
    WatchEvent,
)
from euvieouvi.database.schema import database_status
from euvieouvi.database.unit_of_work import UnitOfWork
from euvieouvi.extensions import db
from euvieouvi.sync.jobs import _rotate_sync_runs


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
        "catalog_entries",
        "catalog_entry_members",
        "catalog_projection_dirty_items",
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


def test_latest_operational_migration_is_reversible(app: Flask) -> None:
    with app.app_context():
        downgrade(directory="migrations", revision="20260815_0022")
        inspector = inspect(db.engine)
        assert "catalog_entries" not in set(inspector.get_table_names())
        assert "pending_payload" not in {
            column["name"] for column in inspector.get_columns("async_tasks")
        }
        assert "uq_job_runs_active_job" not in {
            index["name"] for index in inspector.get_indexes("job_runs")
        }

        upgrade(directory="migrations")
        inspector = inspect(db.engine)
        assert {
            "catalog_entries",
            "catalog_entry_members",
            "catalog_projection_dirty_items",
        } <= set(inspector.get_table_names())
        assert "pending_payload" in {
            column["name"] for column in inspector.get_columns("async_tasks")
        }
        assert "uq_job_runs_active_job" in {
            index["name"] for index in inspector.get_indexes("job_runs")
        }
        assert "last_played_at" in {
            column["name"] for column in inspector.get_columns("watch_states")
        }


def test_playback_migration_uses_real_dates_not_collection_time(app: Flask) -> None:
    with app.app_context():
        source = make_source()
        item = MediaItem(kind=MediaKind.MOVIE, title="Migration test")
        db.session.add_all([source, item])
        db.session.commit()
        source_id, item_id = source.id, item.id
        downgrade(directory="migrations", revision="20260930_0028")
        db.session.execute(
            text("""
            INSERT INTO watch_states(media_item_id, source_id, view_count, completed,
                progress_ms, observed_at, created_at, updated_at)
            VALUES(:item, :source, 0, 0, 12000, '2026-09-30 06:38:04',
                '2026-09-30 06:38:04', '2026-09-30 06:38:04')
        """),
            {"item": item_id, "source": source_id},
        )
        db.session.commit()
        upgrade(directory="migrations")
        assert db.session.execute(text("SELECT last_played_at FROM watch_states")).scalar() is None
        downgrade(directory="migrations", revision="20260930_0028")
        db.session.execute(text("UPDATE watch_states SET last_watched_at='2026-09-29 21:40:55'"))
        db.session.commit()
        upgrade(directory="migrations")
        assert (
            db.session.execute(text("SELECT last_played_at FROM watch_states")).scalar()
            == "2026-09-29 21:40:55"
        )


def test_history_indexes_support_large_playback_collections(app: Flask) -> None:
    with app.app_context():
        indexes = {index["name"] for index in inspect(db.engine).get_indexes("watch_events")}

    assert {
        "ix_watch_events_completed_id",
        "ix_watch_events_media_completed_watched",
        "ix_watch_events_watched_id",
    } <= indexes


def test_operational_indexes_and_single_active_job_are_enforced(app: Flask) -> None:
    with app.app_context():
        inspector = inspect(db.engine)
        assert "pending_payload" in {
            column["name"] for column in inspector.get_columns("async_tasks")
        }
        assert "uq_job_runs_active_job" in {
            index["name"] for index in inspector.get_indexes("job_runs")
        }
        assert "ix_media_items_updated" in {
            index["name"] for index in inspector.get_indexes("media_items")
        }
        first = JobRun(
            job_id="metadata",
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.RUNNING,
            progress_percent=1,
        )
        db.session.add(first)
        db.session.commit()
        db.session.add(
            JobRun(
                job_id="metadata",
                trigger=SyncTrigger.SCHEDULED,
                status=SyncStatus.QUEUED,
                progress_percent=0,
            )
        )
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

        task = AsyncTask(
            task_type="watch_update",
            dedup_key="schema-check",
            payload="{}",
            pending_payload="{}",
            status="pending",
            next_attempt_at=datetime.now(UTC),
        )
        db.session.add(task)
        db.session.commit()
        assert task.pending_payload == "{}"


def test_sync_run_retention_is_applied_per_source_and_preserves_active_runs(app: Flask) -> None:
    with app.app_context():
        plex = make_source("Plex retention")
        jellyfin = Source(
            connector_type=ConnectorType.JELLYFIN,
            name="Jellyfin retention",
            base_url="http://jellyfin.local:8096",
            secret='{"api_key":"key","user_id":"user"}',
            enabled=True,
        )
        db.session.add_all([plex, jellyfin])
        db.session.flush()
        db.session.add(Setting(key="sync.retention.keep_last", value="1"))
        for source, day in ((plex, 1), (jellyfin, 2)):
            db.session.add_all(
                [
                    SyncRun(
                        source_id=source.id,
                        trigger=SyncTrigger.MANUAL,
                        status=SyncStatus.SUCCEEDED,
                        created_at=datetime(2026, 8, day, tzinfo=UTC),
                        finished_at=datetime(2026, 8, day, 1, tzinfo=UTC),
                    ),
                    SyncRun(
                        source_id=source.id,
                        trigger=SyncTrigger.MANUAL,
                        status=SyncStatus.SUCCEEDED,
                        created_at=datetime(2026, 8, day + 10, tzinfo=UTC),
                        finished_at=datetime(2026, 8, day + 10, 1, tzinfo=UTC),
                    ),
                ]
            )
        active = SyncRun(
            source_id=plex.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.RUNNING,
            created_at=datetime(2026, 8, 20, tzinfo=UTC),
        )
        db.session.add(active)
        db.session.commit()
        active_id = active.id

        assert _rotate_sync_runs() == 2
        remaining = db.session.scalars(select(SyncRun).order_by(SyncRun.id)).all()
        assert len(remaining) == 3
        assert {run.source_id for run in remaining if run.status is SyncStatus.SUCCEEDED} == {
            plex.id,
            jellyfin.id,
        }
        assert db.session.get(SyncRun, active_id) is not None


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
