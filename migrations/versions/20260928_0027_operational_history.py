"""Persist bounded operational health samples.

Revision ID: 20260928_0027
Revises: 20260928_0026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0027"
down_revision: str | None = "20260928_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operational_samples",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_jobs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("heartbeat_age_seconds", sa.Integer()),
        sa.Column("open_circuits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("connector_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("average_connector_latency_ms", sa.Float()),
        sa.Column("slow_operations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("database_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wal_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alert_level", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("alert_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alert_summary", sa.Text()),
        sa.CheckConstraint(
            "queued_jobs >= 0 AND pending_tasks >= 0 AND open_circuits >= 0 AND "
            "connector_failures >= 0 AND slow_operations >= 0 AND alert_count >= 0",
            name="ck_operational_samples_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_operational_samples_captured", "operational_samples", ["captured_at"])
    op.create_index(
        "ix_operational_samples_level_captured",
        "operational_samples",
        ["alert_level", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_operational_samples_level_captured", table_name="operational_samples")
    op.drop_index("ix_operational_samples_captured", table_name="operational_samples")
    op.drop_table("operational_samples")
