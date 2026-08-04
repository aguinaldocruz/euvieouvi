"""Database extensions and SQLite connection policy."""

from flask import Flask
from flask_migrate import Migrate  # type: ignore[import-untyped]
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Engine, event
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base shared by all persistence models."""


db = SQLAlchemy(model_class=Base)
migrate = Migrate(compare_type=True, render_as_batch=True)


@event.listens_for(Engine, "connect")
def configure_sqlite_connection(dbapi_connection: object, connection_record: object) -> None:
    """Apply integrity and concurrency pragmas to every SQLite connection."""
    del connection_record
    module_name = type(dbapi_connection).__module__
    if not module_name.startswith("sqlite3"):
        return
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.close()


def init_extensions(app: Flask) -> None:
    """Initialize database lifecycle and migration commands."""
    timeout = int(app.config["SQLITE_BUSY_TIMEOUT_MS"])
    app.config.setdefault(
        "SQLALCHEMY_ENGINE_OPTIONS", {"connect_args": {"timeout": timeout / 1000}}
    )
    db.init_app(app)

    from euvieouvi.database import models  # noqa: F401

    migrate.init_app(app, db, directory="migrations")
