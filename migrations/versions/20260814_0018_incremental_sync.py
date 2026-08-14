"""Track incremental catalog and periodic full-scan watermarks.

Revision ID: 20260814_0018
Revises: 20260814_0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0018"
down_revision: str | None = "20260814_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sync_checkpoints") as batch:
        batch.add_column(sa.Column("last_full_scan_at", sa.DateTime(timezone=True)))
    # Every completed checkpoint created by the previous strategy represents a
    # full catalog pass. Seed both clocks so upgrades become incremental
    # immediately, with the same overlap used by the runtime.
    op.execute(
        "UPDATE sync_checkpoints "
        "SET watermark_at = datetime(updated_at, '-2 minutes'), "
        "last_full_scan_at = updated_at "
        "WHERE last_successful_run_id IS NOT NULL AND cursor IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("sync_checkpoints") as batch:
        batch.drop_column("last_full_scan_at")
