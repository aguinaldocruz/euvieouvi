"""Track watch origins and recent webhook activity.

Revision ID: 20260811_0010
Revises: 20260805_0009
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0010"
down_revision: str | None = "20260805_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    with op.batch_alter_table("watch_events") as batch:
        batch.add_column(
            sa.Column(
                "origin",
                sa.String(length=32),
                nullable=False,
                server_default="synchronization",
            )
        )
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("event_key", sa.String(length=255)),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("media_kind", sa.String(length=32)),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_webhook_events_completed_occurred",
        "webhook_events",
        ["completed", "occurred_at"],
    )
    op.create_index("ix_webhook_events_active_source", "webhook_events", ["active", "source_id"])

def downgrade() -> None:
    op.drop_table("webhook_events")
    with op.batch_alter_table("watch_events") as batch:
        batch.drop_column("origin")
