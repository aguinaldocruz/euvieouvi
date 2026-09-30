"""Make operational jobs durable across process restarts.

Revision ID: 20260928_0026
Revises: 20260928_0025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0026"
down_revision: str | None = "20260928_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job_runs") as batch:
        batch.add_column(sa.Column("available_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("lease_owner", sa.String(length=96)))
        batch.add_column(sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3")
        )
        batch.add_column(sa.Column("execution_reference", sa.String(length=96)))
    op.execute(
        "UPDATE job_runs SET available_at=COALESCE(started_at, created_at, CURRENT_TIMESTAMP)"
    )
    op.create_index("ix_job_runs_queue", "job_runs", ["status", "available_at", "id"])
    op.create_index("ix_job_runs_lease", "job_runs", ["status", "lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_job_runs_lease", table_name="job_runs")
    op.drop_index("ix_job_runs_queue", table_name="job_runs")
    with op.batch_alter_table("job_runs") as batch:
        batch.drop_column("execution_reference")
        batch.drop_column("max_attempts")
        batch.drop_column("attempt_count")
        batch.drop_column("cancel_requested_at")
        batch.drop_column("lease_owner")
        batch.drop_column("lease_expires_at")
        batch.drop_column("heartbeat_at")
        batch.drop_column("available_at")
