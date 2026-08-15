"""Track the physical server behind each configured source.

Revision ID: 20260815_0022
Revises: 20260815_0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_0022"
down_revision: str | None = "20260815_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sources") as batch_op:
        batch_op.add_column(sa.Column("server_identifier", sa.String(length=255)))


def downgrade() -> None:
    with op.batch_alter_table("sources") as batch_op:
        batch_op.drop_column("server_identifier")
