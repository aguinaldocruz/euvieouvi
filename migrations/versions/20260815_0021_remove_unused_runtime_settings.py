"""Remove obsolete image-job runtime settings.

Job results are persisted in job_runs, so the former duplicate settings are unused.

Revision ID: 20260815_0021
Revises: 20260814_0020
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260815_0021"
down_revision: str | None = "20260814_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM settings WHERE key IN "
        "('jobs.catalog_images.last_finished_at', 'jobs.catalog_images.last_summary')"
    )


def downgrade() -> None:
    pass
