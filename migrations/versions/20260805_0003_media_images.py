"""Add external artwork references and local cache metadata.

Revision ID: 20260805_0003
Revises: 20260805_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0003"
down_revision: str | None = "20260805_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_images",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("image_type", sa.String(32), nullable=False),
        sa.Column("source_path", sa.String(2048), nullable=False),
        sa.Column("local_filename", sa.String(255)),
        sa.Column("mime_type", sa.String(128)),
        sa.Column("cache_status", sa.String(32), nullable=False),
        sa.Column("cached_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("media_item_id", "image_type", name="uq_media_images_item_type"),
    )
    op.create_index("ix_media_images_cache_status", "media_images", ["cache_status"])


def downgrade() -> None:
    op.drop_index("ix_media_images_cache_status", table_name="media_images")
    op.drop_table("media_images")
