"""Trakt importer compatibility with the current migrated database."""

import json
import sqlite3
from pathlib import Path
from zipfile import ZipFile

from flask import Flask
from scripts import import_trakt_export as importer

from euvieouvi.database.enums import ConnectorType
from euvieouvi.database.models import Source
from euvieouvi.extensions import db


def test_trakt_import_runs_against_current_schema(app: Flask, tmp_path: Path) -> None:
    with app.app_context():
        source = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex:32400",
            secret="secret",
            enabled=True,
        )
        db.session.add(source)
        db.session.commit()
        source_id = source.id
        database = Path(app.instance_path) / "test.db"
        db.session.remove()
        db.engine.dispose()

    archive = tmp_path / "trakt.zip"
    document = [
        {
            "id": 101,
            "watched_at": "2026-08-01T12:00:00Z",
            "type": "movie",
            "movie": {
                "title": "Historical Movie",
                "year": 2020,
                "ids": {"trakt": 101, "tmdb": 202},
            },
        }
    ]
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("watched-history-1.json", json.dumps(document))

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        importer._validate_database(connection)
        report = importer.import_archive(
            connection,
            archive,
            database,
            source_id=source_id,
            apply=True,
        )
        assert report.events_inserted == 1
        assert report.media_created == 1
        event = connection.execute(
            "SELECT completed, origin FROM watch_events WHERE source_event_id = 'trakt:101'"
        ).fetchone()
        assert event is not None and tuple(event) == (1, "trakt_import")
    finally:
        connection.close()
