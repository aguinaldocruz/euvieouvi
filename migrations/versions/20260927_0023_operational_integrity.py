"""Protect active jobs and preserve progress arriving during queue processing.

Revision ID: 20260927_0023
Revises: 20260815_0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0023"
down_revision: str | None = "20260815_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("async_tasks") as batch:
        batch.add_column(sa.Column("pending_payload", sa.Text(), nullable=True))
    connection = op.get_bind()
    active_rows = connection.execute(
        sa.text(
            "SELECT id, job_id FROM job_runs "
            "WHERE status IN ('queued', 'running') ORDER BY job_id, id DESC"
        )
    ).all()
    seen: set[str] = set()
    for run_id, job_id in active_rows:
        if job_id in seen:
            connection.execute(
                sa.text(
                    "UPDATE job_runs SET status='interrupted', "
                    "finished_at=COALESCE(finished_at, CURRENT_TIMESTAMP), "
                    "summary='Interrupted while enforcing one active execution per job.' "
                    "WHERE id=:run_id"
                ),
                {"run_id": run_id},
            )
        seen.add(job_id)
    op.create_index(
        "uq_job_runs_active_job",
        "job_runs",
        ["job_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index("ix_media_items_updated", "media_items", ["updated_at"])
    op.create_index("ix_source_media_refs_updated", "source_media_refs", ["updated_at"])
    op.create_index("ix_media_images_updated", "media_images", ["updated_at"])
    op.create_index("ix_watch_states_updated", "watch_states", ["updated_at"])
    op.create_index("ix_watch_events_updated", "watch_events", ["updated_at"])
    op.create_index("ix_media_identifiers_created", "media_identifiers", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_media_identifiers_created", table_name="media_identifiers")
    op.drop_index("ix_watch_events_updated", table_name="watch_events")
    op.drop_index("ix_watch_states_updated", table_name="watch_states")
    op.drop_index("ix_media_images_updated", table_name="media_images")
    op.drop_index("ix_source_media_refs_updated", table_name="source_media_refs")
    op.drop_index("ix_media_items_updated", table_name="media_items")
    op.drop_index("uq_job_runs_active_job", table_name="job_runs")
    with op.batch_alter_table("async_tasks") as batch:
        batch.drop_column("pending_payload")
