"""Physical media evidence and persistent manual TMDB identification."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0028"
down_revision: str | None = "20260928_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("media_items", sa.Column("manual_match_tmdb_id", sa.String(32)))
    op.create_table(
        "media_file_paths",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reference_id",
            sa.Integer(),
            sa.ForeignKey("source_media_refs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path_key", sa.String(64), nullable=False),
        sa.Column("original_path", sa.Text(), nullable=False),
        sa.Column("canonical_path", sa.Text(), nullable=False),
        sa.UniqueConstraint("reference_id", "path_key", name="uq_media_file_paths_reference_key"),
    )
    op.create_index("ix_media_file_paths_key", "media_file_paths", ["path_key"])
    # Existing incremental baselines predate physical evidence. Recollect once.
    op.execute("DELETE FROM sync_checkpoints")
    op.execute("""
        UPDATE watch_states SET completed=1
        WHERE completed=0 AND (view_count>0 OR EXISTS (
            SELECT 1 FROM watch_events e WHERE e.media_item_id=watch_states.media_item_id
              AND e.source_id=watch_states.source_id AND e.completed=1))
    """)
    op.execute("DELETE FROM settings WHERE key LIKE 'watch_sync.incremental.%'")


def downgrade() -> None:
    op.drop_table("media_file_paths")
    op.drop_column("media_items", "manual_match_tmdb_id")
