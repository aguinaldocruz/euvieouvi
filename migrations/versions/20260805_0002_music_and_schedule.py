"""Add music hierarchy and scheduled sync values.

Revision ID: 20260805_0002
Revises: 20260804_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0002"
down_revision: str | None = "20260804_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

old_library_type = sa.Enum(
    "movie", "show", name="librarymediatype", native_enum=False, create_constraint=True
)
new_library_type = sa.Enum(
    "movie",
    "show",
    "artist",
    name="librarymediatype",
    native_enum=False,
    create_constraint=True,
)
old_media_kind = sa.Enum(
    "movie",
    "show",
    "season",
    "episode",
    name="mediakind",
    native_enum=False,
    create_constraint=True,
)
new_media_kind = sa.Enum(
    "movie",
    "show",
    "season",
    "episode",
    "artist",
    "album",
    "track",
    name="mediakind",
    native_enum=False,
    create_constraint=True,
)
old_sync_trigger = sa.Enum(
    "manual", "api", name="synctrigger", native_enum=False, create_constraint=True
)
new_sync_trigger = sa.Enum(
    "manual",
    "api",
    "scheduled",
    name="synctrigger",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    with op.batch_alter_table("libraries") as batch:
        batch.alter_column(
            "media_type", existing_type=old_library_type, type_=new_library_type, nullable=False
        )
    with op.batch_alter_table("media_items") as batch:
        batch.alter_column(
            "kind", existing_type=old_media_kind, type_=new_media_kind, nullable=False
        )
        batch.add_column(sa.Column("disc_number", sa.Integer()))
        batch.add_column(sa.Column("track_number", sa.Integer()))
        batch.create_index(
            "ix_media_items_music_hierarchy",
            ["parent_id", "disc_number", "track_number"],
        )
    with op.batch_alter_table("sync_runs") as batch:
        batch.alter_column(
            "trigger", existing_type=old_sync_trigger, type_=new_sync_trigger, nullable=False
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT 1 FROM libraries WHERE media_type = 'artist' LIMIT 1")
    ).first():
        raise RuntimeError("Cannot downgrade while music libraries exist.")
    if connection.execute(
        sa.text("SELECT 1 FROM media_items WHERE kind IN ('artist','album','track') LIMIT 1")
    ).first():
        raise RuntimeError("Cannot downgrade while music items exist.")
    with op.batch_alter_table("sync_runs") as batch:
        batch.alter_column(
            "trigger", existing_type=new_sync_trigger, type_=old_sync_trigger, nullable=False
        )
    with op.batch_alter_table("media_items") as batch:
        batch.drop_index("ix_media_items_music_hierarchy")
        batch.drop_column("track_number")
        batch.drop_column("disc_number")
        batch.alter_column(
            "kind", existing_type=new_media_kind, type_=old_media_kind, nullable=False
        )
    with op.batch_alter_table("libraries") as batch:
        batch.alter_column(
            "media_type", existing_type=new_library_type, type_=old_library_type, nullable=False
        )
