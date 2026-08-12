"""Store webhook playback users.

Revision ID: 20260812_0014
Revises: 20260812_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0014"
down_revision: str | None = "20260812_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("webhook_events") as batch:
        batch.add_column(sa.Column("playback_user", sa.String(length=255)))


def downgrade() -> None:
    with op.batch_alter_table("webhook_events") as batch:
        batch.drop_column("playback_user")
