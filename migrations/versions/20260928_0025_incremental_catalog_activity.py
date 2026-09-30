"""Track playback rows requiring an incremental catalog refresh.

Revision ID: 20260928_0025
Revises: 20260928_0024
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0025"
down_revision: str | None = "20260928_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROJECTION_SETTING = "catalog.projection.state"
_ACTIVITY_TABLES = ("watch_events", "watch_states")


def upgrade() -> None:
    op.create_table(
        "catalog_projection_dirty_items",
        sa.Column("media_item_id", sa.Integer(), primary_key=True),
    )
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for table in _ACTIVITY_TABLES:
            for action in ("insert", "update", "delete"):
                op.execute(f"DROP TRIGGER IF EXISTS trg_catalog_dirty_{table}_{action}")
                _create_activity_trigger(table, action)
    connection.execute(
        sa.text(
            "UPDATE settings SET value=CASE WHEN value LIKE 'clean:%' THEN value "
            "ELSE 'identity' END, updated_at=CURRENT_TIMESTAMP WHERE key=:key"
        ),
        {"key": _PROJECTION_SETTING},
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for table in _ACTIVITY_TABLES:
            for action in ("insert", "update", "delete"):
                op.execute(f"DROP TRIGGER IF EXISTS trg_catalog_dirty_{table}_{action}")
                _create_legacy_activity_trigger(table, action)
    op.drop_table("catalog_projection_dirty_items")
    connection.execute(
        sa.text(
            "UPDATE settings SET value='identity', updated_at=CURRENT_TIMESTAMP WHERE key=:key"
        ),
        {"key": _PROJECTION_SETTING},
    )


def _create_activity_trigger(table: str, action: str) -> None:
    row_prefixes = (
        ("OLD", "NEW") if action == "update" else (("OLD",) if action == "delete" else ("NEW",))
    )
    dirty_statements = "\n".join(
        "INSERT OR IGNORE INTO catalog_projection_dirty_items(media_item_id) "
        f"VALUES ({prefix}.media_item_id);"
        for prefix in row_prefixes
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_catalog_dirty_{table}_{action}
        AFTER {action.upper()} ON {table}
        BEGIN
          {dirty_statements}
          UPDATE settings
             SET value=CASE WHEN value='identity' THEN value ELSE 'activity' END,
                 updated_at=CURRENT_TIMESTAMP
           WHERE key='{_PROJECTION_SETTING}';
        END
        """
    )


def _create_legacy_activity_trigger(table: str, action: str) -> None:
    op.execute(
        f"""
        CREATE TRIGGER trg_catalog_dirty_{table}_{action}
        AFTER {action.upper()} ON {table}
        BEGIN
          UPDATE settings
             SET value=CASE WHEN value='identity' THEN value ELSE 'activity' END,
                 updated_at=CURRENT_TIMESTAMP
           WHERE key='{_PROJECTION_SETTING}';
        END
        """
    )
