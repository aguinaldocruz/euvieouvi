"""Add idempotent external enrichment audit records.

Revision ID: 20260805_0005
Revises: 20260805_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0005"
down_revision: str | None = "20260805_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "enrichment_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("message", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "media_item_id", "provider", name="uq_enrichment_records_item_provider"
        ),
    )
    op.create_index(
        "ix_enrichment_records_status_checked",
        "enrichment_records",
        ["status", "checked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_enrichment_records_status_checked", table_name="enrichment_records")
    op.drop_table("enrichment_records")
