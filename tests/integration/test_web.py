"""Web interface flows, HTMX fallback, CSRF and safe presentation."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
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
    Library,
    MediaGenre,
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


def test_daily_schedule_settings_are_persisted(app: Flask) -> None:
    client = app.test_client()
    token = csrf(client, "/settings/sync")
    response = client.post(
        "/settings/sync",
        data={"csrf_token": token, "enabled": "on", "scheduled_time": "04:30"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Agendamento diário atualizado" in response.get_data(as_text=True)
    with app.app_context():
        from euvieouvi.database.models import Setting

        enabled = db.session.get(Setting, "sync.schedule.enabled")
        scheduled_time = db.session.get(Setting, "sync.schedule.time")
        assert enabled is not None and enabled.value == "true"
        assert scheduled_time is not None and scheduled_time.value == "04:30"


def test_daily_schedule_rejects_invalid_time(app: Flask) -> None:
    client = app.test_client()
    token = csrf(client, "/settings/sync")
    response = client.post(
        "/settings/sync",
        data={"csrf_token": token, "scheduled_time": "99:00"},
    )
    assert response.status_code == 200
    assert "horário válido" in response.get_data(as_text=True)


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
        active = False

        def submit(self) -> bool:
            return True

    monkeypatch.setattr("euvieouvi.web.routes.get_enrichment_executor", lambda app: Executor())
    token = csrf(client, "/settings/metadata")
    started = client.post("/metadata/enrich", data={"csrf_token": token}, follow_redirects=True)
    assert "Enriquecimento iniciado" in started.get_data(as_text=True)


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
        ("/libraries", "Bibliotecas"),
        ("/sync", "Sincronizações"),
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
    sync = client.get(f"/sync/{run_id}").get_data(as_text=True)
    assert "Mensagem segura" in sync and "Bibliotecas" in sync
    assert client.get("/sync/active-fragment").status_code == 200
    assert (
        client.get("/media/999").status_code == 404 and client.get("/sync/999").status_code == 404
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
    assert "Assistido 1 vez" in text
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


def test_media_image_placeholder_and_local_cache(
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
    assert calls == [f"/library/metadata/{movie_id}/thumb"]


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
    text = app.test_client().get(f"/media/{show_id}").get_data(as_text=True)
    assert "Temporada 1" in text
    assert "E01" in text
    assert "Space Pilot 3000" in text
    assert "Histórico completo" in text
    assert "Space Pilot 3000</strong>" in text
    assert "reprodução 1" in text
    assert "Disponível no Plex" in text


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
            ]
        )
        db.session.commit()
        original_count = db.session.query(WatchEvent).count()

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
                    "event": "media.scrobble",
                    "Metadata": {
                        "ratingKey": "m1",
                        "librarySectionID": "1",
                        "duration": 1000,
                    },
                }
            )
        },
    )
    assert plex.status_code == 204
    jellyfin = client.post(
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
    assert jellyfin.status_code == 204
    assert client.post("/webhooks/plex/wrong", data={}).status_code == 404
    with app.app_context():
        assert db.session.query(WatchEvent).count() == original_count + 2
        assert db.session.get(Source, plex_source_id) is not None
        assert db.session.get(Library, plex_library_id) is not None


def test_webhook_settings_generate_secret_urls(app: Flask) -> None:
    response = app.test_client().get("/settings/webhooks")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "/webhooks/plex/" in text and "/webhooks/jellyfin/" in text
    with app.app_context():
        assert db.session.get(Setting, "webhook.plex.token") is not None


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
