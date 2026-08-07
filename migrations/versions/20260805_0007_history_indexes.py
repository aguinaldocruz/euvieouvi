"""Index high-volume playback history queries.

Revision ID: 20260805_0007
Revises: 20260805_0006
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260805_0007"
down_revision: str | None = "20260805_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_watch_events_completed_id", "watch_events", ["completed", "id"])
    op.create_index("ix_watch_events_watched_id", "watch_events", ["watched_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_watch_events_watched_id", table_name="watch_events")
    op.drop_index("ix_watch_events_completed_id", table_name="watch_events")
