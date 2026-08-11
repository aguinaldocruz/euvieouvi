"""Compatibility checks for the standalone Trakt importer."""

import sqlite3
from collections import defaultdict

import pytest
from scripts import import_trakt_export as importer


def database_at_revision(revision: str) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    for table in importer.REQUIRED_TABLES - {"alembic_version"}:
        connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
    connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
    return connection


def test_importer_accepts_current_database_revision() -> None:
    connection = database_at_revision("4ac542335f9b")
    try:
        importer._validate_database(connection)
    finally:
        connection.close()


def test_importer_rejects_obsolete_database_revision() -> None:
    connection = database_at_revision("20260804_0001")
    try:
        with pytest.raises(importer.ImportFailure, match="20260804_0001"):
            importer._validate_database(connection)
    finally:
        connection.close()


def test_media_index_prefers_selected_source_when_identifiers_tie() -> None:
    identifiers = (("tmdb", "241609"), ("tvdb", "443433"))
    values = defaultdict(set)
    for provider, external_id in identifiers:
        values[("show", provider, external_id)].update({22805, 51508})

    index = importer.MediaIndex(values, preferred_media_ids={22805})

    assert index.match("show", identifiers) == 22805


def test_media_index_keeps_tie_ambiguous_with_multiple_selected_source_items() -> None:
    identifiers = (("tmdb", "241609"),)
    values = defaultdict(set)
    values[("show", "tmdb", "241609")].update({22805, 51508})

    index = importer.MediaIndex(values, preferred_media_ids={22805, 51508})

    assert index.match("show", identifiers) == -1
