"""Allow tightly controlled external artwork fallbacks.

Revision ID: 20260805_0006
Revises: 20260805_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0006"
down_revision: str | None = "20260805_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_images") as batch:
        batch.alter_column("source_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("source_path", existing_type=sa.String(2048), nullable=True)
        batch.add_column(
            sa.Column("provider", sa.String(32), nullable=False, server_default="plex")
        )
        batch.add_column(sa.Column("source_url", sa.String(2048)))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT 1 FROM media_images WHERE source_id IS NULL LIMIT 1")
    ).first():
        raise RuntimeError("Cannot downgrade while external cached images exist.")
    with op.batch_alter_table("media_images") as batch:
        batch.drop_column("source_url")
        batch.drop_column("provider")
        batch.alter_column("source_path", existing_type=sa.String(2048), nullable=False)
        batch.alter_column("source_id", existing_type=sa.Integer(), nullable=False)
