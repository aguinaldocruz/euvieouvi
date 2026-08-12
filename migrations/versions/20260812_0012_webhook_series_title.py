"""Store webhook episode series titles.

Revision ID: 20260812_0012
Revises: 20260811_0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0012"
down_revision: str | None = "20260811_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("webhook_events") as batch:
        batch.add_column(sa.Column("series_title", sa.String(length=500)))


def downgrade() -> None:
    with op.batch_alter_table("webhook_events") as batch:
        batch.drop_column("series_title")
