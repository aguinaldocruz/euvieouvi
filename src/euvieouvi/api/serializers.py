"""Safe JSON serializers for persistent resources."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from euvieouvi.database.models import Library, MediaItem, Source, SyncRun, WatchEvent, WatchState


def timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def source(value: Source) -> dict[str, Any]:
    return {
        "id": value.id,
        "connector_type": value.connector_type.value,
        "name": value.name,
        "base_url": value.base_url,
        "enabled": value.enabled,
        "has_secret": bool(value.secret),
        "last_connection_test_at": timestamp(value.last_connection_test_at),
        "last_connection_status": value.last_connection_status,
        "created_at": timestamp(value.created_at),
        "updated_at": timestamp(value.updated_at),
    }


def library(value: Library) -> dict[str, Any]:
    return {
        "id": value.id,
        "source_id": value.source_id,
        "external_id": value.external_id,
        "name": value.name,
        "media_type": value.media_type.value,
        "enabled": value.enabled,
        "available": value.available,
        "discovered_at": timestamp(value.discovered_at),
        "last_seen_at": timestamp(value.last_seen_at),
    }


def media(
    value: MediaItem, *, watched: WatchState | None = None, available: bool | None = None
) -> dict[str, Any]:
    return {
        "id": value.id,
        "kind": value.kind.value,
        "parent_id": value.parent_id,
        "title": value.title,
        "original_title": value.original_title,
        "sort_title": value.sort_title,
        "year": value.year,
        "season_number": value.season_number,
        "episode_number": value.episode_number,
        "duration_ms": value.duration_ms,
        "originally_available_on": scalar(value.originally_available_on),
        "summary": value.summary,
        "available": available,
        "watch_state": watch_state(watched) if watched else None,
        "created_at": timestamp(value.created_at),
        "updated_at": timestamp(value.updated_at),
    }


def watch_event(value: WatchEvent) -> dict[str, Any]:
    return {
        "id": value.id,
        "media_id": value.media_item_id,
        "source_id": value.source_id,
        "watched_at": timestamp(value.watched_at),
        "completed": value.completed,
        "progress_ms": value.progress_ms,
        "duration_ms": value.duration_ms,
        "view_number": value.view_number,
    }


def watch_state(value: WatchState) -> dict[str, Any]:
    return {
        "id": value.id,
        "media_id": value.media_item_id,
        "source_id": value.source_id,
        "view_count": value.view_count,
        "last_watched_at": timestamp(value.last_watched_at),
        "completed": value.completed,
        "progress_ms": value.progress_ms,
        "observed_at": timestamp(value.observed_at),
    }


def sync_run(value: SyncRun) -> dict[str, Any]:
    return {
        "id": value.id,
        "source_id": value.source_id,
        "trigger": value.trigger.value,
        "status": value.status.value,
        "started_at": timestamp(value.started_at),
        "finished_at": timestamp(value.finished_at),
        "heartbeat_at": timestamp(value.heartbeat_at),
        "items_read": value.items_read,
        "items_inserted": value.items_inserted,
        "items_updated": value.items_updated,
        "items_unchanged": value.items_unchanged,
        "items_failed": value.items_failed,
        "events_inserted": value.events_inserted,
        "summary": value.summary,
        "created_at": timestamp(value.created_at),
        "updated_at": timestamp(value.updated_at),
    }


def scalar(value: object) -> object:
    if isinstance(value, datetime):
        return timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value
