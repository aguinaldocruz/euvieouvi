"""Store watched-event user ownership.

Revision ID: 20260813_0015
Revises: 20260812_0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0015"
down_revision: str | None = "20260812_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("watch_events") as batch:
        batch.add_column(sa.Column("playback_user", sa.String(length=255)))


def downgrade() -> None:
    with op.batch_alter_table("watch_events") as batch:
        batch.drop_column("playback_user")
