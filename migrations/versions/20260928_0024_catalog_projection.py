"""Add a persistent canonical catalog projection.

Revision ID: 20260928_0024
Revises: 20260927_0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0024"
down_revision: str | None = "20260927_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROJECTION_SETTING = "catalog.projection.state"
_IDENTITY_TABLES = ("media_items", "media_identifiers", "source_media_refs")
_ACTIVITY_TABLES = ("watch_events", "watch_states")


def upgrade() -> None:
    op.create_table(
        "catalog_entries",
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("group_key", sa.String(length=80), nullable=False, unique=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("completion_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_played_at", sa.DateTime(timezone=True)),
        sa.Column("last_completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_played_at", sa.DateTime(timezone=True)),
        sa.Column("progress_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("child_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_child_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("available_plex", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("available_jellyfin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("completion_count >= 0", name="ck_catalog_entries_completion_count"),
        sa.CheckConstraint("child_count >= 0", name="ck_catalog_entries_child_count"),
        sa.CheckConstraint(
            "completed_child_count >= 0 AND completed_child_count <= child_count",
            name="ck_catalog_entries_completed_children",
        ),
    )
    op.create_index(
        "ix_catalog_entries_kind_last_played",
        "catalog_entries",
        ["kind", "last_played_at"],
    )
    op.create_index(
        "ix_catalog_entries_kind_first_played",
        "catalog_entries",
        ["kind", "first_played_at"],
    )
    op.create_index(
        "ix_catalog_entries_kind_play_count",
        "catalog_entries",
        ["kind", "completion_count"],
    )
    op.create_index(
        "ix_catalog_entries_kind_availability",
        "catalog_entries",
        ["kind", "available"],
    )
    op.create_table(
        "catalog_entry_members",
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "catalog_media_item_id",
            sa.Integer(),
            sa.ForeignKey("catalog_entries.media_item_id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_catalog_entry_members_catalog",
        "catalog_entry_members",
        ["catalog_media_item_id", "media_item_id"],
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "INSERT INTO settings(key,value,updated_at) "
            "VALUES (:key,'identity',CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value='identity', updated_at=CURRENT_TIMESTAMP"
        ),
        {"key": _PROJECTION_SETTING},
    )
    if connection.dialect.name == "sqlite":
        for table in _IDENTITY_TABLES:
            for action in ("insert", "update", "delete"):
                _create_dirty_trigger(table, action, "identity")
        for table in _ACTIVITY_TABLES:
            for action in ("insert", "update", "delete"):
                _create_dirty_trigger(table, action, "activity")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for table in (*_IDENTITY_TABLES, *_ACTIVITY_TABLES):
            for action in ("insert", "update", "delete"):
                op.execute(f"DROP TRIGGER IF EXISTS trg_catalog_dirty_{table}_{action}")
    op.drop_index("ix_catalog_entry_members_catalog", table_name="catalog_entry_members")
    op.drop_table("catalog_entry_members")
    op.drop_index("ix_catalog_entries_kind_availability", table_name="catalog_entries")
    op.drop_index("ix_catalog_entries_kind_play_count", table_name="catalog_entries")
    op.drop_index("ix_catalog_entries_kind_first_played", table_name="catalog_entries")
    op.drop_index("ix_catalog_entries_kind_last_played", table_name="catalog_entries")
    op.drop_table("catalog_entries")
    connection.execute(sa.text("DELETE FROM settings WHERE key=:key"), {"key": _PROJECTION_SETTING})


def _create_dirty_trigger(table: str, action: str, state: str) -> None:
    value = (
        "'identity'"
        if state == "identity"
        else "CASE WHEN value='identity' THEN value ELSE 'activity' END"
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_catalog_dirty_{table}_{action}
        AFTER {action.upper()} ON {table}
        BEGIN
          UPDATE settings
             SET value={value}, updated_at=CURRENT_TIMESTAMP
           WHERE key='{_PROJECTION_SETTING}';
        END
        """
    )
