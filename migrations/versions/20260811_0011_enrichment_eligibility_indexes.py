"""Index enrichment eligibility and poster lookups.

Revision ID: 20260811_0011
Revises: 4ac542335f9b
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0011"
down_revision: str | None = "4ac542335f9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MISSING_METADATA = (
    "summary IS NULL OR tagline IS NULL OR studio IS NULL "
    "OR audience_rating IS NULL"
)


def upgrade() -> None:
    op.create_index(
        "ix_media_items_enrichment_missing",
        "media_items",
        ["kind", "id"],
        unique=False,
        sqlite_where=sa.text(MISSING_METADATA),
        postgresql_where=sa.text(MISSING_METADATA),
    )
    op.create_index(
        "ix_media_images_type_item",
        "media_images",
        ["image_type", "media_item_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_media_images_type_item",
        table_name="media_images",
    )
    op.drop_index(
        "ix_media_items_enrichment_missing",
        table_name="media_items",
    )
