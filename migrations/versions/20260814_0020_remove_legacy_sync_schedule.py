"""Move legacy synchronization schedules to independent jobs.

Revision ID: 20260814_0020
Revises: 20260814_0019
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260814_0020"
down_revision: str | None = "20260814_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _copy_if_missing(target: str, *sources: str) -> None:
    source_list = ", ".join(f"'{source}'" for source in sources)
    op.execute(
        "INSERT INTO settings (key, value, updated_at) "
        f"SELECT '{target}', value, CURRENT_TIMESTAMP FROM settings "
        f"WHERE key IN ({source_list}) "
        f"ORDER BY CASE key "
        + " ".join(f"WHEN '{source}' THEN {index}" for index, source in enumerate(sources))
        + " END LIMIT 1 "
        "ON CONFLICT(key) DO NOTHING"
    )


def upgrade() -> None:
    _copy_if_missing(
        "jobs.sync_plex.enabled", "sync.schedule.enabled.plex", "sync.schedule.enabled"
    )
    _copy_if_missing("jobs.sync_plex.time", "sync.schedule.time.plex", "sync.schedule.time")
    _copy_if_missing(
        "jobs.sync_plex.last_date", "sync.schedule.last_date.plex", "sync.schedule.last_date"
    )
    _copy_if_missing("jobs.sync_jellyfin.enabled", "sync.schedule.enabled.jellyfin")
    _copy_if_missing("jobs.sync_jellyfin.time", "sync.schedule.time.jellyfin", "sync.schedule.time")
    _copy_if_missing(
        "jobs.sync_jellyfin.last_date",
        "sync.schedule.last_date.jellyfin",
        "sync.schedule.last_date",
    )
    op.execute("DELETE FROM settings WHERE key LIKE 'sync.schedule.%'")


def downgrade() -> None:
    pass
