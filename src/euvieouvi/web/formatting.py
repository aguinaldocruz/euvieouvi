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
