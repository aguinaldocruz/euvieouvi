"""Separate genuine playback timestamps from synchronization observations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0029"
down_revision: str | None = "20260930_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("watch_states", sa.Column("last_played_at", sa.DateTime(timezone=True)))
    # observed_at is collection time, not evidence of playback. Never backfill from it.
    op.execute("UPDATE watch_states SET last_played_at=last_watched_at")
    op.execute("UPDATE settings SET value='full' WHERE key='catalog.projection.state'")


def downgrade() -> None:
    op.drop_column("watch_states", "last_played_at")
    op.execute("UPDATE settings SET value='full' WHERE key='catalog.projection.state'")
