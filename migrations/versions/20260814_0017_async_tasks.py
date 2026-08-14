"""Add a durable asynchronous task queue.

Revision ID: 20260814_0017
Revises: 20260814_0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0017"
down_revision: str | None = "20260814_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "async_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("dedup_key", sa.String(128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedup_key", name="uq_async_tasks_dedup"),
    )
    op.create_index(
        "ix_async_tasks_due", "async_tasks", ["status", "next_attempt_at", "id"]
    )
    op.execute("UPDATE settings SET value = 'false' WHERE key = 'watch_sync.pending'")


def downgrade() -> None:
    op.drop_index("ix_async_tasks_due", table_name="async_tasks")
    op.drop_table("async_tasks")
