"""Presentation-only formatting in configured local time."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from flask import current_app


def local_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone(ZoneInfo(current_app.config["TIMEZONE"]))
    return local.strftime("%d/%m/%Y %H:%M")


def duration_ms(value: int | None) -> str:
    if value is None:
        return "—"
    minutes = value // 60_000
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}min" if hours else f"{minutes} min"


def elapsed_time(started_at: datetime | None, finished_at: datetime | None = None) -> str:
    """Format a job runtime using compact minutes and seconds."""
    if started_at is None:
        return "—"
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    end = finished_at or datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    seconds = max(0, int((end - started_at).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s"
