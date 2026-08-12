"""Store webhook playback progress.

Revision ID: 20260812_0013
Revises: 20260812_0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0013"
down_revision: str | None = "20260812_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("webhook_events") as batch:
        batch.add_column(sa.Column("progress_percent", sa.Integer()))


def downgrade() -> None:
    with op.batch_alter_table("webhook_events") as batch:
        batch.drop_column("progress_percent")
