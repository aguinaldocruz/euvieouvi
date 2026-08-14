"""Web interface flows, HTMX fallback, CSRF and safe presentation."""

from __future__ import annotations

import io
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import select

from euvieouvi.api.runtime import LocalSyncExecutor
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
from euvieouvi.connectors.plex.connector import PlexConnector
from euvieouvi.database.enums import (
    ConnectorType,
    LibraryMediaType,
    MediaKind,
    SyncStatus,
    SyncTrigger,
)
from euvieouvi.database.models import (
    Genre,
    JobRun,
    Library,
    MediaGenre,
    MediaIdentifier,
    MediaImage,
    MediaItem,
    Setting,
    Source,
    SourceMediaRef,
    SyncError,
    SyncRun,
    SyncRunLibrary,
    WatchEvent,
    WatchState,
    WebhookEvent,
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

    def fetch_image(self, source_path: str, *, width: int, height: int) -> tuple[bytes, str]:
        del source_path, width, height
        return b"", "image/jpeg"


def csrf(client: object, path: str = "/") -> str:
    response = client.get(path)  # type: ignore[attr-defined]
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', response.get_data(as_text=True))
    assert match
    return match.group(1)


def test_jobs_page_lists_independent_operations_and_saves_schedules(app: Flask) -> None:
    client = app.test_client()
    page = client.get("/jobs")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Sincronizar Plex" in body
    assert "Assistidos: Plex → Jellyfin" in body
    assert "Assistidos: Jellyfin → Plex" in body
    assert "Atualizar metadados" in body
    assert "Baixar imagens do catálogo" in body
    assert "Otimizar dados" in body
    assert "Última execução" in body
    assert "Jobs e agendamentos" not in body
    assert client.get("/settings/sync").status_code == 404
    assert "Agendamento sync" not in client.get("/settings/backup").get_data(as_text=True)
    assert 'hx-trigger="every 2s"' in body
    assert client.get("/jobs/sync_plex/status-fragment").status_code == 200
    dashboard_jobs = client.get("/jobs/dashboard-fragment").get_data(as_text=True)
    assert "Jobs e tarefas" in dashboard_jobs
    assert 'hx-trigger="every 2s"' in dashboard_jobs

    token = csrf(client, "/jobs")
    response = client.post(
        "/jobs",
        data={
            "csrf_token": token,
            "enabled_sync_plex": "on",
            "time_sync_plex": "02:15",
            "time_sync_jellyfin": "03:15",
            "time_watched_plex_to_jellyfin": "03:30",
            "time_watched_jellyfin_to_plex": "03:45",
            "time_metadata": "04:00",
            "time_catalog_images": "04:30",
            "time_maintenance": "05:00",
            "retention_keep": "7",
            "watch_sync_enabled": "on",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Setting, "jobs.sync_plex.enabled").value == "true"  # type: ignore[union-attr]
        assert db.session.get(Setting, "jobs.sync_plex.time").value == "02:15"  # type: ignore[union-attr]
        assert db.session.get(Setting, "jobs.retention.keep_last").value == "7"  # type: ignore[union-attr]
        assert db.session.get(Setting, "watch_sync.enabled").value == "true"  # type: ignore[union-attr]


def test_maintenance_job_persists_result_and_readable_log(app: Flask) -> None:
    client = app.test_client()
    response = client.post(
        "/jobs/maintenance/run",
        data={"csrf_token": csrf(client, "/jobs")},
    )
    assert response.status_code == 302
    deadline = time.monotonic() + 3
    run_id = 0
    while time.monotonic() < deadline:
        with app.app_context():
            run = db.session.scalar(
                select(JobRun).where(JobRun.job_id == "maintenance").order_by(JobRun.id.desc())
            )
            if run is not None:
                run_id = run.id
                if run.status is SyncStatus.SUCCEEDED:
                    break
        time.sleep(0.02)
    assert run_id
    log = client.get(f"/job-runs/{run_id}/log")
    assert log.status_code == 200
    assert "Otimização concluída" in log.get_data(as_text=True)


def test_watched_state_propagation_is_disabled_by_default_and_configurable(app: Flask) -> None:
    client = app.test_client()
    page = client.get("/jobs").get_data(as_text=True)
    assert 'id="watch_sync_enabled"' in page
    assert 'id="watch_sync_enabled" name="watch_sync_enabled" type="checkbox" checked' not in page

    token = csrf(client, "/jobs")
    response = client.post(
        "/jobs",
        data={"csrf_token": token, "watch_sync_enabled": "on"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        setting = db.session.get(Setting, "watch_sync.enabled")
        assert setting is not None and setting.value == "true"


def test_appearance_setting_is_persisted_and_used_as_page_default(app: Flask) -> None:
    client = app.test_client()
    token = csrf(client, "/settings/appearance")
    response = client.post(
        "/settings/appearance",
        data={"csrf_token": token, "theme": "dark"},
        follow_redirects=True,
    )
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Preferência de aparência atualizada" in text
    assert 'data-default-theme="dark"' in text
    assert 'value="dark" checked' in text
    with app.app_context():
        setting = db.session.get(Setting, "ui.theme")
        assert setting is not None and setting.value == "dark"


def test_metadata_settings_and_manual_enrichment(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = app.test_client()
    token = csrf(client, "/settings/metadata")
    missing = client.post(
        "/settings/metadata",
        data={"csrf_token": token, "tmdb_enabled": "on"},
    )
    assert "Informe o token" in missing.get_data(as_text=True)
    token = csrf(client, "/settings/metadata")
    saved = client.post(
        "/settings/metadata",
        data={
            "csrf_token": token,
            "tmdb_enabled": "on",
            "tmdb_token": "hidden-token",
            "musicbrainz_enabled": "on",
            "auto_after_sync": "on",
            "language": "pt-BR",
        },
        follow_redirects=True,
    )
    text = saved.get_data(as_text=True)
    assert saved.status_code == 200 and "Configuração de metadados atualizada" in text
    assert "hidden-token" not in text

    class Executor:
        watch_sync_active = False

        active = False

        def __init__(self) -> None:
            self.snapshot = {
                "active": False,
                "processed": 0,
                "updated": 0,
                "failed": 0,
                "total": 0,
                "percent": 0,
            }

        def submit(self) -> bool:
            return True

    executor = Executor()
    monkeypatch.setattr("euvieouvi.web.routes.get_enrichment_executor", lambda app: executor)
    token = csrf(client, "/settings/metadata")
    started = client.post("/metadata/enrich", data={"csrf_token": token}, follow_redirects=True)
    assert "Enriquecimento iniciado" in started.get_data(as_text=True)

    executor.snapshot = {
        "active": True,
        "processed": 50,
        "updated": 20,
        "failed": 1,
        "total": 100,
        "percent": 50,
    }
    progress = client.get("/metadata/enrichment-status").get_data(as_text=True)
    assert "50%" in progress and "50 de 100 processados" in progress


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
        studio="Paramount",
        content_rating="14",
        audience_rating=8.1,
    )
    db.session.add(movie)
    db.session.flush()
    genre = Genre(name="Ficção científica", normalized_name="ficção científica")
    db.session.add(genre)
    db.session.flush()
    db.session.add(MediaGenre(media_item_id=movie.id, genre_id=genre.id))
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
        ("/settings/appearance", "Aparência"),
        ("/libraries", "Bibliotecas"),
        ("/jobs", "Jobs e tarefas"),
        ("/history", "Histórico"),
        ("/catalog", "Catálogo"),
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


def test_plex_user_selector_saves_numeric_account_id(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PlexUsersConnector(PlexConnector):
        def __init__(self) -> None:
            pass

        def list_users(self):
            from euvieouvi.connectors.dtos import ExternalUser

            return (ExternalUser("7", "Alice"), ExternalUser("42", "Zoe"))

        def close(self) -> None:
            pass

    with app.app_context():
        source = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex:32400",
            secret="private",
            enabled=True,
            last_connection_status="succeeded",
        )
        db.session.add_all([source, Setting(key="plex.user_id", value="7")])
        db.session.commit()

    monkeypatch.setattr("euvieouvi.web.routes.connector_for", lambda source: PlexUsersConnector())
    client = app.test_client()
    page = client.get("/settings/plex")
    text = page.get_data(as_text=True)
    assert '<option value="7" selected>Alice</option>' in text
    assert '<option value="42"' in text

    backup_text = client.get("/settings/backup").get_data(as_text=True)
    assert 'id="trakt_plex_user_name" value="Alice"' in backup_text
    assert 'type="hidden" name="plex_user" value="7"' in backup_text

    token = csrf(client, "/settings/plex")
    saved = client.post(
        "/settings/plex",
        data={
            "csrf_token": token,
            "name": "Plex",
            "base_url": "http://plex:32400",
            "secret": "",
            "user_id": "42",
            "enabled": "on",
        },
    )
    assert saved.status_code == 302
    with app.app_context():
        assert db.session.get(Setting, "plex.user_id").value == "42"
        assert db.session.get(Setting, "webhook.plex.user_filter").value == "42"


def test_trakt_upload_uses_route_specific_body_limit(app: Flask) -> None:
    client = app.test_client()
    token = csrf(client, "/settings/backup")
    response = client.post(
        "/backups/trakt-import",
        data={
            "csrf_token": token,
            "archive": (io.BytesIO(b"x" * (65 * 1024)), "export.zip"),
            "source_id": "invalid",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    status = client.get("/backups/trakt-import/status")
    assert status.status_code == 200
    assert {"active", "state", "percent", "message"} <= set(status.get_json())


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
        and "Histórico completo" in detail
        and "reprodução 2" in detail
        and "Principal · Filmes" in detail
    )
    with app.app_context():
        detail_row = db.session.query(SyncRunLibrary).filter_by(sync_run_id=run_id).one()
        detail_row.status = SyncStatus.RUNNING
        detail_row.finished_at = None
        detail_row.items_scanned = 50
        detail_row.items_total = 200
        db.session.commit()
    sync = client.get(f"/jobs/sync-runs/{run_id}/fragment").get_data(as_text=True)
    assert (
        "Sincronização #" in sync
        and "Plex" in sync
        and "Bibliotecas" in sync
        and "Filmes" in sync
        and "Mensagem segura" in sync
        and "(25%)" in sync
    )
    jobs = client.get("/jobs").get_data(as_text=True)
    assert "Sincronizar Plex" in jobs and "Acompanhar" in jobs
    assert (
        client.get("/media/999").status_code == 404
        and client.get("/jobs/sync-runs/999/fragment").status_code == 404
    )


def test_partial_event_is_hidden_from_completion_views(app: Flask) -> None:
    with app.app_context():
        _, _, media_id, _ = seed_web()
        event = db.session.query(WatchEvent).one()
        state = db.session.query(WatchState).one()
        event.completed = False
        event.progress_ms = 120_000
        state.completed = False
        state.view_count = 0
        state.progress_ms = 120_000
        db.session.commit()

    client = app.test_client()
    detail = client.get(f"/media/{media_id}").get_data(as_text=True)
    dashboard = client.get("/").get_data(as_text=True)

    assert "Nenhuma reprodução conhecida" in detail
    assert "Reprodução parcial" not in detail
    assert "Reprodução parcial" not in dashboard


def test_sync_start_cancel_and_polling_fragment(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        source_id, _, _, finished_id = seed_web()

    class Executor:
        watch_sync_active = False

        def submit(self, submitted_source_id: int, *, trigger: SyncTrigger) -> int:
            assert submitted_source_id == source_id
            assert trigger is SyncTrigger.MANUAL
            run = SyncRun(source_id=source_id, trigger=trigger, status=SyncStatus.QUEUED)
            db.session.add(run)
            db.session.commit()
            return run.id

        def cancel(self, run_id: int) -> bool:
            return True

    executor = Executor()
    monkeypatch.setattr("euvieouvi.sync.jobs.get_executor", lambda app: executor)
    monkeypatch.setattr("euvieouvi.web.routes.get_executor", lambda app: executor)
    client = app.test_client()
    token = csrf(client, "/jobs")
    started = client.post("/jobs/sync_plex/run", data={"csrf_token": token})
    assert started.status_code == 302 and started.headers["Location"].endswith("/jobs")
    with app.app_context():
        run_id = db.session.scalar(select(SyncRun.id).order_by(SyncRun.id.desc()))
    active = client.get(f"/jobs/sync-runs/{run_id}/fragment").get_data(as_text=True)
    assert 'hx-trigger="every 3s"' in active
    token = csrf(client, "/jobs")
    assert client.post(
        f"/jobs/sync-runs/{run_id}/cancel", data={"csrf_token": token}
    ).status_code == 302
    token = csrf(client, "/jobs")
    assert (
        client.post(
            f"/jobs/sync-runs/{finished_id}/cancel",
            data={"csrf_token": token},
            follow_redirects=True,
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


def test_catalog_filters_sorting_and_availability(app: Flask) -> None:
    with app.app_context():
        _, library_id, _, _ = seed_web()
    client = app.test_client()
    response = client.get(
        "/catalog?kind=movie&availability=available&played=played&sort=last_played&direction=desc"
    )
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Arrival" in text
    assert "Disponível no Plex" in text
    assert "Assistido 2 vezes" in text
    advanced = client.get(
        f"/catalog?kind=movie&library={library_id}&genre=ficção+científica&decade=2010&sort=rating&direction=desc"
    )
    assert advanced.status_code == 200 and "Arrival" in advanced.get_data(as_text=True)
    assert client.get("/catalog?kind=track&played=unplayed&sort=year").status_code == 200
    assert client.get("/catalog?availability=unavailable&sort=play_count").status_code == 200
    assert client.get("/catalog?played=progress&sort=first_played").status_code == 200
    assert client.get("/catalog?sort=duration&direction=desc").status_code == 200
    assert client.get("/catalog?sort=original_title").status_code == 200
    assert client.get("/catalog?sort=updated&direction=desc").status_code == 200
    assert client.get("/catalog?sort=removed&direction=desc").status_code == 200


def test_catalog_badges_follow_shared_provider_identity_across_localized_titles(app: Flask) -> None:
    with app.app_context():
        _, _, media_id, _ = seed_web()
        plex_movie = db.session.get(MediaItem, media_id)
        assert plex_movie is not None
        jellyfin = Source(
            connector_type=ConnectorType.JELLYFIN,
            name="Jellyfin",
            base_url="http://jellyfin",
            secret="{}",
            enabled=True,
        )
        db.session.add(jellyfin)
        db.session.flush()
        library = Library(
            source_id=jellyfin.id,
            external_id="jf-movies",
            name="Filmes Jellyfin",
            media_type=LibraryMediaType.MOVIE,
            enabled=True,
            available=True,
            discovered_at=NOW,
            last_seen_at=NOW,
        )
        localized = MediaItem(kind=MediaKind.MOVIE, title="A Chegada", year=plex_movie.year)
        db.session.add_all([library, localized])
        db.session.flush()
        db.session.add_all(
            [
                MediaIdentifier(media_item_id=plex_movie.id, provider="tmdb", external_id="329865"),
                MediaIdentifier(media_item_id=localized.id, provider="tmdb", external_id="329865"),
                SourceMediaRef(
                    source_id=jellyfin.id,
                    library_id=library.id,
                    media_item_id=localized.id,
                    external_id="jf-arrival",
                    last_seen_at=NOW,
                    available=True,
                ),
            ]
        )
        db.session.commit()

    text = app.test_client().get("/catalog?query=Arrival&kind=movie").get_data(as_text=True)
    assert "Disponível no Plex" in text
    assert "Disponível no Jellyfin" in text


def test_state_without_history_still_marks_media_as_watched(app: Flask) -> None:
    with app.app_context():
        seed_web()
        for event in db.session.scalars(select(WatchEvent)).all():
            db.session.delete(event)
        db.session.commit()

    client = app.test_client()
    catalog = client.get("/catalog?kind=movie&played=played").get_data(as_text=True)
    history = client.get("/history?kind=movie&watched=watched").get_data(as_text=True)

    assert "Arrival" in catalog
    assert "Assistido 2 vezes" in catalog
    assert "Arrival" in history


def test_auto_enrichment_remains_part_of_active_sync(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        source_id, _, _, _ = seed_web()
        db.session.add(Setting(key="metadata.auto_after_sync", value="true"))
        db.session.commit()

        def enrich(
            application: Flask,
            *,
            limit: int = 100,
            progress: object = None,
            cancelled: object = None,
        ) -> dict[str, int]:
            del limit
            assert application is app
            assert callable(progress)
            assert callable(cancelled)
            progress({"processed": 1, "updated": 1, "failed": 0})
            return {"processed": 1, "updated": 1, "failed": 0}

        monkeypatch.setattr("euvieouvi.enrichment.service.enrich_catalog", enrich)
        run_id = LocalSyncExecutor(app, lambda source: WebConnector()).submit(source_id)

    for _ in range(200):
        with app.app_context():
            run = db.session.get(SyncRun, run_id)
            if run is not None and run.status is SyncStatus.SUCCEEDED:
                break
        time.sleep(0.01)
    else:
        pytest.fail("background synchronization did not finish")

    with app.app_context():
        run = db.session.get(SyncRun, run_id)
        assert run is not None
        assert run.finished_at is not None
        assert run.summary is not None and "enriquecimento concluídos" in run.summary
        fragment = app.test_client().get(f"/jobs/sync-runs/{run_id}/fragment")
        assert fragment.status_code == 200
        assert "Etapa atual" in fragment.get_data(as_text=True)


def test_media_image_proxies_available_source_without_local_cache(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        source_id, _, movie_id, _ = seed_web()
    client = app.test_client()
    placeholder = client.get(f"/media/{movie_id}/image")
    assert placeholder.status_code == 200
    assert placeholder.mimetype == "image/svg+xml"

    with app.app_context():
        db.session.add(
            MediaImage(
                media_item_id=movie_id,
                source_id=source_id,
                image_type="poster",
                source_path=f"/library/metadata/{movie_id}/thumb",
                cache_status="pending",
            )
        )
        db.session.commit()

    calls: list[str] = []

    def fetch(
        connector: PlexConnector, source_path: str, *, width: int, height: int
    ) -> tuple[bytes, str]:
        del connector
        calls.append(source_path)
        assert (width, height) == (300, 450)
        return b"cached-jpeg", "image/jpeg"

    monkeypatch.setattr(PlexConnector, "fetch_image", fetch)
    first = client.get(f"/media/{movie_id}/image")
    second = client.get(f"/media/{movie_id}/image")
    assert first.status_code == 200 and first.data == b"cached-jpeg"
    assert second.status_code == 200 and second.data == b"cached-jpeg"
    assert calls == [
        f"/library/metadata/{movie_id}/thumb",
        f"/library/metadata/{movie_id}/thumb",
    ]
    with app.app_context():
        image = db.session.query(MediaImage).filter_by(media_item_id=movie_id).one()
        assert image.local_filename is None and image.cache_status == "pending"


def test_media_image_serves_external_fallback(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    with app.app_context():
        _, _, movie_id, _ = seed_web()
        db.session.add(
            MediaImage(
                media_item_id=movie_id,
                source_id=None,
                image_type="poster",
                provider="tmdb",
                source_url="https://image.tmdb.org/t/p/w500/poster.jpg",
                cache_status="pending",
            )
        )
        db.session.commit()

    def cache(image: MediaImage, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "external.jpg"
        path.write_bytes(b"external-image")
        image.mime_type = "image/jpeg"
        image.local_filename = path.name
        return path

    monkeypatch.setattr("euvieouvi.web.routes.ensure_external_cached", cache)
    response = app.test_client().get(f"/media/{movie_id}/image")
    assert response.status_code == 200
    assert response.data == b"external-image"


def test_series_detail_groups_episodes_by_season(app: Flask) -> None:
    with app.app_context():
        source_id, library_id, _, _ = seed_web()
        show = MediaItem(kind=MediaKind.SHOW, title="Futurama")
        db.session.add(show)
        db.session.flush()
        season = MediaItem(
            kind=MediaKind.SEASON,
            title="Season 1",
            parent_id=show.id,
            season_number=1,
        )
        db.session.add(season)
        db.session.flush()
        episode = MediaItem(
            kind=MediaKind.EPISODE,
            title="Space Pilot 3000",
            parent_id=season.id,
            season_number=1,
            episode_number=1,
        )
        db.session.add(episode)
        db.session.flush()
        db.session.add_all(
            [
                SourceMediaRef(
                    source_id=source_id,
                    library_id=library_id,
                    media_item_id=show.id,
                    external_id="show-1",
                    last_seen_at=NOW,
                    available=True,
                ),
                SourceMediaRef(
                    source_id=source_id,
                    library_id=library_id,
                    media_item_id=episode.id,
                    external_id="episode-1",
                    last_seen_at=NOW,
                    available=True,
                ),
                WatchState(
                    media_item_id=episode.id,
                    source_id=source_id,
                    view_count=1,
                    last_watched_at=NOW,
                    completed=True,
                    observed_at=NOW,
                ),
                WatchEvent(
                    media_item_id=episode.id,
                    source_id=source_id,
                    source_event_id="episode-event-1",
                    dedup_key="episode-dedup-1",
                    watched_at=NOW,
                    completed=True,
                    view_number=1,
                ),
            ]
        )
        db.session.commit()
        show_id = show.id
        episode_id = episode.id
    text = app.test_client().get(f"/media/{show_id}").get_data(as_text=True)
    assert "Temporada 1" in text
    assert "E01" in text
    assert "Space Pilot 3000" in text
    assert "Histórico completo" in text
    assert "Space Pilot 3000</strong>" in text
    assert "reprodução 1" in text
    assert "Disponível no Plex" in text

    client = app.test_client()
    episode_detail = client.get(f"/media/{episode_id}").get_data(as_text=True)
    dashboard = client.get("/").get_data(as_text=True)
    history = client.get("/history?kind=episode").get_data(as_text=True)
    catalog = client.get("/catalog?kind=episode").get_data(as_text=True)
    assert "Futurama" in episode_detail and "Space Pilot 3000" in episode_detail
    assert 'aria-label="Episódio"' in dashboard and "Futurama" in dashboard
    assert 'aria-label="Episódio"' in history and "Futurama" in history
    assert 'class="media-type-tab is-episode">Episódio' in catalog
    assert "Futurama" in catalog and "Space Pilot 3000" in catalog


def test_artist_detail_rolls_up_track_history_with_pagination(app: Flask) -> None:
    with app.app_context():
        source_id, library_id, _, _ = seed_web()
        artist = MediaItem(kind=MediaKind.ARTIST, title="Massive Attack")
        db.session.add(artist)
        db.session.flush()
        album = MediaItem(kind=MediaKind.ALBUM, title="Mezzanine", parent_id=artist.id)
        db.session.add(album)
        db.session.flush()
        track = MediaItem(
            kind=MediaKind.TRACK,
            title="Teardrop",
            parent_id=album.id,
            disc_number=1,
            track_number=3,
        )
        db.session.add(track)
        db.session.flush()
        db.session.add_all(
            [
                SourceMediaRef(
                    source_id=source_id,
                    library_id=library_id,
                    media_item_id=artist.id,
                    external_id="artist-1",
                    last_seen_at=NOW,
                    available=True,
                ),
                SourceMediaRef(
                    source_id=source_id,
                    library_id=library_id,
                    media_item_id=track.id,
                    external_id="track-1",
                    last_seen_at=NOW,
                    available=False,
                    unavailable_since=NOW,
                ),
                WatchState(
                    media_item_id=track.id,
                    source_id=source_id,
                    view_count=51,
                    last_watched_at=NOW,
                    completed=True,
                    observed_at=NOW,
                ),
            ]
        )
        for number in range(1, 52):
            db.session.add(
                WatchEvent(
                    media_item_id=track.id,
                    source_id=source_id,
                    source_event_id=f"track-event-{number}",
                    dedup_key=f"track-dedup-{number}",
                    watched_at=NOW,
                    completed=True,
                    view_number=number,
                )
            )
        db.session.commit()
        artist_id = artist.id

    client = app.test_client()
    first = client.get(f"/media/{artist_id}").get_data(as_text=True)
    assert "Mezzanine" in first
    assert "Teardrop</strong>" in first
    assert "Histórico completo" in first and "(51)" in first
    assert "Próximo histórico" in first
    assert "Removido do Plex" not in first
    assert "Ouvida 51 vezes" in first
    second = client.get(f"/media/{artist_id}?history_page=2").get_data(as_text=True)
    assert "Histórico anterior" in second
    assert "reprodução 1" in second


def test_webhooks_accept_only_completed_events_and_deduplicate(app: Flask) -> None:
    with app.app_context():
        plex_source_id, plex_library_id, media_id, _ = seed_web()
        jellyfin = Source(
            connector_type=ConnectorType.JELLYFIN,
            name="Jellyfin",
            base_url="http://jellyfin",
            secret=json.dumps({"api_key": "key", "user_id": "user-1"}),
            enabled=True,
        )
        db.session.add(jellyfin)
        db.session.flush()
        library = Library(
            source_id=jellyfin.id,
            external_id="jf-lib",
            name="Filmes JF",
            media_type=LibraryMediaType.MOVIE,
            enabled=True,
            available=True,
            discovered_at=NOW,
            last_seen_at=NOW,
        )
        db.session.add(library)
        db.session.flush()
        db.session.add(
            SourceMediaRef(
                source_id=jellyfin.id,
                library_id=library.id,
                media_item_id=media_id,
                external_id="jf-movie",
                last_seen_at=NOW,
                available=True,
            )
        )
        db.session.add_all(
            [
                Setting(key="webhook.plex.token", value="plex-secret"),
                Setting(key="webhook.jellyfin.token", value="jf-secret"),
                Setting(key="plex.user_id", value="plex-user"),
            ]
        )
        db.session.commit()
        original_count = db.session.query(WatchEvent).count()
        jellyfin_source_id = jellyfin.id

    client = app.test_client()
    ignored = client.post(
        "/webhooks/plex/plex-secret",
        data={"payload": json.dumps({"event": "media.play"})},
    )
    assert ignored.status_code == 204
    plex = client.post(
        "/webhooks/plex/plex-secret",
        data={
            "payload": json.dumps(
                {
                    "event": "media.stop",
                    "Account": {"id": "plex-user"},
                    "Metadata": {
                        "ratingKey": "m1",
                        "librarySectionID": "1",
                        "duration": 1000,
                        "viewOffset": 950,
                    },
                }
            )
        },
    )
    assert plex.status_code == 204
    jellyfin_resp = client.post(
        "/webhooks/jellyfin/jf-secret",
        json={
            "NotificationType": "PlaybackStop",
            "PlayedToCompletion": True,
            "ItemId": "jf-movie",
            "UserId": "user-1",
            "UtcTimestamp": "2026-08-05T10:00:00Z",
            "RunTimeTicks": 10_000_000,
            "NotificationId": "notification-1",
        },
    )
    assert jellyfin_resp.status_code == 204
    assert client.post("/webhooks/plex/wrong", data={}).status_code == 404
    with app.app_context():
        assert db.session.query(WatchEvent).count() == original_count + 2
        assert {
            event.origin
            for event in db.session.query(WatchEvent).order_by(WatchEvent.id.desc()).limit(2)
        } == {"webhook"}
        webhook_events = (
            db.session.query(WatchEvent)
            .filter(WatchEvent.origin == "webhook")
            .order_by(WatchEvent.id.desc())
            .limit(2)
            .all()
        )
        assert {event.playback_user for event in webhook_events} == {"plex-user", "user-1"}
        states = db.session.query(WatchState).filter_by(media_item_id=media_id).all()
        assert {state.source_id for state in states} >= {plex_source_id, jellyfin_source_id}
        assert all(state.completed and state.view_count > 0 for state in states)
        assert db.session.get(Source, plex_source_id) is not None
        assert db.session.get(Library, plex_library_id) is not None


def test_plex_stop_verifies_authoritative_watch_state_when_progress_is_stale(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        seed_web()
        db.session.add_all(
            [
                Setting(key="webhook.plex.token", value="plex-secret"),
                Setting(key="plex.user_id", value="plex-user"),
            ]
        )
        db.session.commit()

    watched_at = datetime.now(UTC).replace(microsecond=0)
    connector = object.__new__(PlexConnector)
    monkeypatch.setattr(
        connector,
        "get_media_item",
        lambda external_id, library_external_id: type(
            "StoredItem",
            (),
            {"view_count": 4, "last_viewed_at": watched_at},
        )(),
    )
    monkeypatch.setattr(connector, "close", lambda: None)
    monkeypatch.setattr("euvieouvi.web.routes.connector_for", lambda source: connector)

    response = app.test_client().post(
        "/webhooks/plex/plex-secret",
        data={
            "payload": json.dumps(
                {
                    "event": "media.stop",
                    "eventTime": watched_at.timestamp(),
                    "Account": {"id": "plex-user"},
                    "Metadata": {
                        "ratingKey": "m1",
                        "librarySectionID": "1",
                        "duration": 1000,
                        "viewOffset": 880,
                    },
                }
            )
        },
    )

    assert response.status_code == 204
    with app.app_context():
        event = db.session.query(WatchEvent).filter_by(origin="webhook").one()
        state = db.session.query(WatchState).filter_by(media_item_id=event.media_item_id).one()
        assert event.view_number == 4 and event.playback_user == "plex-user"
        assert state.completed is True and state.view_count == 4


def test_webhook_settings_generate_secret_urls(app: Flask) -> None:
    response = app.test_client().get("/settings/webhooks")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "/webhooks/plex/" in text and "/webhooks/jellyfin/" in text
    with app.app_context():
        assert db.session.get(Setting, "webhook.plex.token") is not None


def test_webhook_page_shows_current_activity_and_respects_retention(app: Flask) -> None:
    with app.app_context():
        seed_web()
        db.session.add(Setting(key="webhook.plex.token", value="plex-secret"))
        db.session.commit()
    client = app.test_client()
    play_payload = {
        "event": "media.play",
        "Metadata": {
            "ratingKey": "m1",
            "librarySectionID": "1",
            "title": "Pilot",
            "type": "episode",
            "grandparentTitle": "Example Series",
            "viewOffset": 45_000,
            "duration": 100_000,
        },
    }
    assert (
        client.post(
            "/webhooks/plex/plex-secret", data={"payload": json.dumps(play_payload)}
        ).status_code
        == 204
    )
    assert "Atividade atual" not in client.get("/settings/webhooks").get_data(as_text=True)
    page = client.get("/").get_data(as_text=True)
    assert (
        page.index("Resumo do catálogo")
        < page.index("Atividade atual")
        < page.index("Última sincronização")
    )
    assert "Em reprodução no Plex" in page and "Pilot" in page
    assert "45% reproduzido" in page
    assert "Example Series" in page
    assert 'aria-label="Episódio"' in page
    assert 'hx-trigger="every 2s"' in page
    fragment = client.get("/settings/webhooks/activity-fragment")
    assert fragment.status_code == 200 and "Pilot" in fragment.get_data(as_text=True)

    token = csrf(client, "/settings/webhooks")
    assert (
        client.post(
            "/settings/webhooks", data={"csrf_token": token, "history_limit": "1"}
        ).status_code
        == 302
    )
    for event_id in ("one", "two"):
        payload = {
            "event": "media.scrobble",
            "event_id": event_id,
            "Metadata": {"ratingKey": "m1", "librarySectionID": "1", "title": event_id},
        }
        assert (
            client.post(
                "/webhooks/plex/plex-secret", data={"payload": json.dumps(payload)}
            ).status_code
            == 204
        )
    with app.app_context():
        assert db.session.query(WebhookEvent).filter_by(completed=True).count() == 1


def test_jellyfin_completed_unmapped_item_commits_before_queuing_sync(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app.app_context():
        jellyfin = Source(
            connector_type=ConnectorType.JELLYFIN,
            name="Jellyfin",
            base_url="http://jellyfin",
            secret=json.dumps({"api_key": "key", "user_id": "user-1"}),
            enabled=True,
        )
        db.session.add_all([jellyfin, Setting(key="webhook.jellyfin.token", value="jf-secret")])
        db.session.commit()
        source_id = jellyfin.id

    calls: list[str] = []

    class Executor:
        def submit(self, submitted_source_id: int) -> int:
            assert submitted_source_id == source_id
            assert not db.session().in_transaction()
            calls.append("sync")
            return 1

        def submit_pending_watch_sync(self) -> bool:
            calls.append("propagate")
            return True

    monkeypatch.setattr("euvieouvi.web.routes.get_executor", lambda app: Executor())
    response = app.test_client().post(
        "/webhooks/jellyfin/jf-secret",
        data={
            "NotificationType": "PlaybackStop",
            "PlayedToCompletion": "True",
            "ItemId": "unmapped-episode",
            "Name": "Episode",
            "ItemType": "Episode",
            "UserId": "user-1",
            "PlaybackPositionTicks": "999999",
            "RunTimeTicks": "1000000",
        },
    )

    assert response.status_code == 204
    assert calls == ["sync"]
    with app.app_context():
        event = db.session.query(WebhookEvent).filter_by(external_id="unmapped-episode").one()
        assert event.completed is True and event.active is False
        pending = db.session.get(Setting, "watch_sync.pending")
        assert pending is None


def test_plex_activity_shows_all_users_but_completion_uses_filter(app: Flask) -> None:
    with app.app_context():
        seed_web()
        db.session.add_all(
            [
                Setting(key="webhook.plex.token", value="plex-secret"),
                Setting(key="webhook.plex.user_filter", value="alice"),
            ]
        )
        db.session.commit()
        original_watch_count = db.session.query(WatchEvent).count()

    client = app.test_client()

    def send(event: str, user: str, rating_key: str, title: str) -> None:
        payload = {
            "event": event,
            "Account": {"id": 10 if user == "alice" else 20, "title": user},
            "Metadata": {
                "ratingKey": rating_key,
                "librarySectionID": "1",
                "title": title,
                "type": "movie",
            },
        }
        assert (
            client.post(
                "/webhooks/plex/plex-secret",
                data={"payload": json.dumps(payload)},
            ).status_code
            == 204
        )

    send("media.play", "alice", "m1", "Alice Movie")
    send("media.play", "bob", "m2", "Bob Movie")
    home = client.get("/").get_data(as_text=True)
    assert "Alice Movie" in home and "por alice" in home
    assert "Bob Movie" in home and "por bob" in home

    send("media.scrobble", "bob", "m1", "Ignored completion")
    with app.app_context():
        assert db.session.query(WatchEvent).count() == original_watch_count
        assert db.session.query(WebhookEvent).filter_by(completed=True).count() == 0

    send("media.scrobble", "alice", "m1", "Accepted completion")
    with app.app_context():
        assert db.session.query(WatchEvent).count() == original_watch_count + 1
        completed = db.session.query(WebhookEvent).filter_by(completed=True).one()
        assert completed.playback_user == "alice"


def test_jellyfin_progress_creates_and_updates_current_activity(app: Flask) -> None:
    with app.app_context():
        jellyfin = Source(
            connector_type=ConnectorType.JELLYFIN,
            name="Jellyfin",
            base_url="http://jellyfin",
            secret=json.dumps({"api_key": "key", "user_id": "user-1"}),
            enabled=True,
        )
        db.session.add_all([jellyfin, Setting(key="webhook.jellyfin.token", value="jf-secret")])
        db.session.commit()

    client = app.test_client()
    payload = {
        "NotificationType": "PlaybackProgress",
        "NotificationId": "progress-1",
        "ItemId": "jf-episode",
        "ItemName": "Epis&#243;dio 2",
        "ItemType": "Episode",
        "SeriesName": "S&#233;rie de Teste",
        "UserId": "6c1f52e4-3b42-4d87-a80d-b324215c93ad",
        "NotificationUsername": "user-1",
        "PlaybackPositionTicks": 3_000_000,
        "RunTimeTicks": 10_000_000,
    }
    assert client.post("/webhooks/jellyfin/jf-secret", data=payload).status_code == 204
    page = client.get("/settings/webhooks/activity-fragment").get_data(as_text=True)
    assert "Em reprodução no Jellyfin" in page
    assert "Série de Teste" in page and "Episódio 2" in page
    assert "&#243;" not in page and "&#233;" not in page
    assert "30% reproduzido" in page

    payload["NotificationId"] = "progress-2"
    payload["PlaybackPositionTicks"] = 7_500_000
    assert client.post("/webhooks/jellyfin/jf-secret", json=payload).status_code == 204
    page = client.get("/settings/webhooks/activity-fragment").get_data(as_text=True)
    assert "75% reproduzido" in page

    payload.update(
        ItemId="jf-next-episode",
        ItemName="Episódio 3",
        NotificationId="progress-3",
        PlaybackPositionTicks=500_000,
    )
    assert client.post("/webhooks/jellyfin/jf-secret", data=payload).status_code == 204
    with app.app_context():
        active = db.session.query(WebhookEvent).filter_by(active=True).one()
        assert active.external_id == "jf-next-episode"
    with app.app_context():
        assert db.session.query(WebhookEvent).filter_by(active=True).count() == 1


def test_webhook_page_deactivates_activity_older_than_one_hour(app: Flask) -> None:
    with app.app_context():
        source_id, _, _, _ = seed_web()
        db.session.add(
            WebhookEvent(
                source_id=source_id,
                external_id="stale",
                title="Stale playback",
                media_kind="movie",
                event_type="media.play",
                occurred_at=datetime.now(UTC) - timedelta(hours=1, seconds=1),
                completed=False,
                active=True,
            )
        )
        db.session.commit()

    response = app.test_client().get("/settings/webhooks/activity-fragment")
    assert response.status_code == 200
    assert "Stale playback" not in response.get_data(as_text=True)
    with app.app_context():
        event = db.session.scalar(select(WebhookEvent).where(WebhookEvent.external_id == "stale"))
        assert event is not None and event.active is False


def test_jellyfin_settings_validation_save_update_and_test(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = app.test_client()
    token = csrf(client, "/settings/jellyfin")
    invalid = client.post(
        "/settings/jellyfin",
        data={"csrf_token": token, "name": "", "base_url": "ftp://bad"},
    )
    invalid_text = invalid.get_data(as_text=True)
    assert "API key é obrigatória" in invalid_text
    assert "URL HTTP ou HTTPS válida" in invalid_text

    token = csrf(client, "/settings/jellyfin")
    saved = client.post(
        "/settings/jellyfin",
        data={
            "csrf_token": token,
            "name": "Jellyfin",
            "base_url": "http://jellyfin.local:8096",
            "api_key": "api-key",
            "user_id": "user-1",
            "enabled": "on",
        },
    )
    assert saved.status_code == 302
    with app.app_context():
        source = db.session.scalar(
            db.select(Source).where(Source.connector_type == ConnectorType.JELLYFIN)
        )
        assert source is not None
        assert json.loads(source.secret)["api_key"] == "api-key"

    token = csrf(client, "/settings/jellyfin")
    updated = client.post(
        "/settings/jellyfin",
        data={
            "csrf_token": token,
            "name": "Jellyfin Casa",
            "base_url": "http://jellyfin.local:8096",
            "api_key": "",
            "user_id": "",
            "enabled": "on",
        },
    )
    assert updated.status_code == 302
    monkeypatch.setattr("euvieouvi.web.routes.connector_for", lambda source: WebConnector())
    token = csrf(client, "/settings/jellyfin")
    tested = client.post(
        "/settings/jellyfin/test",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert "Conexão com o Jellyfin realizada" in tested.get_data(as_text=True)


def test_webhooks_reject_malformed_or_irrelevant_payloads(app: Flask) -> None:
    with app.app_context():
        db.session.add_all(
            [
                Setting(key="webhook.plex.token", value="plex-secret"),
                Setting(key="webhook.jellyfin.token", value="jf-secret"),
            ]
        )
        db.session.commit()
    client = app.test_client()
    assert client.post("/webhooks/plex/plex-secret", data={"payload": "{"}).status_code == 204
    with app.app_context():
        source = Source(
            connector_type=ConnectorType.PLEX,
            name="Plex",
            base_url="http://plex",
            secret="token",
            enabled=True,
        )
        jellyfin = Source(
            connector_type=ConnectorType.JELLYFIN,
            name="Jellyfin",
            base_url="http://jellyfin",
            secret=json.dumps({"api_key": "key", "user_id": "user-1"}),
            enabled=True,
        )
        db.session.add_all([source, jellyfin])
        db.session.commit()
    assert client.post("/webhooks/plex/plex-secret", data={"payload": "{"}).status_code == 400
    assert (
        client.post(
            "/webhooks/plex/plex-secret",
            data={"payload": json.dumps({"event": "media.scrobble"})},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/webhooks/jellyfin/jf-secret",
            json={"NotificationType": "PlaybackStop", "PlayedToCompletion": False},
        ).status_code
        == 204
    )
    assert client.post("/webhooks/jellyfin/wrong", json={}).status_code == 404
    assert (
        client.post(
            "/webhooks/plex/plex-secret",
            data={
                "payload": json.dumps({"event": "media.scrobble", "Metadata": {"ratingKey": "m1"}})
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/webhooks/jellyfin/jf-secret",
            json={
                "NotificationType": "PlaybackStop",
                "PlayedToCompletion": True,
                "ItemId": "missing",
                "UserId": "other-user",
                "UtcTimestamp": "invalid",
            },
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/webhooks/jellyfin/jf-secret",
            json={
                "NotificationType": "PlaybackStop",
                "PlayedToCompletion": "true",
                "ItemId": "missing",
                "UserId": "user-1",
                "UtcTimestamp": "invalid",
            },
        ).status_code
        == 400
    )


def test_jellyfin_test_handles_missing_and_failed_connection(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = app.test_client()
    token = csrf(client, "/settings/jellyfin")
    missing = client.post(
        "/settings/jellyfin/test", data={"csrf_token": token}, follow_redirects=True
    )
    assert "Salve a configuração" in missing.get_data(as_text=True)
    with app.app_context():
        db.session.add(
            Source(
                connector_type=ConnectorType.JELLYFIN,
                name="Jellyfin",
                base_url="http://jellyfin",
                secret="invalid",
                enabled=True,
            )
        )
        db.session.commit()
    monkeypatch.setattr(
        "euvieouvi.web.routes.connector_for",
        lambda source: (_ for _ in ()).throw(ValueError("bad credentials")),
    )
    token = csrf(client, "/settings/jellyfin")
    failed = client.post(
        "/settings/jellyfin/test", data={"csrf_token": token}, follow_redirects=True
    )
    assert "não respondeu" in failed.get_data(as_text=True)
    with app.app_context():
        source = db.session.scalar(db.select(Source))
        assert source is not None and source.last_connection_status == "failed"
