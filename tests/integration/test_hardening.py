"""Persistence, update, backup and representative volume gates."""

# mypy: disable-error-code="index"

from __future__ import annotations

import sqlite3
from pathlib import Path
from time import perf_counter

import pytest
from flask import Flask
from sqlalchemy import func, insert, select

from euvieouvi import create_app
from euvieouvi.database.backup import backup_database, restore_database
from euvieouvi.database.enums import ConnectorType, MediaKind
from euvieouvi.database.models import MediaItem, Source
from euvieouvi.extensions import db


def test_backup_restore_and_application_recreation_preserve_volume(
    app: Flask, tmp_path: Path
) -> None:
    with app.app_context():
        source = Source(
            connector_type=ConnectorType.PLEX,
            name="Persistent Plex",
            base_url="http://plex:32400",
            secret="volume-secret",
            enabled=True,
        )
        db.session.add(source)
        db.session.commit()
        database_path = Path(db.engine.url.database or "")
        assert db.session.scalar(select(func.count()).select_from(Source)) == 1

    backup_path = tmp_path / "outside-volume" / "euvieouvi-backup.db"
    restored_path = tmp_path / "restored" / "euvieouvi.db"
    backup_database(database_path, backup_path)
    restore_database(backup_path, restored_path)

    with sqlite3.connect(restored_path) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT name FROM sources").fetchone() == ("Persistent Plex",)

    recreated = create_app(
        {
            "ENVIRONMENT": "testing",
            "SECRET_KEY": "recreated-test-secret",
            "TESTING": True,
            "INSTANCE_PATH": Path(app.instance_path),
            "DATABASE_URI": app.config["SQLALCHEMY_DATABASE_URI"],
        }
    )
    with recreated.app_context():
        persisted = db.session.scalar(select(Source).where(Source.name == "Persistent Plex"))
        assert persisted is not None and persisted.secret == "volume-secret"
        assert recreated.test_client().get("/health/ready").status_code == 200


@pytest.mark.volume
def test_three_thousand_movies_remain_paginated_and_queryable(app: Flask) -> None:
    started = perf_counter()
    with app.app_context():
        db.session.execute(
            insert(MediaItem),
            [
                {
                    "kind": MediaKind.MOVIE,
                    "title": f"Volume Movie {number:04d}",
                    "sort_title": f"Volume Movie {number:04d}",
                    "year": 1980 + (number % 47),
                }
                for number in range(3000)
            ],
        )
        db.session.commit()
        database_path = Path(db.engine.url.database or "")
        db.session.execute(db.text("PRAGMA wal_checkpoint(PASSIVE)"))

    client = app.test_client()
    first = client.get("/api/v1/media?kind=movie&limit=200")
    assert first.status_code == 200
    assert len(first.json["items"]) == 200
    assert first.json["pagination"]["has_more"] is True
    second = client.get(
        "/api/v1/media?kind=movie&limit=200&cursor=" + first.json["pagination"]["next_cursor"]
    )
    assert second.status_code == 200 and len(second.json["items"]) == 200
    assert client.get("/history?watched=all&page=60").status_code == 200
    assert client.get("/").status_code == 200
    assert database_path.stat().st_size > 0
    assert perf_counter() - started < 10
