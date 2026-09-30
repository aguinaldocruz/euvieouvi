"""SQLAlchemy 2.x models for the approved persistence schema."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from euvieouvi.database.enums import (
    ConnectorType,
    LibraryMediaType,
    MediaKind,
    SyncStatus,
    SyncTrigger,
)
from euvieouvi.extensions import Base


def utc_now() -> datetime:
    """Return an aware UTC timestamp for Python-side defaults."""
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Source(TimestampMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("name", name="uq_sources_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    connector_type: Mapped[ConnectorType] = mapped_column(
        Enum(
            ConnectorType,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [item.value for item in enum],
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_connection_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_connection_status: Mapped[str | None] = mapped_column(String(64))
    server_identifier: Mapped[str | None] = mapped_column(String(255))


class Library(TimestampMixin, Base):
    __tablename__ = "libraries"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_libraries_source_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[LibraryMediaType] = mapped_column(
        Enum(
            LibraryMediaType,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [item.value for item in enum],
        ),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MediaItem(TimestampMixin, Base):
    __tablename__ = "media_items"
    __table_args__ = (
        CheckConstraint("year IS NULL OR year >= 0", name="ck_media_items_year_nonnegative"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0", name="ck_media_items_duration_nonnegative"
        ),
        Index("ix_media_items_kind_title", "kind", "title"),
        Index("ix_media_items_year", "year"),
        Index("ix_media_items_source_added", "source_added_at"),
        Index("ix_media_items_updated", "updated_at"),
        Index("ix_media_items_hierarchy", "parent_id", "season_number", "episode_number"),
        Index("ix_media_items_music_hierarchy", "parent_id", "disc_number", "track_number"),
        Index(
            "ix_media_items_enrichment_missing",
            "kind",
            "id",
            sqlite_where=text(
                "summary IS NULL OR tagline IS NULL OR studio IS NULL OR audience_rating IS NULL"
            ),
            postgresql_where=text(
                "summary IS NULL OR tagline IS NULL OR studio IS NULL OR audience_rating IS NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[MediaKind] = mapped_column(
        Enum(
            MediaKind,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [item.value for item in enum],
        ),
        nullable=False,
    )
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_title: Mapped[str | None] = mapped_column(String(500))
    sort_title: Mapped[str | None] = mapped_column(String(500))
    manual_match_tmdb_id: Mapped[str | None] = mapped_column(String(32))
    year: Mapped[int | None] = mapped_column(Integer)
    season_number: Mapped[int | None] = mapped_column(Integer)
    episode_number: Mapped[int | None] = mapped_column(Integer)
    disc_number: Mapped[int | None] = mapped_column(Integer)
    track_number: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    originally_available_on: Mapped[date | None] = mapped_column(Date)
    summary: Mapped[str | None] = mapped_column(Text)
    tagline: Mapped[str | None] = mapped_column(Text)
    studio: Mapped[str | None] = mapped_column(String(255))
    content_rating: Mapped[str | None] = mapped_column(String(64))
    audience_rating: Mapped[float | None] = mapped_column(Float)
    source_added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CatalogEntry(Base):
    """Persistent read model for one canonical card in the catalog."""

    __tablename__ = "catalog_entries"
    __table_args__ = (
        CheckConstraint("completion_count >= 0", name="ck_catalog_entries_completion_count"),
        CheckConstraint("child_count >= 0", name="ck_catalog_entries_child_count"),
        CheckConstraint(
            "completed_child_count >= 0 AND completed_child_count <= child_count",
            name="ck_catalog_entries_completed_children",
        ),
        Index("ix_catalog_entries_kind_last_played", "kind", "last_played_at"),
        Index("ix_catalog_entries_kind_first_played", "kind", "first_played_at"),
        Index("ix_catalog_entries_kind_play_count", "kind", "completion_count"),
        Index("ix_catalog_entries_kind_availability", "kind", "available"),
    )

    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), primary_key=True
    )
    group_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    completion_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    child_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_child_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    available_plex: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    available_jellyfin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class CatalogEntryMember(Base):
    """Map every source-specific media row to its canonical catalog card."""

    __tablename__ = "catalog_entry_members"
    __table_args__ = (
        Index("ix_catalog_entry_members_catalog", "catalog_media_item_id", "media_item_id"),
    )

    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), primary_key=True
    )
    catalog_media_item_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_entries.media_item_id", ondelete="CASCADE"), nullable=False
    )


class CatalogProjectionDirtyItem(Base):
    """Playback item awaiting an incremental catalog projection refresh."""

    __tablename__ = "catalog_projection_dirty_items"

    media_item_id: Mapped[int] = mapped_column(primary_key=True)


class Genre(Base):
    __tablename__ = "genres"
    __table_args__ = (UniqueConstraint("normalized_name", name="uq_genres_normalized_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)


class MediaGenre(Base):
    __tablename__ = "media_genres"
    __table_args__ = (
        UniqueConstraint("media_item_id", "genre_id", name="uq_media_genres_identity"),
        Index("ix_media_genres_genre_media", "genre_id", "media_item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False
    )
    genre_id: Mapped[int] = mapped_column(
        ForeignKey("genres.id", ondelete="CASCADE"), nullable=False
    )


class SourceMediaRef(TimestampMixin, Base):
    __tablename__ = "source_media_refs"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_source_media_refs_identity"),
        Index("ix_source_media_refs_library_available", "library_id", "available"),
        Index("ix_source_media_refs_media_item", "media_item_id"),
        Index("ix_source_media_refs_unavailable_since", "unavailable_since"),
        Index("ix_source_media_refs_updated", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    library_id: Mapped[int] = mapped_column(
        ForeignKey("libraries.id", ondelete="RESTRICT"), nullable=False
    )
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="RESTRICT"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_key: Mapped[str | None] = mapped_column(String(2048))
    external_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    unavailable_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_hash: Mapped[str | None] = mapped_column(String(128))


class MediaFilePath(Base):
    """Exact, root-mapped physical evidence, never a basename/title guess."""

    __tablename__ = "media_file_paths"
    __table_args__ = (
        UniqueConstraint("reference_id", "path_key", name="uq_media_file_paths_reference_key"),
        Index("ix_media_file_paths_key", "path_key"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    reference_id: Mapped[int] = mapped_column(
        ForeignKey("source_media_refs.id", ondelete="CASCADE"), nullable=False
    )
    path_key: Mapped[str] = mapped_column(String(64), nullable=False)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_path: Mapped[str] = mapped_column(Text, nullable=False)


class MediaIdentifier(Base):
    __tablename__ = "media_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "media_item_id", "provider", "external_id", name="uq_media_identifiers_identity"
        ),
        Index("ix_media_identifiers_provider_external", "provider", "external_id"),
        Index("ix_media_identifiers_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class MediaImage(TimestampMixin, Base):
    __tablename__ = "media_images"
    __table_args__ = (
        UniqueConstraint("media_item_id", "image_type", name="uq_media_images_item_type"),
        Index("ix_media_images_cache_status", "cache_status"),
        Index("ix_media_images_type_item", "image_type", "media_item_id"),
        Index("ix_media_images_updated", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    image_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="plex", nullable=False)
    source_path: Mapped[str | None] = mapped_column(String(2048))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    local_filename: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    cache_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    cached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EnrichmentRecord(TimestampMixin, Base):
    __tablename__ = "enrichment_records"
    __table_args__ = (
        UniqueConstraint("media_item_id", "provider", name="uq_enrichment_records_item_provider"),
        Index("ix_enrichment_records_status_checked", "status", "checked_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message: Mapped[str | None] = mapped_column(String(500))


class WatchEvent(TimestampMixin, Base):
    __tablename__ = "watch_events"
    __table_args__ = (
        CheckConstraint("progress_ms IS NULL OR progress_ms >= 0", name="ck_watch_events_progress"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_watch_events_duration"),
        CheckConstraint(
            "view_number IS NULL OR view_number > 0", name="ck_watch_events_view_number"
        ),
        UniqueConstraint("source_id", "source_event_id", name="uq_watch_events_source_event"),
        UniqueConstraint("source_id", "dedup_key", name="uq_watch_events_dedup"),
        Index(
            "ix_watch_events_media_completed_watched",
            "media_item_id",
            "completed",
            "watched_at",
        ),
        Index("ix_watch_events_media_watched", "media_item_id", "watched_at"),
        Index("ix_watch_events_source_watched", "source_id", "watched_at"),
        Index("ix_watch_events_completed_id", "completed", "id"),
        Index("ix_watch_events_watched_id", "watched_at", "id"),
        Index("ix_watch_events_updated", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    source_event_id: Mapped[str | None] = mapped_column(String(255))
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    watched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    progress_ms: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    view_number: Mapped[int | None] = mapped_column(Integer)
    playback_user: Mapped[str | None] = mapped_column(String(255))
    origin: Mapped[str] = mapped_column(String(32), default="synchronization", nullable=False)


class WebhookEvent(TimestampMixin, Base):
    """Recent webhook playback activity, including items not synchronized yet."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        Index("ix_webhook_events_completed_occurred", "completed", "occurred_at"),
        Index("ix_webhook_events_active_source", "active", "source_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_key: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    series_title: Mapped[str | None] = mapped_column(String(500))
    playback_user: Mapped[str | None] = mapped_column(String(255))
    media_kind: Mapped[str | None] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    progress_percent: Mapped[int | None] = mapped_column(Integer)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WatchState(TimestampMixin, Base):
    __tablename__ = "watch_states"
    __table_args__ = (
        CheckConstraint("view_count >= 0", name="ck_watch_states_view_count"),
        CheckConstraint("progress_ms IS NULL OR progress_ms >= 0", name="ck_watch_states_progress"),
        UniqueConstraint("media_item_id", "source_id", name="uq_watch_states_item_source"),
        Index("ix_watch_states_completed_watched", "completed", "last_watched_at"),
        Index("ix_watch_states_updated", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_watched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    progress_ms: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SyncRun(TimestampMixin, Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint(
            "items_read >= 0 AND items_inserted >= 0 AND items_updated >= 0 AND "
            "items_unchanged >= 0 AND items_failed >= 0 AND events_inserted >= 0",
            name="ck_sync_runs_counters",
        ),
        CheckConstraint(
            "watch_sync_scanned >= 0 AND watch_sync_updated >= 0 AND "
            "watch_sync_skipped >= 0 AND watch_sync_failed >= 0",
            name="ck_sync_runs_watch_sync_counters",
        ),
        CheckConstraint(
            "status != 'succeeded' OR finished_at IS NOT NULL",
            name="ck_sync_runs_succeeded_finished",
        ),
        Index("ix_sync_runs_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    trigger: Mapped[SyncTrigger] = mapped_column(
        Enum(
            SyncTrigger,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [item.value for item in enum],
        ),
        nullable=False,
    )
    status: Mapped[SyncStatus] = mapped_column(
        Enum(
            SyncStatus,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [item.value for item in enum],
        ),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_unchanged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    events_inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    watch_sync_status: Mapped[SyncStatus | None] = mapped_column(
        Enum(
            SyncStatus,
            native_enum=False,
            create_constraint=False,
            length=16,
            values_callable=lambda enum: [item.value for item in enum],
        )
    )
    watch_sync_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watch_sync_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watch_sync_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    watch_sync_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    watch_sync_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    watch_sync_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    watch_sync_summary: Mapped[str | None] = mapped_column(Text)


class JobRun(TimestampMixin, Base):
    """Persistent execution and log metadata for one operational job."""

    __tablename__ = "job_runs"
    __table_args__ = (
        Index("ix_job_runs_job_created", "job_id", "created_at"),
        Index("ix_job_runs_status_created", "status", "created_at"),
        Index("ix_job_runs_queue", "status", "available_at", "id"),
        Index("ix_job_runs_lease", "status", "lease_expires_at"),
        Index(
            "uq_job_runs_active_job",
            "job_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[SyncTrigger] = mapped_column(
        Enum(
            SyncTrigger,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [item.value for item in enum],
        ),
        nullable=False,
    )
    status: Mapped[SyncStatus] = mapped_column(
        Enum(
            SyncStatus,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [item.value for item in enum],
        ),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(96))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    execution_reference: Mapped[str | None] = mapped_column(String(96))
    progress_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    log_filename: Mapped[str | None] = mapped_column(String(255))


class AsyncTask(TimestampMixin, Base):
    """Durable idempotent work triggered by webhooks or other instant events."""

    __tablename__ = "async_tasks"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_async_tasks_dedup"),
        Index("ix_async_tasks_due", "status", "next_attempt_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    pending_payload: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(1000))


class OperationalSample(Base):
    """Bounded historical sample of queues, worker and connector health."""

    __tablename__ = "operational_samples"
    __table_args__ = (
        CheckConstraint(
            "queued_jobs >= 0 AND pending_tasks >= 0 AND open_circuits >= 0 AND "
            "connector_failures >= 0 AND slow_operations >= 0 AND alert_count >= 0",
            name="ck_operational_samples_nonnegative",
        ),
        Index("ix_operational_samples_captured", "captured_at"),
        Index("ix_operational_samples_level_captured", "alert_level", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    queued_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_tasks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    heartbeat_age_seconds: Mapped[int | None] = mapped_column(Integer)
    open_circuits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    connector_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    average_connector_latency_ms: Mapped[float | None] = mapped_column(Float)
    slow_operations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    database_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wal_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    alert_level: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    alert_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    alert_summary: Mapped[str | None] = mapped_column(Text)


class SyncRunLibrary(Base):
    __tablename__ = "sync_run_libraries"
    __table_args__ = (
        CheckConstraint(
            "items_read >= 0 AND items_inserted >= 0 AND items_updated >= 0 AND items_failed >= 0",
            name="ck_sync_run_libraries_counters",
        ),
        CheckConstraint(
            "items_scanned >= 0 AND (items_total IS NULL OR items_total >= 0)",
            name="ck_sync_run_libraries_progress",
        ),
        CheckConstraint(
            "status != 'succeeded' OR finished_at IS NOT NULL",
            name="ck_sync_run_libraries_succeeded_finished",
        ),
        UniqueConstraint("sync_run_id", "library_id", name="uq_sync_run_libraries_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False
    )
    library_id: Mapped[int] = mapped_column(
        ForeignKey("libraries.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[SyncStatus] = mapped_column(
        Enum(
            SyncStatus,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda enum: [item.value for item in enum],
        ),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_total: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)

    @property
    def progress_percent(self) -> int | None:
        if self.items_total is None or self.items_total <= 0:
            return None
        return min(100, self.items_scanned * 100 // self.items_total)


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(
        ForeignKey("libraries.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor: Mapped[str | None] = mapped_column(Text)
    watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_external_id: Mapped[str | None] = mapped_column(String(255))
    last_successful_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SyncError(Base):
    __tablename__ = "sync_errors"
    __table_args__ = (Index("ix_sync_errors_run_occurred", "sync_run_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False
    )
    library_id: Mapped[int | None] = mapped_column(ForeignKey("libraries.id", ondelete="SET NULL"))
    media_external_id: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
