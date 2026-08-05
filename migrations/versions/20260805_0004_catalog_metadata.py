"""Add searchable catalog metadata and normalized genres.

Revision ID: 20260805_0004
Revises: 20260805_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0004"
down_revision: str | None = "20260805_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_items") as batch:
        batch.add_column(sa.Column("tagline", sa.Text()))
        batch.add_column(sa.Column("studio", sa.String(255)))
        batch.add_column(sa.Column("content_rating", sa.String(64)))
        batch.add_column(sa.Column("audience_rating", sa.Float()))
        batch.add_column(sa.Column("source_added_at", sa.DateTime(timezone=True)))
        batch.create_index("ix_media_items_year", ["year"])
        batch.create_index("ix_media_items_source_added", ["source_added_at"])
    with op.batch_alter_table("source_media_refs") as batch:
        batch.add_column(sa.Column("unavailable_since", sa.DateTime(timezone=True)))
        batch.create_index("ix_source_media_refs_unavailable_since", ["unavailable_since"])
    op.create_table(
        "genres",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("normalized_name", sa.String(255), nullable=False),
        sa.UniqueConstraint("normalized_name", name="uq_genres_normalized_name"),
    )
    op.create_table(
        "media_genres",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "genre_id",
            sa.Integer(),
            sa.ForeignKey("genres.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("media_item_id", "genre_id", name="uq_media_genres_identity"),
    )
    op.create_index(
        "ix_media_genres_genre_media", "media_genres", ["genre_id", "media_item_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_media_genres_genre_media", table_name="media_genres")
    op.drop_table("media_genres")
    op.drop_table("genres")
    with op.batch_alter_table("source_media_refs") as batch:
        batch.drop_index("ix_source_media_refs_unavailable_since")
        batch.drop_column("unavailable_since")
    with op.batch_alter_table("media_items") as batch:
        batch.drop_index("ix_media_items_source_added")
        batch.drop_index("ix_media_items_year")
        batch.drop_column("source_added_at")
        batch.drop_column("audience_rating")
        batch.drop_column("content_rating")
        batch.drop_column("studio")
        batch.drop_column("tagline")
