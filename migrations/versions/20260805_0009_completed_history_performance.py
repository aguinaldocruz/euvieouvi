"""Optimize completed-history lookups after large Trakt imports.

Revision ID: 20260805_0009
Revises: 20260805_0008
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260805_0009"
down_revision: str | None = "20260805_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_watch_events_media_completed_watched",
        "watch_events",
        ["media_item_id", "completed", "watched_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_watch_events_media_completed_watched",
        table_name="watch_events",
    )
