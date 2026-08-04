"""SQLite backup and restore tests."""

import sqlite3
from pathlib import Path

import pytest

from euvieouvi.database.backup import backup_database, main, restore_database


def test_backup_and_restore_preserve_integrity(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    restored = tmp_path / "restored.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO samples (value) VALUES ('preserved')")

    backup_database(source, backup)
    restore_database(backup, restored)

    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT value FROM samples").fetchone() == ("preserved",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_backup_rejects_missing_or_identical_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        backup_database(missing, tmp_path / "backup.db")

    source = tmp_path / "source.db"
    source.touch()
    with pytest.raises(ValueError, match="different paths"):
        backup_database(source, source)


def test_command_line_backup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")

    main(["backup", str(source), str(destination)])

    assert destination.is_file()
    assert "SQLite backup completed" in capsys.readouterr().out
