"""track library sync progress

Revision ID: 4ac542335f9b
Revises: 20260811_0010
Create Date: 2026-08-11 16:48:04.121347
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4ac542335f9b"
down_revision: str | None = "20260811_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sync_run_libraries") as batch:
        batch.add_column(
            sa.Column("items_scanned", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("items_total", sa.Integer()))
        batch.create_check_constraint(
            "ck_sync_run_libraries_progress",
            "items_scanned >= 0 AND (items_total IS NULL OR items_total >= 0)",
        )
    with op.batch_alter_table("sync_runs") as batch:
        batch.add_column(sa.Column("watch_sync_status", sa.String(length=16)))
        batch.add_column(sa.Column("watch_sync_started_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("watch_sync_finished_at", sa.DateTime(timezone=True)))
        batch.add_column(
            sa.Column("watch_sync_scanned", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("watch_sync_updated", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("watch_sync_skipped", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("watch_sync_failed", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("watch_sync_summary", sa.Text()))
        batch.create_check_constraint(
            "ck_sync_runs_watch_sync_counters",
            "watch_sync_scanned >= 0 AND watch_sync_updated >= 0 AND "
            "watch_sync_skipped >= 0 AND watch_sync_failed >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("sync_runs") as batch:
        batch.drop_constraint("ck_sync_runs_watch_sync_counters", type_="check")
        batch.drop_column("watch_sync_summary")
        batch.drop_column("watch_sync_failed")
        batch.drop_column("watch_sync_skipped")
        batch.drop_column("watch_sync_updated")
        batch.drop_column("watch_sync_scanned")
        batch.drop_column("watch_sync_finished_at")
        batch.drop_column("watch_sync_started_at")
        batch.drop_column("watch_sync_status")
    with op.batch_alter_table("sync_run_libraries") as batch:
        batch.drop_constraint("ck_sync_run_libraries_progress", type_="check")
        batch.drop_column("items_total")
        batch.drop_column("items_scanned")
