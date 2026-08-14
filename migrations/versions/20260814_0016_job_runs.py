"""Persist independent job executions and log references.

Revision ID: 20260814_0016
Revises: 20260813_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0016"
down_revision: str | None = "20260813_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("trigger", sa.String(length=9), nullable=False),
        sa.Column("status", sa.String(length=11), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", sa.Text()),
        sa.Column("log_filename", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_job_runs_job_created", "job_runs", ["job_id", "created_at"])
    op.create_index("ix_job_runs_status_created", "job_runs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_job_runs_status_created", table_name="job_runs")
    op.drop_index("ix_job_runs_job_created", table_name="job_runs")
    op.drop_table("job_runs")
