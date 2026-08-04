"""Database availability and migration revision checks."""

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from euvieouvi.extensions import db


def database_status() -> tuple[bool, bool]:
    """Return database connectivity and current-schema status."""
    try:
        with db.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            current_revision = MigrationContext.configure(connection).get_current_revision()
        migration = current_app.extensions["migrate"].migrate
        heads = set(ScriptDirectory.from_config(migration.get_config()).get_heads())
        return True, current_revision is not None and current_revision in heads
    except (KeyError, OSError, SQLAlchemyError):
        return False, False
