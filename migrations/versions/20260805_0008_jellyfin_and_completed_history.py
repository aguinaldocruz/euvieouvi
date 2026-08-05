"""Enable Jellyfin sources and retain completed history only.

Revision ID: 20260805_0008
Revises: 20260805_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0008"
down_revision: str | None = "20260805_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

old_connector = sa.Enum("plex", name="connectortype", native_enum=False, create_constraint=True)
new_connector = sa.Enum(
    "plex",
    "jellyfin",
    name="connectortype",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    op.execute(sa.text("DELETE FROM watch_events WHERE completed = 0"))
    with op.batch_alter_table("sources") as batch:
        batch.alter_column(
            "connector_type",
            existing_type=old_connector,
            type_=new_connector,
            existing_nullable=False,
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT 1 FROM sources WHERE connector_type = 'jellyfin' LIMIT 1")
    ).first():
        raise RuntimeError("Cannot downgrade while Jellyfin sources exist.")
    with op.batch_alter_table("sources") as batch:
        batch.alter_column(
            "connector_type",
            existing_type=new_connector,
            type_=old_connector,
            existing_nullable=False,
        )
