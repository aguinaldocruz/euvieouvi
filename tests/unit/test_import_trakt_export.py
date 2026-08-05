"""Compatibility checks for the standalone Trakt importer."""

import sqlite3

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
    connection = database_at_revision("20260805_0009")
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
