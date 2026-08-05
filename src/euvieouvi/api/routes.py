"""Thin HTTP routes for the approved v1 API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from euvieouvi.api.runtime import connector_for, get_executor
from euvieouvi.api.serializers import (
    library,
    media,
    source,
    sync_run,
    timestamp,
    watch_event,
    watch_state,
)
from euvieouvi.api.validation import (
    bool_query,
    boolean,
    decode_cursor,
    encode_cursor,
    fingerprint,
    http_url,
    integer,
    json_object,
    limit,
    query_args,
    string,
    validation_error,
)
from euvieouvi.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorConnectionError,
    ConnectorError,
    ConnectorResponseError,
    ConnectorTimeoutError,
)
from euvieouvi.database.enums import (
    ConnectorType,
    LibraryMediaType,
    MediaKind,
    SyncStatus,
    SyncTrigger,
)
from euvieouvi.database.models import (
    Library,
    MediaIdentifier,
    MediaItem,
    Source,
    SourceMediaRef,
    SyncCheckpoint,
    SyncError,
    SyncRun,
    SyncRunLibrary,
    WatchEvent,
    WatchState,
)
from euvieouvi.errors import AppError
from euvieouvi.extensions import db
from euvieouvi.sync.discovery import LibraryDiscoveryService
from euvieouvi.sync.errors import SyncAlreadyRunningError, SyncSourceUnavailableError

blueprint = Blueprint("api_v1", __name__, url_prefix="/api/v1")


def _not_found(resource: str) -> AppError:
    return AppError("not_found", f"{resource} was not found.", 404)


def _collection(items: list[dict[str, Any]], size: int, requested: int, fp: str) -> dict[str, Any]:
    has_more = len(items) > requested
    visible = items[:requested]
    next_cursor = encode_cursor(int(visible[-1]["id"]), fp) if has_more and visible else None
    return {
        "items": visible,
        "pagination": {"limit": requested, "next_cursor": next_cursor, "has_more": has_more},
    }


@blueprint.get("/sources")
def sources_list() -> Response:
    values = db.session.scalars(select(Source).order_by(Source.id)).all()
    return jsonify([source(item) for item in values])


@blueprint.post("/sources")
def sources_create() -> tuple[Response, int]:
    data = json_object(
        request,
        allowed={"connector_type", "name", "base_url", "secret", "enabled"},
        required={"connector_type", "name", "base_url", "secret"},
    )
    if data["connector_type"] != "plex":
        raise validation_error("connector_type", "unsupported_value", "Only plex is supported.")
    entity = Source(
        connector_type=ConnectorType.PLEX,
        name=string(data["name"], "name", maximum=255),
        base_url=http_url(data["base_url"]),
        secret=string(data["secret"], "secret", maximum=4096),
        enabled=boolean(data.get("enabled", True), "enabled"),
    )
    db.session.add(entity)
    try:
        db.session.commit()
    except IntegrityError as error:
        db.session.rollback()
        raise AppError(
            "source_name_conflict", "A source with this name already exists.", 409
        ) from error
    response = jsonify(source(entity))
    response.headers["Location"] = f"/api/v1/sources/{entity.id}"
    return response, 201


@blueprint.get("/sources/<int:source_id>")
def sources_get(source_id: int) -> Response:
    entity = db.session.get(Source, source_id)
    if entity is None:
        raise _not_found("Source")
    return jsonify(source(entity))


@blueprint.patch("/sources/<int:source_id>")
def sources_patch(source_id: int) -> Response:
    entity = db.session.get(Source, source_id)
    if entity is None:
        raise _not_found("Source")
    data = json_object(request, allowed={"name", "base_url", "secret", "enabled"})
    if not data:
        raise validation_error("body", "empty", "At least one field is required.")
    if "name" in data:
        entity.name = string(data["name"], "name", maximum=255)
    if "base_url" in data:
        entity.base_url = http_url(data["base_url"])
        entity.last_connection_status = None
    if "secret" in data:
        if data["secret"] is None:
            raise validation_error("secret", "null_not_allowed", "Secret cannot be null.")
        entity.secret = string(data["secret"], "secret", maximum=4096)
        entity.last_connection_status = None
    if "enabled" in data:
        entity.enabled = boolean(data["enabled"], "enabled")
    try:
        db.session.commit()
    except IntegrityError as error:
        db.session.rollback()
        raise AppError(
            "source_name_conflict", "A source with this name already exists.", 409
        ) from error
    return jsonify(source(entity))


@blueprint.post("/sources/<int:source_id>/connection-test")
def source_connection_test(source_id: int) -> Response:
    entity = db.session.get(Source, source_id)
    if entity is None:
        raise _not_found("Source")
    try:
        info = connector_for(entity).test_connection()
    except ConnectorError as error:
        raise _connector_error(error) from error
    entity.last_connection_test_at = datetime.now().astimezone()
    entity.last_connection_status = "succeeded"
    db.session.commit()
    return jsonify(
        {
            "source_id": source_id,
            "status": "succeeded",
            "server_name": info.server_name,
            "server_identifier": info.server_identifier,
            "server_version": info.server_version,
            "capabilities": list(info.capabilities),
        }
    )


@blueprint.post("/sources/<int:source_id>/library-discoveries")
def library_discovery(source_id: int) -> Response:
    entity = db.session.get(Source, source_id)
    if entity is None:
        raise _not_found("Source")
    connector = connector_for(entity)
    try:
        count = LibraryDiscoveryService(lambda: db.session(), connector).discover(source_id)
    except SyncSourceUnavailableError as error:
        raise AppError("source_unavailable", str(error), 409) from error
    except ConnectorError as error:
        raise _connector_error(error) from error
    values = db.session.scalars(
        select(Library).where(Library.source_id == source_id).order_by(Library.name, Library.id)
    ).all()
    unsupported = len(getattr(connector, "last_unsupported_libraries", ()))
    return jsonify(
        {
            "source_id": source_id,
            "discovered": count + unsupported,
            "supported": count,
            "unsupported": unsupported,
            "libraries": [library(item) for item in values],
        }
    )


@blueprint.get("/libraries")
def libraries_list() -> Response:
    args = query_args(request, {"source_id", "media_type", "enabled", "available"})
    statement = select(Library)
    if "source_id" in args:
        statement = statement.where(Library.source_id == _query_int(args["source_id"], "source_id"))
    if "media_type" in args:
        try:
            kind = LibraryMediaType(args["media_type"])
        except ValueError as error:
            raise validation_error("media_type", "invalid_value", "Use movie or show.") from error
        statement = statement.where(Library.media_type == kind)
    for field in ("enabled", "available"):
        value = bool_query(args.get(field), field)
        if value is not None:
            statement = statement.where(getattr(Library, field) == value)
    return jsonify(
        [
            library(item)
            for item in db.session.scalars(statement.order_by(Library.name, Library.id)).all()
        ]
    )


@blueprint.get("/libraries/<int:library_id>")
def libraries_get(library_id: int) -> Response:
    entity = db.session.get(Library, library_id)
    if entity is None:
        raise _not_found("Library")
    return jsonify(library(entity))


@blueprint.patch("/libraries/<int:library_id>")
def libraries_patch(library_id: int) -> Response:
    entity = db.session.get(Library, library_id)
    if entity is None:
        raise _not_found("Library")
    data = json_object(request, allowed={"enabled"}, required={"enabled"})
    enabled = boolean(data["enabled"], "enabled")
    if enabled and not entity.available:
        raise AppError("library_unavailable", "An unavailable library cannot be enabled.", 409)
    entity.enabled = enabled
    db.session.commit()
    return jsonify(library(entity))


@blueprint.post("/sync-runs")
def sync_runs_create() -> tuple[Response, int]:
    data = json_object(request, allowed={"source_id"}, required={"source_id"})
    source_id = integer(data["source_id"], "source_id", minimum=1)
    entity = db.session.get(Source, source_id)
    if entity is None:
        raise _not_found("Source")
    if not entity.enabled:
        raise AppError("source_unavailable", "Source is disabled.", 409)
    if (
        db.session.scalar(
            select(func.count())
            .select_from(Library)
            .where(
                Library.source_id == source_id,
                Library.enabled.is_(True),
                Library.available.is_(True),
            )
        )
        == 0
    ):
        raise AppError("no_enabled_libraries", "The source has no enabled available library.", 409)
    try:
        run_id = get_executor(current_app).submit(source_id, trigger=SyncTrigger.API)
    except SyncAlreadyRunningError as error:
        active = db.session.scalar(
            select(SyncRun).where(SyncRun.status == SyncStatus.RUNNING).order_by(SyncRun.id.desc())
        )
        details = [{"active_sync_run_id": active.id}] if active else []
        raise AppError("sync_already_running", str(error), 409, details) from error
    except SyncSourceUnavailableError as error:
        raise AppError("source_unavailable", str(error), 409) from error
    run = db.session.get(SyncRun, run_id)
    if run is None:
        raise RuntimeError("Started synchronization was not persisted.")
    accepted = sync_run(run)
    accepted["status"] = "queued"
    accepted["started_at"] = None
    accepted["heartbeat_at"] = None
    response = jsonify(accepted)
    response.headers["Location"] = f"/api/v1/sync-runs/{run.id}"
    return response, 202


@blueprint.get("/sync-runs")
def sync_runs_list() -> Response:
    args = query_args(
        request, {"source_id", "status", "created_from", "created_to", "limit", "cursor"}
    )
    size = limit(args.get("limit"))
    fp = fingerprint({k: v for k, v in args.items() if k not in {"limit", "cursor"}})
    last = decode_cursor(args.get("cursor"), fp)
    statement = select(SyncRun)
    if "source_id" in args:
        statement = statement.where(SyncRun.source_id == _query_int(args["source_id"], "source_id"))
    if "status" in args:
        try:
            status = SyncStatus(args["status"])
        except ValueError as error:
            raise validation_error("status", "invalid_value", "Unknown sync status.") from error
        statement = statement.where(SyncRun.status == status)
    if last:
        statement = statement.where(SyncRun.id < last)
    values = db.session.scalars(
        statement.order_by(SyncRun.created_at.desc(), SyncRun.id.desc()).limit(size + 1)
    ).all()
    return jsonify(_collection([sync_run(item) for item in values], len(values), size, fp))


@blueprint.get("/sync-runs/active")
def sync_runs_active() -> Response:
    run = db.session.scalar(
        select(SyncRun)
        .where(SyncRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
        .order_by(SyncRun.id.desc())
    )
    return Response(status=204) if run is None else jsonify(sync_run(run))


@blueprint.get("/sync-runs/<int:run_id>")
def sync_runs_get(run_id: int) -> Response:
    run = db.session.get(SyncRun, run_id)
    if run is None:
        raise _not_found("Synchronization")
    result = sync_run(run)
    details = db.session.scalars(
        select(SyncRunLibrary)
        .where(SyncRunLibrary.sync_run_id == run_id)
        .order_by(SyncRunLibrary.id)
    ).all()
    errors = db.session.scalars(
        select(SyncError).where(SyncError.sync_run_id == run_id).order_by(SyncError.id).limit(100)
    ).all()
    result["libraries"] = [
        {
            "id": x.id,
            "library_id": x.library_id,
            "status": x.status.value,
            "started_at": timestamp(x.started_at),
            "finished_at": timestamp(x.finished_at),
            "items_read": x.items_read,
            "items_inserted": x.items_inserted,
            "items_updated": x.items_updated,
            "items_failed": x.items_failed,
            "message": x.message,
        }
        for x in details
    ]
    result["errors"] = [
        {
            "id": x.id,
            "library_id": x.library_id,
            "media_external_id": x.media_external_id,
            "category": x.category,
            "message": x.message,
            "retryable": x.retryable,
            "occurred_at": timestamp(x.occurred_at),
        }
        for x in errors
    ]
    result["checkpoints"] = [
        {
            "library_id": x.library_id,
            "strategy": x.strategy,
            "updated_at": timestamp(x.updated_at),
            "last_successful_run_id": x.last_successful_run_id,
        }
        for x in db.session.scalars(
            select(SyncCheckpoint)
            .join(SyncRunLibrary, SyncRunLibrary.library_id == SyncCheckpoint.library_id)
            .where(SyncRunLibrary.sync_run_id == run_id)
        ).all()
    ]
    return jsonify(result)


@blueprint.post("/sync-runs/<int:run_id>/cancellation")
def sync_runs_cancel(run_id: int) -> tuple[Response, int]:
    run = db.session.get(SyncRun, run_id)
    if run is None:
        raise _not_found("Synchronization")
    if run.status not in {SyncStatus.QUEUED, SyncStatus.RUNNING}:
        raise AppError("sync_not_active", "Synchronization has already finished.", 409)
    get_executor(current_app).cancel(run_id)
    return jsonify({"id": run_id, "status": "cancellation_requested"}), 202


@blueprint.get("/media")
def media_list() -> Response:
    args = query_args(
        request,
        {
            "kind",
            "library_id",
            "parent_id",
            "query",
            "year",
            "available",
            "watched",
            "watched_from",
            "watched_to",
            "sort",
            "order",
            "limit",
            "cursor",
        },
    )
    size = limit(args.get("limit"))
    fp = fingerprint({k: v for k, v in args.items() if k not in {"limit", "cursor"}})
    last = decode_cursor(args.get("cursor"), fp)
    statement = select(MediaItem)
    if "kind" in args:
        try:
            kind = MediaKind(args["kind"])
        except ValueError as error:
            raise validation_error("kind", "invalid_value", "Unknown media kind.") from error
        statement = statement.where(MediaItem.kind == kind)
    if "parent_id" in args:
        statement = statement.where(
            MediaItem.parent_id == _query_int(args["parent_id"], "parent_id")
        )
    if "year" in args:
        statement = statement.where(MediaItem.year == _query_int(args["year"], "year"))
    if "query" in args:
        query = string(args["query"], "query", maximum=200)
        statement = statement.where(MediaItem.title.ilike(f"%{query}%"))
    if "library_id" in args:
        statement = statement.join(SourceMediaRef).where(
            SourceMediaRef.library_id == _query_int(args["library_id"], "library_id")
        )
    available = bool_query(args.get("available"), "available")
    if available is not None:
        statement = statement.where(
            select(SourceMediaRef.id)
            .where(
                SourceMediaRef.media_item_id == MediaItem.id,
                SourceMediaRef.available == available,
            )
            .correlate(MediaItem)
            .exists()
        )
    watched = bool_query(args.get("watched"), "watched")
    if watched is not None:
        statement = statement.where(
            select(WatchState.id)
            .where(WatchState.media_item_id == MediaItem.id, WatchState.completed == watched)
            .correlate(MediaItem)
            .exists()
        )
    if last:
        statement = statement.where(MediaItem.id > last)
    values = db.session.scalars(statement.distinct().order_by(MediaItem.id).limit(size + 1)).all()
    return jsonify(_collection([_media_summary(item) for item in values], len(values), size, fp))


@blueprint.get("/media/<int:media_id>")
def media_get(media_id: int) -> Response:
    entity = db.session.get(MediaItem, media_id)
    if entity is None:
        raise _not_found("Media")
    result = _media_summary(entity)
    result["children"] = [
        {
            "id": x.id,
            "kind": x.kind.value,
            "title": x.title,
            "season_number": x.season_number,
            "episode_number": x.episode_number,
        }
        for x in db.session.scalars(
            select(MediaItem).where(MediaItem.parent_id == media_id).order_by(MediaItem.id)
        ).all()
    ]
    result["identifiers"] = [
        {"provider": x.provider, "external_id": x.external_id}
        for x in db.session.scalars(
            select(MediaIdentifier).where(MediaIdentifier.media_item_id == media_id)
        ).all()
    ]
    result["references"] = [
        {
            "source_id": x.source_id,
            "library_id": x.library_id,
            "external_id": x.external_id,
            "available": x.available,
            "last_seen_at": timestamp(x.last_seen_at),
        }
        for x in db.session.scalars(
            select(SourceMediaRef).where(SourceMediaRef.media_item_id == media_id)
        ).all()
    ]
    result["known_event_count"] = (
        db.session.scalar(
            select(func.count()).select_from(WatchEvent).where(WatchEvent.media_item_id == media_id)
        )
        or 0
    )
    return jsonify(result)


@blueprint.get("/watch-events")
def watch_events_list() -> Response:
    args = query_args(
        request,
        {
            "media_id",
            "source_id",
            "library_id",
            "kind",
            "watched_from",
            "watched_to",
            "completed",
            "limit",
            "cursor",
        },
    )
    size = limit(args.get("limit"))
    fp = fingerprint({k: v for k, v in args.items() if k not in {"limit", "cursor"}})
    last = decode_cursor(args.get("cursor"), fp)
    statement = select(WatchEvent)
    for field, column in (
        ("media_id", WatchEvent.media_item_id),
        ("source_id", WatchEvent.source_id),
    ):
        if field in args:
            statement = statement.where(column == _query_int(args[field], field))
    completed = bool_query(args.get("completed"), "completed")
    if completed is not None:
        statement = statement.where(WatchEvent.completed == completed)
    if "library_id" in args:
        statement = statement.join(
            SourceMediaRef, SourceMediaRef.media_item_id == WatchEvent.media_item_id
        ).where(SourceMediaRef.library_id == _query_int(args["library_id"], "library_id"))
    if "kind" in args:
        statement = statement.join(MediaItem).where(MediaItem.kind == args["kind"])
    if last:
        statement = statement.where(WatchEvent.id < last)
    values = db.session.scalars(
        statement.distinct()
        .order_by(WatchEvent.watched_at.desc(), WatchEvent.id.desc())
        .limit(size + 1)
    ).all()
    return jsonify(_collection([watch_event(item) for item in values], len(values), size, fp))


@blueprint.get("/watch-states")
def watch_states_list() -> Response:
    args = query_args(
        request,
        {"media_id", "source_id", "completed", "observed_from", "observed_to", "limit", "cursor"},
    )
    size = limit(args.get("limit"))
    fp = fingerprint({k: v for k, v in args.items() if k not in {"limit", "cursor"}})
    last = decode_cursor(args.get("cursor"), fp)
    statement = select(WatchState)
    for field, column in (
        ("media_id", WatchState.media_item_id),
        ("source_id", WatchState.source_id),
    ):
        if field in args:
            statement = statement.where(column == _query_int(args[field], field))
    completed = bool_query(args.get("completed"), "completed")
    if completed is not None:
        statement = statement.where(WatchState.completed == completed)
    if last:
        statement = statement.where(WatchState.id > last)
    values = db.session.scalars(statement.order_by(WatchState.id).limit(size + 1)).all()
    return jsonify(_collection([watch_state(item) for item in values], len(values), size, fp))


@blueprint.get("/dashboard/summary")
def dashboard_summary() -> Response:
    def count(model: type[Any], *criteria: Any) -> int:
        return int(db.session.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)

    last = db.session.scalar(select(SyncRun).order_by(SyncRun.created_at.desc(), SyncRun.id.desc()))
    active = db.session.scalar(
        select(SyncRun)
        .where(SyncRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
        .order_by(SyncRun.id.desc())
    )
    return jsonify(
        {
            "media": {
                "movies": count(MediaItem, MediaItem.kind == MediaKind.MOVIE),
                "shows": count(MediaItem, MediaItem.kind == MediaKind.SHOW),
                "episodes": count(MediaItem, MediaItem.kind == MediaKind.EPISODE),
            },
            "watched": {
                "movies": count(
                    WatchState,
                    WatchState.completed.is_(True),
                    WatchState.media_item_id.in_(
                        select(MediaItem.id).where(MediaItem.kind == MediaKind.MOVIE)
                    ),
                ),
                "episodes": count(
                    WatchState,
                    WatchState.completed.is_(True),
                    WatchState.media_item_id.in_(
                        select(MediaItem.id).where(MediaItem.kind == MediaKind.EPISODE)
                    ),
                ),
            },
            "sources": {
                "configured": count(Source),
                "enabled": count(Source, Source.enabled.is_(True)),
            },
            "libraries": {
                "available": count(Library, Library.available.is_(True)),
                "enabled": count(Library, Library.enabled.is_(True)),
            },
            "last_sync_run": sync_run(last) if last else None,
            "active_sync_run": sync_run(active) if active else None,
        }
    )


def _media_summary(entity: MediaItem) -> dict[str, Any]:
    state = db.session.scalar(
        select(WatchState)
        .where(WatchState.media_item_id == entity.id)
        .order_by(WatchState.last_watched_at.desc().nullslast(), WatchState.id.desc())
    )
    available = bool(
        db.session.scalar(
            select(func.count())
            .select_from(SourceMediaRef)
            .where(SourceMediaRef.media_item_id == entity.id, SourceMediaRef.available.is_(True))
        )
    )
    return media(entity, watched=state, available=available)


def _query_int(value: str, field: str) -> int:
    try:
        return integer(int(value), field, minimum=1)
    except ValueError as error:
        raise validation_error(
            field, "invalid_integer", "A positive integer is required."
        ) from error


def _connector_error(error: ConnectorError) -> AppError:
    if isinstance(error, ConnectorAuthenticationError):
        return AppError("plex_authentication_failed", "Plex authentication failed.", 502)
    if isinstance(error, ConnectorTimeoutError):
        return AppError("plex_timeout", "Plex request timed out.", 504)
    if isinstance(error, ConnectorConnectionError):
        return AppError("plex_unreachable", "Plex is unreachable.", 503)
    if isinstance(error, ConnectorResponseError):
        return AppError("plex_invalid_response", "Plex returned an invalid response.", 502)
    return AppError("plex_error", "Plex request failed.", 502)
