"""REST API contract, flow and security tests."""

# mypy: disable-error-code="index"

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from flask import Flask

from euvieouvi.connectors.dtos import (
    ConnectionInfo,
    ExternalLibrary,
    ExternalLibraryRef,
    ExternalLibraryType,
    ExternalMediaItem,
    ExternalMediaKind,
    ExternalWatchEvent,
    HistoryCheckpoint,
    Page,
    PageRequest,
)
from euvieouvi.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorConnectionError,
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
from euvieouvi.extensions import db

NOW = datetime(2026, 8, 4, 18, tzinfo=UTC)


class ApiConnector:
    last_unsupported_libraries = ()

    def test_connection(self) -> ConnectionInfo:
        return ConnectionInfo(
            "plexsrv", "machine", True, "1.0", frozenset({"libraries", "history"})
        )

    def list_libraries(self) -> list[ExternalLibrary]:
        return [ExternalLibrary("1", "Filmes", ExternalLibraryType.MOVIE)]

    def get_media_page(
        self, library: ExternalLibraryRef, media_kind: ExternalMediaKind, page: PageRequest
    ) -> Page[ExternalMediaItem]:
        del library, media_kind
        return Page((), page.start, 0)

    def get_history_page(
        self, library: ExternalLibraryRef, checkpoint: HistoryCheckpoint | None, page: PageRequest
    ) -> Page[ExternalWatchEvent]:
        del library, checkpoint
        return Page((), page.start, 0)


def seed_catalog() -> tuple[int, int, int, int]:
    source = Source(
        connector_type=ConnectorType.PLEX,
        name="Principal",
        base_url="http://plex:32400",
        secret="top-secret",
        enabled=True,
    )
    db.session.add(source)
    db.session.flush()
    library = Library(
        source_id=source.id,
        external_id="1",
        name="Filmes",
        media_type=LibraryMediaType.MOVIE,
        enabled=True,
        available=True,
        discovered_at=NOW,
        last_seen_at=NOW,
    )
    db.session.add(library)
    db.session.flush()
    movie = MediaItem(
        kind=MediaKind.MOVIE,
        title="Arrival",
        original_title="Arrival",
        sort_title="Arrival",
        year=2016,
        duration_ms=6960000,
        originally_available_on=date(2016, 11, 11),
        summary="First contact",
    )
    db.session.add(movie)
    db.session.flush()
    db.session.add_all(
        [
            SourceMediaRef(
                source_id=source.id,
                library_id=library.id,
                media_item_id=movie.id,
                external_id="m1",
                external_key="/library/metadata/m1",
                last_seen_at=NOW,
                available=True,
            ),
            MediaIdentifier(media_item_id=movie.id, provider="tmdb", external_id="329865"),
            WatchState(
                media_item_id=movie.id,
                source_id=source.id,
                view_count=2,
                last_watched_at=NOW,
                completed=True,
                progress_ms=None,
                observed_at=NOW,
            ),
            WatchEvent(
                media_item_id=movie.id,
                source_id=source.id,
                source_event_id="e1",
                dedup_key="dedup",
                watched_at=NOW,
                completed=True,
                progress_ms=None,
                duration_ms=6960000,
                view_number=2,
            ),
        ]
    )
    run = SyncRun(
        source_id=source.id,
        trigger=SyncTrigger.API,
        status=SyncStatus.SUCCEEDED,
        started_at=NOW,
        finished_at=NOW,
        heartbeat_at=NOW,
        summary="ok",
    )
    db.session.add(run)
    db.session.flush()
    db.session.add_all(
        [
            SyncRunLibrary(
                sync_run_id=run.id,
                library_id=library.id,
                status=SyncStatus.SUCCEEDED,
                started_at=NOW,
                finished_at=NOW,
                items_read=1,
                items_inserted=1,
            ),
            SyncCheckpoint(
                library_id=library.id,
                strategy="plex_pages_v1",
                cursor='{"stage":"media","start":0}',
                last_successful_run_id=run.id,
                updated_at=NOW,
            ),
            SyncError(
                sync_run_id=run.id,
                library_id=library.id,
                category="warning",
                message="safe",
                retryable=False,
                occurred_at=NOW,
            ),
        ]
    )
    db.session.commit()
    return source.id, library.id, movie.id, run.id


def test_source_crud_discovery_and_secret_safety(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("euvieouvi.api.routes.connector_for", lambda source: ApiConnector())
    client = app.test_client()
    response = client.post(
        "/api/v1/sources",
        json={
            "connector_type": "plex",
            "name": "Plex",
            "base_url": "http://plex:32400",
            "secret": "never-return",
            "enabled": True,
        },
    )
    assert response.status_code == 201
    source_id = response.json["id"]
    assert "never-return" not in response.get_data(as_text=True)
    assert response.json["has_secret"] is True
    assert client.get("/api/v1/sources").json[0]["id"] == source_id
    assert client.get(f"/api/v1/sources/{source_id}").json["name"] == "Plex"
    tested = client.post(f"/api/v1/sources/{source_id}/connection-test")
    assert tested.json["server_name"] == "plexsrv"
    discovered = client.post(f"/api/v1/sources/{source_id}/library-discoveries")
    assert discovered.json["supported"] == 1
    library_id = discovered.json["libraries"][0]["id"]
    assert (
        client.patch(f"/api/v1/libraries/{library_id}", json={"enabled": True}).json["enabled"]
        is True
    )
    assert (
        client.get(
            "/api/v1/libraries?source_id=1&media_type=movie&enabled=true&available=true"
        ).status_code
        == 200
    )
    changed = client.patch(
        f"/api/v1/sources/{source_id}",
        json={
            "name": "Plex novo",
            "base_url": "https://plex.local",
            "secret": "changed",
            "enabled": False,
        },
    )
    assert changed.json["name"] == "Plex novo" and "changed" not in changed.get_data(as_text=True)


@pytest.mark.parametrize(
    "payload,status",
    [
        ({}, 422),
        ({"connector_type": "jellyfin", "name": "x", "base_url": "http://x", "secret": "x"}, 422),
        ({"connector_type": "plex", "name": "x", "base_url": "http://u:p@x", "secret": "x"}, 422),
    ],
)
def test_source_validation_and_uniform_errors(
    app: Flask, payload: dict[str, object], status: int
) -> None:
    response = app.test_client().post("/api/v1/sources", json=payload)
    assert response.status_code == status
    assert set(response.json["error"]) == {"code", "message", "status", "request_id", "details"}


def test_content_type_duplicates_missing_and_library_conflicts(app: Flask) -> None:
    client = app.test_client()
    assert client.post("/api/v1/sources", data="{}", content_type="text/plain").status_code == 415
    valid = {"connector_type": "plex", "name": "Plex", "base_url": "http://plex", "secret": "x"}
    assert client.post("/api/v1/sources", json=valid).status_code == 201
    assert client.post("/api/v1/sources", json=valid).status_code == 409
    assert client.patch("/api/v1/sources/1", json={}).status_code == 422
    assert client.patch("/api/v1/sources/1", json={"secret": None}).status_code == 422
    assert client.get("/api/v1/sources/999").status_code == 404
    with app.app_context():
        unavailable = Library(
            source_id=1,
            external_id="x",
            name="Gone",
            media_type=LibraryMediaType.MOVIE,
            enabled=False,
            available=False,
            discovered_at=NOW,
            last_seen_at=NOW,
        )
        db.session.add(unavailable)
        db.session.commit()
        library_id = unavailable.id
    assert (
        client.patch(f"/api/v1/libraries/{library_id}", json={"enabled": True}).status_code == 409
    )
    assert client.get("/api/v1/libraries?enabled=yes").status_code == 422


def test_catalog_events_states_dashboard_and_pagination(app: Flask) -> None:
    with app.app_context():
        source_id, library_id, media_id, run_id = seed_catalog()
    client = app.test_client()
    listing = client.get(
        f"/api/v1/media?kind=movie&library_id={library_id}&query=Arr&year=2016&available=true&watched=true&limit=1"
    )
    assert listing.status_code == 200 and listing.json["items"][0]["watch_state"]["view_count"] == 2
    detail = client.get(f"/api/v1/media/{media_id}").json
    assert detail["identifiers"][0]["provider"] == "tmdb" and detail["known_event_count"] == 1
    assert (
        client.get(
            f"/api/v1/watch-events?media_id={media_id}&source_id={source_id}&library_id={library_id}&kind=movie&completed=true"
        ).json["items"][0]["view_number"]
        == 2
    )
    assert (
        client.get(
            f"/api/v1/watch-states?media_id={media_id}&source_id={source_id}&completed=true"
        ).json["items"][0]["view_count"]
        == 2
    )
    sync = client.get(f"/api/v1/sync-runs/{run_id}").json
    assert sync["libraries"] and sync["errors"] and sync["checkpoints"]
    assert (
        client.get(f"/api/v1/sync-runs?source_id={source_id}&status=succeeded&limit=1").json[
            "items"
        ][0]["id"]
        == run_id
    )
    summary = client.get("/api/v1/dashboard/summary").json
    assert summary["media"]["movies"] == 1 and summary["watched"]["movies"] == 1
    assert client.get("/api/v1/sync-runs/active").status_code == 204
    assert client.get("/api/v1/media/999").status_code == 404


def test_cursor_limits_and_query_validation(app: Flask) -> None:
    with app.app_context():
        seed_catalog()
    client = app.test_client()
    assert client.get("/api/v1/media?limit=0").status_code == 422
    assert client.get("/api/v1/media?limit=x").status_code == 422
    assert client.get("/api/v1/media?cursor=garbage").status_code == 422
    assert client.get("/api/v1/media?unknown=x").status_code == 422
    assert client.get("/api/v1/media?kind=bad").status_code == 422
    assert client.get("/api/v1/media?year=bad").status_code == 422
    assert client.get("/api/v1/media?limit=1&limit=2").status_code == 422
    assert client.get("/api/v1/sync-runs?status=bad").status_code == 422
    assert client.get("/api/v1/libraries/999").status_code == 404
    assert client.patch("/api/v1/libraries/999", json={"enabled": True}).status_code == 404
    assert (
        client.post("/api/v1/sources", data="{", content_type="application/json").status_code == 400
    )
    assert client.post("/api/v1/sources", json=[]).status_code == 422
    assert (
        client.post(
            "/api/v1/sources",
            json={
                "connector_type": "plex",
                "name": "x",
                "base_url": "http://x",
                "secret": "x",
                "extra": 1,
            },
        ).status_code
        == 422
    )


def test_http_errors_body_limit_and_cors_default(app: Flask) -> None:
    client = app.test_client()
    missing = client.get("/api/v1/not-a-route")
    assert missing.status_code == 404 and missing.is_json
    assert client.delete("/api/v1/sources").status_code == 405
    oversized = client.post(
        "/api/v1/sources",
        data=b"x" * (65 * 1024),
        content_type="application/json",
    )
    assert oversized.status_code == 413
    assert "Access-Control-Allow-Origin" not in client.get("/api/v1/sources").headers


@pytest.mark.parametrize(
    "error,status,code",
    [
        (ConnectorAuthenticationError("x"), 502, "plex_authentication_failed"),
        (ConnectorTimeoutError("x"), 504, "plex_timeout"),
        (ConnectorConnectionError("x"), 503, "plex_unreachable"),
        (ConnectorResponseError("x"), 502, "plex_invalid_response"),
    ],
)
def test_safe_connector_errors(
    app: Flask, monkeypatch: pytest.MonkeyPatch, error: Exception, status: int, code: str
) -> None:
    class Failing(ApiConnector):
        def test_connection(self) -> ConnectionInfo:
            raise error

    with app.app_context():
        source_id, _, _, _ = seed_catalog()
    monkeypatch.setattr("euvieouvi.api.routes.connector_for", lambda source: Failing())
    response = app.test_client().post(f"/api/v1/sources/{source_id}/connection-test")
    assert (
        response.status_code == status
        and response.json["error"]["code"] == code
        and "top-secret" not in response.get_data(as_text=True)
    )


def test_sync_start_finish_and_finished_cancellation(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        source_id, _, _, finished_run_id = seed_catalog()
    monkeypatch.setattr("euvieouvi.api.runtime.connector_for", lambda source: ApiConnector())
    response = app.test_client().post("/api/v1/sync-runs", json={"source_id": source_id})
    assert response.status_code == 202 and response.headers["Location"]
    run_id = response.json["id"]
    assert app.test_client().post(f"/api/v1/sync-runs/{run_id}/cancellation").status_code == 202
    assert (
        app.test_client().post(f"/api/v1/sync-runs/{finished_run_id}/cancellation").status_code
        == 409
    )


def test_openapi_covers_all_routes() -> None:
    text = Path("openapi.yaml").read_text(encoding="utf-8")
    for path in (
        "/sources:",
        "/libraries:",
        "/sync-runs:",
        "/media:",
        "/watch-events:",
        "/watch-states:",
        "/dashboard/summary:",
    ):
        assert path in text
    assert "openapi: 3.1.0" in text and "secret:" not in text
