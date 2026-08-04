"""Web interface flows, HTMX fallback, CSRF and safe presentation."""

from __future__ import annotations

import re
from datetime import UTC, datetime

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
from euvieouvi.connectors.errors import ConnectorConnectionError
from euvieouvi.database.enums import (
    ConnectorType,
    LibraryMediaType,
    MediaKind,
    SyncStatus,
    SyncTrigger,
)
from euvieouvi.database.models import (
    Library,
    MediaItem,
    Source,
    SourceMediaRef,
    SyncError,
    SyncRun,
    SyncRunLibrary,
    WatchEvent,
    WatchState,
)
from euvieouvi.extensions import db
from euvieouvi.web.formatting import duration_ms, local_datetime

NOW = datetime(2026, 8, 4, 18, tzinfo=UTC)


class WebConnector:
    def test_connection(self) -> ConnectionInfo:
        return ConnectionInfo("plexsrv", "machine", True)

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


def csrf(client: object, path: str = "/") -> str:
    response = client.get(path)  # type: ignore[attr-defined]
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', response.get_data(as_text=True))
    assert match
    return match.group(1)


def seed_web() -> tuple[int, int, int, int]:
    source = Source(
        connector_type=ConnectorType.PLEX,
        name="Principal",
        base_url="http://plex:32400",
        secret="never-show",
        enabled=True,
        last_connection_status="succeeded",
        last_connection_test_at=NOW,
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
        year=2016,
        duration_ms=6960000,
        summary="Contato com visitantes.",
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
                last_seen_at=NOW,
                available=True,
            ),
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
                dedup_key="d1",
                watched_at=NOW,
                completed=True,
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
        items_read=1,
        items_inserted=1,
        summary="Concluída",
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
            SyncError(
                sync_run_id=run.id,
                library_id=library.id,
                category="warning",
                message="Mensagem segura",
                retryable=False,
                occurred_at=NOW,
            ),
        ]
    )
    db.session.commit()
    return source.id, library.id, movie.id, run.id


def test_first_access_navigation_and_local_assets(app: Flask) -> None:
    client = app.test_client()
    page = client.get("/")
    text = page.get_data(as_text=True)
    assert page.status_code == 200 and "Configure seu servidor Plex" in text
    assert "bootstrap.d85327d9.min.css" in text
    assert "htmx.22283ef6.min.js" in text and "Pular para o conteúdo" in text
    assert client.get("/setup").status_code == 302
    for path, title in (
        ("/settings/plex", "Configurações do Plex"),
        ("/libraries", "Bibliotecas"),
        ("/sync", "Sincronizações"),
        ("/history", "Histórico"),
        ("/about", "Sobre esta instalação"),
    ):
        response = client.get(path)
        assert response.status_code == 200 and title in response.get_data(as_text=True)
    assert client.get("/static/vendor/bootstrap.d85327d9.min.css").status_code == 200
    assert client.get("/static/vendor/htmx.22283ef6.min.js").status_code == 200
    assert page.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]


def test_plex_form_validation_save_and_token_preservation(app: Flask) -> None:
    client = app.test_client()
    token = csrf(client, "/settings/plex")
    assert client.post("/settings/plex", data={"csrf_token": token}).status_code == 200
    saved = client.post(
        "/settings/plex",
        data={
            "csrf_token": token,
            "name": "Plex",
            "base_url": "http://plex:32400",
            "secret": "private",
            "enabled": "on",
        },
    )
    assert saved.status_code == 302
    page = client.get("/settings/plex").get_data(as_text=True)
    assert "Token já configurado" in page and "private" not in page
    token = csrf(client, "/settings/plex")
    assert (
        client.post(
            "/settings/plex",
            data={
                "csrf_token": token,
                "name": "Plex novo",
                "base_url": "http://plex:32400",
                "secret": "",
                "enabled": "on",
            },
        ).status_code
        == 302
    )
    with app.app_context():
        source = db.session.get(Source, 1)
        assert source and source.secret == "private" and source.name == "Plex novo"


def test_csrf_required_for_web_but_not_api(app: Flask) -> None:
    client = app.test_client()
    csrf(client)
    denied = client.post("/settings/plex", data={"name": "x"})
    assert denied.status_code == 400
    api = client.post(
        "/api/v1/sources",
        json={"connector_type": "plex", "name": "API", "base_url": "http://plex", "secret": "x"},
    )
    assert api.status_code == 201


def test_connection_discovery_selection_htmx_and_fallback(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        source = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex",
            secret="x",
            enabled=True,
        )
        db.session.add(source)
        db.session.commit()
    monkeypatch.setattr("euvieouvi.web.routes.connector_for", lambda source: WebConnector())
    client = app.test_client()
    token = csrf(client, "/settings/plex")
    assert client.post("/settings/plex/test", data={"csrf_token": token}).status_code == 302
    token = csrf(client, "/libraries")
    assert client.post("/libraries/discover", data={"csrf_token": token}).status_code == 302
    with app.app_context():
        library_id = db.session.scalar(db.select(Library.id))
        assert library_id
    response = client.post(
        f"/libraries/{library_id}/selection",
        data={"csrf_token": token, "enabled": "true"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200 and f'id="library-{library_id}"' in response.get_data(
        as_text=True
    )
    token = csrf(client, "/libraries")
    assert (
        client.post(
            f"/libraries/{library_id}/selection", data={"csrf_token": token, "enabled": "false"}
        ).status_code
        == 302
    )


def test_failed_connection_and_discovery_preserve_local_pages(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        seed_web()

    class Failed(WebConnector):
        def test_connection(self) -> ConnectionInfo:
            raise ConnectorConnectionError("secret technical detail")

    monkeypatch.setattr("euvieouvi.web.routes.connector_for", lambda source: Failed())
    client = app.test_client()
    token = csrf(client, "/settings/plex")
    result = client.post("/settings/plex/test", data={"csrf_token": token}, follow_redirects=True)
    assert "não respondeu" in result.get_data(
        as_text=True
    ) and "secret technical detail" not in result.get_data(as_text=True)
    token = csrf(client, "/libraries")
    result = client.post("/libraries/discover", data={"csrf_token": token}, follow_redirects=True)
    assert "lista anterior foi preservada" in result.get_data(as_text=True)
    assert client.get("/history").status_code == 200 and "Arrival" in client.get(
        "/history"
    ).get_data(as_text=True)


def test_dashboard_history_media_and_sync_detail(app: Flask) -> None:
    with app.app_context():
        _, _, media_id, run_id = seed_web()
    client = app.test_client()
    home = client.get("/").get_data(as_text=True)
    assert "Arrival" in home and "Filmes assistidos" in home and "succeeded" in home
    for query in (
        "",
        "?query=Arr&kind=movie&watched=watched",
        "?watched=all",
        "?watched=progress",
        "?watched=unwatched",
        "?page=2",
    ):
        assert client.get(f"/history{query}").status_code == 200
    detail = client.get(f"/media/{media_id}").get_data(as_text=True)
    assert (
        "Contato com visitantes" in detail
        and "Eventos reais conhecidos" in detail
        and "visualização 2" in detail
    )
    sync = client.get(f"/sync/{run_id}").get_data(as_text=True)
    assert "Mensagem segura" in sync and "Bibliotecas" in sync
    assert client.get("/sync/active-fragment").status_code == 200
    assert (
        client.get("/media/999").status_code == 404 and client.get("/sync/999").status_code == 404
    )


def test_sync_start_cancel_and_polling_fragment(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        source_id, _, _, finished_id = seed_web()

    class Executor:
        def submit(self, submitted_source_id: int) -> int:
            assert submitted_source_id == source_id
            run = SyncRun(source_id=source_id, trigger=SyncTrigger.API, status=SyncStatus.QUEUED)
            db.session.add(run)
            db.session.commit()
            return run.id

        def cancel(self, run_id: int) -> bool:
            return True

    executor = Executor()
    monkeypatch.setattr("euvieouvi.web.routes.get_executor", lambda app: executor)
    client = app.test_client()
    token = csrf(client, "/sync")
    started = client.post("/sync", data={"csrf_token": token})
    assert started.status_code == 302 and "/sync/" in started.headers["Location"]
    active = client.get("/sync/active-fragment").get_data(as_text=True)
    assert 'hx-trigger="every 3s"' in active
    run_id = int(started.headers["Location"].rsplit("/", 1)[1])
    token = csrf(client, f"/sync/{run_id}")
    assert client.post(f"/sync/{run_id}/cancel", data={"csrf_token": token}).status_code == 302
    token = csrf(client, f"/sync/{finished_id}")
    assert (
        client.post(
            f"/sync/{finished_id}/cancel", data={"csrf_token": token}, follow_redirects=True
        ).status_code
        == 200
    )


def test_formatters(app: Flask) -> None:
    with app.app_context():
        assert local_datetime(NOW) == "04/08/2026 15:00"
        assert local_datetime(None) == "—"
        assert duration_ms(6_960_000) == "1h 56min"
        assert duration_ms(120_000) == "2 min"
        assert duration_ms(None) == "—"
