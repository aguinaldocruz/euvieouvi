"""Create the approved initial schema.

Revision ID: 20260804_0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

connector_type = sa.Enum("plex", name="connectortype", native_enum=False, create_constraint=True)
library_media_type = sa.Enum(
    "movie", "show", name="librarymediatype", native_enum=False, create_constraint=True
)
media_kind = sa.Enum(
    "movie",
    "show",
    "season",
    "episode",
    name="mediakind",
    native_enum=False,
    create_constraint=True,
)
sync_trigger = sa.Enum(
    "manual", "api", name="synctrigger", native_enum=False, create_constraint=True
)
sync_status = sa.Enum(
    "queued",
    "running",
    "succeeded",
    "failed",
    "interrupted",
    name="syncstatus",
    native_enum=False,
    create_constraint=True,
)


def timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("connector_type", connector_type, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_connection_test_at", sa.DateTime(timezone=True)),
        sa.Column("last_connection_status", sa.String(64)),
        *timestamps(),
        sa.UniqueConstraint("name", name="uq_sources_name"),
    )
    op.create_table(
        "libraries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("media_type", library_media_type, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("source_id", "external_id", name="uq_libraries_source_external"),
    )
    op.create_table(
        "media_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", media_kind, nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("media_items.id", ondelete="RESTRICT")),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("original_title", sa.String(500)),
        sa.Column("sort_title", sa.String(500)),
        sa.Column("year", sa.Integer()),
        sa.Column("season_number", sa.Integer()),
        sa.Column("episode_number", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("originally_available_on", sa.Date()),
        sa.Column("summary", sa.Text()),
        *timestamps(),
        sa.CheckConstraint("year IS NULL OR year >= 0", name="ck_media_items_year_nonnegative"),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0", name="ck_media_items_duration_nonnegative"
        ),
    )
    op.create_index("ix_media_items_kind_title", "media_items", ["kind", "title"])
    op.create_index(
        "ix_media_items_hierarchy", "media_items", ["parent_id", "season_number", "episode_number"]
    )
    op.create_table(
        "source_media_refs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "library_id",
            sa.Integer(),
            sa.ForeignKey("libraries.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("external_key", sa.String(2048)),
        sa.Column("external_updated_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("raw_hash", sa.String(128)),
        *timestamps(),
        sa.UniqueConstraint("source_id", "external_id", name="uq_source_media_refs_identity"),
    )
    op.create_index(
        "ix_source_media_refs_library_available", "source_media_refs", ["library_id", "available"]
    )
    op.create_index("ix_source_media_refs_media_item", "source_media_refs", ["media_item_id"])
    op.create_table(
        "media_identifiers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "media_item_id", "provider", "external_id", name="uq_media_identifiers_identity"
        ),
    )
    op.create_index(
        "ix_media_identifiers_provider_external", "media_identifiers", ["provider", "external_id"]
    )
    op.create_table(
        "watch_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_event_id", sa.String(255)),
        sa.Column("dedup_key", sa.String(128), nullable=False),
        sa.Column("watched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("progress_ms", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("view_number", sa.Integer()),
        *timestamps(),
        sa.CheckConstraint(
            "progress_ms IS NULL OR progress_ms >= 0", name="ck_watch_events_progress"
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0", name="ck_watch_events_duration"
        ),
        sa.CheckConstraint(
            "view_number IS NULL OR view_number > 0", name="ck_watch_events_view_number"
        ),
        sa.UniqueConstraint("source_id", "source_event_id", name="uq_watch_events_source_event"),
        sa.UniqueConstraint("source_id", "dedup_key", name="uq_watch_events_dedup"),
    )
    op.create_index(
        "ix_watch_events_media_watched", "watch_events", ["media_item_id", "watched_at"]
    )
    op.create_index("ix_watch_events_source_watched", "watch_events", ["source_id", "watched_at"])
    op.create_table(
        "watch_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("view_count", sa.Integer(), nullable=False),
        sa.Column("last_watched_at", sa.DateTime(timezone=True)),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("progress_ms", sa.Integer()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        *timestamps(),
        sa.CheckConstraint("view_count >= 0", name="ck_watch_states_view_count"),
        sa.CheckConstraint(
            "progress_ms IS NULL OR progress_ms >= 0", name="ck_watch_states_progress"
        ),
        sa.UniqueConstraint("media_item_id", "source_id", name="uq_watch_states_item_source"),
    )
    op.create_index(
        "ix_watch_states_completed_watched", "watch_states", ["completed", "last_watched_at"]
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("trigger", sync_trigger, nullable=False),
        sa.Column("status", sync_status, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("items_read", sa.Integer(), nullable=False),
        sa.Column("items_inserted", sa.Integer(), nullable=False),
        sa.Column("items_updated", sa.Integer(), nullable=False),
        sa.Column("items_unchanged", sa.Integer(), nullable=False),
        sa.Column("items_failed", sa.Integer(), nullable=False),
        sa.Column("events_inserted", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text()),
        *timestamps(),
        sa.CheckConstraint(
            "items_read >= 0 AND items_inserted >= 0 AND items_updated >= 0 AND "
            "items_unchanged >= 0 AND items_failed >= 0 AND events_inserted >= 0",
            name="ck_sync_runs_counters",
        ),
        sa.CheckConstraint(
            "status != 'succeeded' OR finished_at IS NOT NULL",
            name="ck_sync_runs_succeeded_finished",
        ),
    )
    op.create_index("ix_sync_runs_status_created", "sync_runs", ["status", "created_at"])
    op.create_table(
        "sync_run_libraries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sync_run_id",
            sa.Integer(),
            sa.ForeignKey("sync_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "library_id",
            sa.Integer(),
            sa.ForeignKey("libraries.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sync_status, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("items_read", sa.Integer(), nullable=False),
        sa.Column("items_inserted", sa.Integer(), nullable=False),
        sa.Column("items_updated", sa.Integer(), nullable=False),
        sa.Column("items_failed", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text()),
        sa.CheckConstraint(
            "items_read >= 0 AND items_inserted >= 0 AND items_updated >= 0 AND items_failed >= 0",
            name="ck_sync_run_libraries_counters",
        ),
        sa.CheckConstraint(
            "status != 'succeeded' OR finished_at IS NOT NULL",
            name="ck_sync_run_libraries_succeeded_finished",
        ),
        sa.UniqueConstraint("sync_run_id", "library_id", name="uq_sync_run_libraries_identity"),
    )
    op.create_table(
        "sync_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "library_id",
            sa.Integer(),
            sa.ForeignKey("libraries.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("cursor", sa.Text()),
        sa.Column("watermark_at", sa.DateTime(timezone=True)),
        sa.Column("last_external_id", sa.String(255)),
        sa.Column(
            "last_successful_run_id",
            sa.Integer(),
            sa.ForeignKey("sync_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "sync_errors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sync_run_id",
            sa.Integer(),
            sa.ForeignKey("sync_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("libraries.id", ondelete="SET NULL")),
        sa.Column("media_external_id", sa.String(255)),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sync_errors_run_occurred", "sync_errors", ["sync_run_id", "occurred_at"])
    op.create_table(
        "settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table_name in (
        "settings",
        "sync_errors",
        "sync_checkpoints",
        "sync_run_libraries",
        "sync_runs",
        "watch_states",
        "watch_events",
        "media_identifiers",
        "source_media_refs",
        "media_items",
        "libraries",
        "sources",
    ):
        op.drop_table(table_name)
