"""Consistent SQLite backup and stopped-service restore primitives."""

import argparse
import sqlite3
from collections.abc import Sequence
from pathlib import Path


def backup_database(source: Path, destination: Path) -> None:
    """Create a transactionally consistent backup using SQLite's backup API."""
    _validate_paths(source, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_connection, sqlite3.connect(destination) as target:
        source_connection.backup(target)


def restore_database(backup: Path, destination: Path) -> None:
    """Restore a verified SQLite backup; caller must ensure the service is stopped."""
    _validate_paths(backup, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(backup) as source_connection, sqlite3.connect(destination) as target:
        source_connection.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError("Restored database failed SQLite integrity_check.")


def _validate_paths(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    if source.resolve() == destination.resolve():
        raise ValueError("SQLite source and destination must be different paths.")


def main(argv: Sequence[str] | None = None) -> None:
    """Run an explicit backup or stopped-service restore operation."""
    parser = argparse.ArgumentParser(description="Back up or restore an euvieouvi SQLite database.")
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args(argv)
    operation = backup_database if arguments.operation == "backup" else restore_database
    operation(arguments.source, arguments.destination)
    print(f"SQLite {arguments.operation} completed: {arguments.destination}", flush=True)


if __name__ == "__main__":
    main()
