"""Prefer the last completed full baseline over legacy partial cursors.

Revision ID: 20260814_0019
Revises: 20260814_0018
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260814_0019"
down_revision: str | None = "20260814_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE sync_checkpoints "
        "SET cursor = NULL, "
        "watermark_at = datetime((SELECT finished_at FROM sync_runs "
        "WHERE sync_runs.id = sync_checkpoints.last_successful_run_id), '-2 minutes'), "
        "last_full_scan_at = (SELECT finished_at FROM sync_runs "
        "WHERE sync_runs.id = sync_checkpoints.last_successful_run_id) "
        "WHERE last_successful_run_id IS NOT NULL AND last_full_scan_at IS NULL"
    )


def downgrade() -> None:
    pass
