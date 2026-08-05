"""Jinja pages and HTMX fragments for the local interface."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError

from euvieouvi.api.runtime import connector_for, get_executor
from euvieouvi.api.validation import http_url
from euvieouvi.connectors.dtos import ExternalWatchEvent
from euvieouvi.connectors.errors import ConnectorError
from euvieouvi.database.enums import ConnectorType, MediaKind, SyncStatus
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
)
from euvieouvi.database.unit_of_work import UnitOfWork
from euvieouvi.enrichment.runtime import get_enrichment_executor
from euvieouvi.errors import AppError
from euvieouvi.extensions import db
from euvieouvi.media_images import ensure_cached, ensure_external_cached
from euvieouvi.sync.discovery import LibraryDiscoveryService
from euvieouvi.sync.errors import SyncAlreadyRunningError, SyncSourceUnavailableError
from euvieouvi.sync.persistence import MediaPersistenceService
from euvieouvi.web.formatting import duration_ms, local_datetime

blueprint = Blueprint("web", __name__)


@blueprint.app_context_processor
def template_helpers() -> dict[str, Any]:
    active = db.session.scalar(
        select(SyncRun)
        .where(SyncRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
        .order_by(SyncRun.id.desc())
    )
    return {
        "app_version": version("euvieouvi"),
        "active_sync": active,
        "format_datetime": local_datetime,
        "format_duration": duration_ms,
    }


def _source(connector_type: ConnectorType | None = None) -> Source | None:
    statement = select(Source)
    if connector_type is not None:
        statement = statement.where(Source.connector_type == connector_type)
    return db.session.scalar(statement.order_by(Source.id))


def _htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


@blueprint.get("/")
def dashboard() -> Any:
    source = _source()
    libraries = db.session.scalars(select(Library).order_by(Library.name)).all()
    last_run = db.session.scalar(
        select(SyncRun).order_by(SyncRun.created_at.desc(), SyncRun.id.desc())
    )
    recent = db.session.execute(
        select(WatchEvent, MediaItem)
        .join(MediaItem, MediaItem.id == WatchEvent.media_item_id)
        .where(WatchEvent.completed.is_(True))
        .order_by(WatchEvent.watched_at.desc(), WatchEvent.id.desc())
        .limit(8)
    ).all()
    counts = {
        "movies": _count(MediaItem, MediaItem.kind == MediaKind.MOVIE),
        "shows": _count(MediaItem, MediaItem.kind == MediaKind.SHOW),
        "episodes": _count(MediaItem, MediaItem.kind == MediaKind.EPISODE),
        "watched_movies": _watched_count(MediaKind.MOVIE),
        "watched_episodes": _watched_count(MediaKind.EPISODE),
        "artists": _count(MediaItem, MediaItem.kind == MediaKind.ARTIST),
        "albums": _count(MediaItem, MediaItem.kind == MediaKind.ALBUM),
        "tracks": _count(MediaItem, MediaItem.kind == MediaKind.TRACK),
        "listened_tracks": _watched_count(MediaKind.TRACK),
    }
    next_step = None
    if source is None:
        next_step = ("Configure seu servidor Plex", "settings_plex")
    elif source.last_connection_status != "succeeded":
        next_step = ("Teste a conexão com o Plex", "settings_plex")
    elif not libraries:
        next_step = ("Descubra suas bibliotecas", "libraries")
    elif not any(item.enabled and item.available for item in libraries):
        next_step = ("Selecione ao menos uma biblioteca", "libraries")
    elif last_run is None:
        next_step = ("Execute a primeira sincronização", "sync_list")
    return render_template(
        "dashboard.html",
        counts=counts,
        source=source,
        next_step=next_step,
        last_run=last_run,
        recent=recent,
    )


@blueprint.get("/setup")
def setup() -> Any:
    return redirect(url_for("web.settings_plex"))


@blueprint.route("/settings/plex", methods=["GET", "POST"])
def settings_plex() -> Any:
    source = _source(ConnectorType.PLEX)
    errors: dict[str, str] = {}
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        base_url = request.form.get("base_url", "").strip()
        secret = request.form.get("secret", "")
        enabled = request.form.get("enabled") == "on"
        if not name or len(name) > 255:
            errors["name"] = "Informe um nome com até 255 caracteres."
        try:
            normalized_url = http_url(base_url)
        except AppError:
            errors["base_url"] = "Informe uma URL HTTP ou HTTPS válida, sem credenciais."
            normalized_url = base_url
        if source is None and not secret.strip():
            errors["secret"] = "O token Plex é obrigatório no primeiro cadastro."
        if not errors:
            if source is None:
                source = Source(
                    connector_type=ConnectorType.PLEX,
                    name=name,
                    base_url=normalized_url,
                    secret=secret.strip(),
                    enabled=enabled,
                )
                db.session.add(source)
            else:
                source.name = name
                source.base_url = normalized_url
                source.enabled = enabled
                if secret.strip():
                    source.secret = secret.strip()
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                errors["name"] = "Já existe uma fonte com este nome."
            else:
                flash("Configuração do Plex salva com segurança.", "success")
                return redirect(url_for("web.settings_plex"))
    return render_template("settings_plex.html", source=source, errors=errors)


@blueprint.route("/settings/jellyfin", methods=["GET", "POST"])
def settings_jellyfin() -> Any:
    source = _source(ConnectorType.JELLYFIN)
    errors: dict[str, str] = {}
    persisted: dict[str, str] = {}
    if source is not None:
        try:
            raw = json.loads(source.secret)
            if isinstance(raw, dict):
                persisted = {key: str(value) for key, value in raw.items()}
        except (TypeError, ValueError, json.JSONDecodeError):
            persisted = {}
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        base_url = request.form.get("base_url", "").strip()
        api_key = request.form.get("api_key", "").strip()
        user_id = request.form.get("user_id", "").strip()
        enabled = request.form.get("enabled") == "on"
        if not name or len(name) > 255:
            errors["name"] = "Informe um nome com até 255 caracteres."
        try:
            normalized_url = http_url(base_url)
        except AppError:
            errors["base_url"] = "Informe uma URL HTTP ou HTTPS válida."
            normalized_url = base_url
        if source is None and not api_key:
            errors["api_key"] = "A API key é obrigatória no primeiro cadastro."
        if not user_id and not persisted.get("user_id"):
            errors["user_id"] = "Informe o ID do usuário Jellyfin acompanhado."
        if not errors:
            credentials = {
                "api_key": api_key or persisted.get("api_key", ""),
                "user_id": user_id or persisted.get("user_id", ""),
            }
            if source is None:
                source = Source(
                    connector_type=ConnectorType.JELLYFIN,
                    name=name,
                    base_url=normalized_url,
                    secret=json.dumps(credentials),
                    enabled=enabled,
                )
                db.session.add(source)
            else:
                source.name = name
                source.base_url = normalized_url
                source.secret = json.dumps(credentials)
                source.enabled = enabled
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                errors["name"] = "Já existe uma fonte com este nome."
            else:
                flash("Configuração do Jellyfin salva com segurança.", "success")
                return redirect(url_for("web.settings_jellyfin"))
    return render_template(
        "settings_jellyfin.html",
        source=source,
        errors=errors,
        persisted=persisted,
    )


@blueprint.route("/settings/sync", methods=["GET", "POST"])
def settings_sync() -> Any:
    values = _settings(
        "sync.schedule.enabled",
        "sync.schedule.time",
    )
    errors: dict[str, str] = {}
    if request.method == "POST":
        enabled = request.form.get("enabled") == "on"
        scheduled_time = request.form.get("scheduled_time", "").strip()
        try:
            parsed = datetime.strptime(scheduled_time, "%H:%M")
        except ValueError:
            errors["scheduled_time"] = "Informe um horário válido entre 00:00 e 23:59."
        if not errors:
            _save_setting("sync.schedule.enabled", "true" if enabled else "false")
            _save_setting("sync.schedule.time", parsed.strftime("%H:%M"))
            db.session.commit()
            flash("Agendamento diário atualizado.", "success")
            return redirect(url_for("web.settings_sync"))
    return render_template(
        "settings_sync.html",
        enabled=(
            request.form.get("enabled") == "on"
            if request.method == "POST"
            else values.get("sync.schedule.enabled", "false") == "true"
        ),
        scheduled_time=request.form.get(
            "scheduled_time", values.get("sync.schedule.time", "03:00")
        ),
        timezone=current_app.config["TIMEZONE"],
        errors=errors,
    )


@blueprint.route("/settings/metadata", methods=["GET", "POST"])
def settings_metadata() -> Any:
    keys = (
        "metadata.tmdb.enabled",
        "metadata.tmdb.token",
        "metadata.musicbrainz.enabled",
        "metadata.auto_after_sync",
        "metadata.language",
        "metadata.last_summary",
    )
    values = _settings(*keys)
    if request.method == "POST":
        tmdb_enabled = request.form.get("tmdb_enabled") == "on"
        token = request.form.get("tmdb_token", "").strip()
        if tmdb_enabled and not token and not values.get("metadata.tmdb.token"):
            flash("Informe o token de leitura do TMDB para ativar a integração.", "danger")
        else:
            _save_setting("metadata.tmdb.enabled", "true" if tmdb_enabled else "false")
            if token:
                _save_setting("metadata.tmdb.token", token)
            _save_setting(
                "metadata.musicbrainz.enabled",
                "true" if request.form.get("musicbrainz_enabled") == "on" else "false",
            )
            _save_setting(
                "metadata.auto_after_sync",
                "true" if request.form.get("auto_after_sync") == "on" else "false",
            )
            language = request.form.get("language", "pt-BR")
            _save_setting(
                "metadata.language",
                language if language in {"pt-BR", "en-US"} else "pt-BR",
            )
            db.session.commit()
            flash("Configuração de metadados atualizada.", "success")
            return redirect(url_for("web.settings_metadata"))
    return render_template(
        "settings_metadata.html",
        values=values,
        enrichment_active=get_enrichment_executor(current_app).active,
    )


@blueprint.post("/metadata/enrich")
def metadata_enrich() -> Any:
    if get_enrichment_executor(current_app).submit():
        flash("Enriquecimento iniciado em segundo plano.", "success")
    else:
        flash("O enriquecimento já está em execução.", "warning")
    return redirect(url_for("web.settings_metadata"))


@blueprint.post("/settings/plex/test")
def settings_plex_test() -> Any:
    source = _source(ConnectorType.PLEX)
    if source is None:
        flash("Salve a configuração antes de testar.", "warning")
        return redirect(url_for("web.settings_plex"))
    try:
        info = connector_for(source).test_connection()
        source.last_connection_status = "succeeded"
        source.last_connection_test_at = datetime.now(UTC)
        db.session.commit()
        flash(f"Conexão com o Plex realizada com sucesso ({info.server_name}).", "success")
    except (ConnectorError, OSError):
        source.last_connection_status = "failed"
        source.last_connection_test_at = datetime.now(UTC)
        db.session.commit()
        flash("O Plex não respondeu ao teste. Verifique URL, token e disponibilidade.", "danger")
    return redirect(url_for("web.settings_plex"))


@blueprint.post("/settings/jellyfin/test")
def settings_jellyfin_test() -> Any:
    source = _source(ConnectorType.JELLYFIN)
    if source is None:
        flash("Salve a configuração antes de testar.", "warning")
        return redirect(url_for("web.settings_jellyfin"))
    try:
        info = connector_for(source).test_connection()
        source.last_connection_status = "succeeded"
        source.last_connection_test_at = datetime.now(UTC)
        db.session.commit()
        flash(f"Conexão com o Jellyfin realizada com sucesso ({info.server_name}).", "success")
    except (ConnectorError, OSError, ValueError):
        source.last_connection_status = "failed"
        source.last_connection_test_at = datetime.now(UTC)
        db.session.commit()
        flash("O Jellyfin não respondeu. Verifique URL, API key e usuário.", "danger")
    return redirect(url_for("web.settings_jellyfin"))


@blueprint.get("/settings/webhooks")
def settings_webhooks() -> Any:
    tokens = _settings("webhook.plex.token", "webhook.jellyfin.token")
    changed = False
    for provider in ("plex", "jellyfin"):
        key = f"webhook.{provider}.token"
        if not tokens.get(key):
            tokens[key] = secrets.token_urlsafe(32)
            _save_setting(key, tokens[key])
            changed = True
    if changed:
        db.session.commit()
    return render_template(
        "settings_webhooks.html",
        plex_url=url_for("web.plex_webhook", token=tokens["webhook.plex.token"], _external=True),
        jellyfin_url=url_for(
            "web.jellyfin_webhook", token=tokens["webhook.jellyfin.token"], _external=True
        ),
    )


@blueprint.post("/webhooks/plex/<token>")
def plex_webhook(token: str) -> Any:
    if not _valid_webhook_token("plex", token):
        return Response(status=404)
    source = _source(ConnectorType.PLEX)
    payload_text = request.form.get("payload")
    if source is None or not payload_text:
        return Response(status=204)
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return Response("Payload inválido.", 400)
    if not isinstance(payload, dict) or payload.get("event") != "media.scrobble":
        return Response(status=204)
    metadata = payload.get("Metadata")
    if not isinstance(metadata, dict):
        return Response("Metadata ausente.", 400)
    external_id = str(metadata.get("ratingKey") or "").strip()
    library_external_id = str(metadata.get("librarySectionID") or "").strip()
    if not external_id or not library_external_id:
        return Response("Identidade da mídia ausente.", 400)
    _persist_webhook_event(
        source,
        external_id=external_id,
        library_external_id=library_external_id,
        watched_at=datetime.now(UTC),
        source_event_id=None,
        duration_ms=_safe_integer(metadata.get("duration")),
    )
    return Response(status=204)


@blueprint.post("/webhooks/jellyfin/<token>")
def jellyfin_webhook(token: str) -> Any:
    if not _valid_webhook_token("jellyfin", token):
        return Response(status=404)
    source = _source(ConnectorType.JELLYFIN)
    payload = request.get_json(silent=True)
    if source is None or not isinstance(payload, dict):
        return Response(status=204)
    if payload.get("NotificationType") != "PlaybackStop" or not _truthy(
        payload.get("PlayedToCompletion")
    ):
        return Response(status=204)
    credentials = _jellyfin_credentials(source)
    user_id = str(payload.get("UserId") or "").strip()
    if credentials.get("user_id") and user_id != credentials["user_id"]:
        return Response(status=204)
    external_id = str(payload.get("ItemId") or "").strip()
    watched_at = _parse_webhook_datetime(payload.get("UtcTimestamp"))
    if not external_id or watched_at is None:
        return Response("Identidade ou data ausente.", 400)
    reference = db.session.scalar(
        select(SourceMediaRef).where(
            SourceMediaRef.source_id == source.id,
            SourceMediaRef.external_id == external_id,
        )
    )
    if reference is None:
        _queue_source_sync(source.id)
        return Response(status=202)
    library = db.session.get(Library, reference.library_id)
    if library is None:
        return Response(status=204)
    _persist_webhook_event(
        source,
        external_id=external_id,
        library_external_id=library.external_id,
        watched_at=watched_at,
        source_event_id=str(payload.get("NotificationId") or "").strip() or None,
        duration_ms=_ticks_to_ms(payload.get("RunTimeTicks")),
    )
    return Response(status=204)


@blueprint.get("/libraries")
def libraries() -> Any:
    sources = db.session.scalars(select(Source).order_by(Source.name)).all()
    values = db.session.scalars(select(Library).order_by(Library.name, Library.id)).all()
    return render_template("libraries.html", sources=sources, libraries=values)


@blueprint.post("/libraries/<int:source_id>/discover")
def libraries_discover(source_id: int) -> Any:
    source = db.session.get(Source, source_id)
    if source is None:
        flash("Configure a fonte antes de descobrir bibliotecas.", "warning")
    else:
        try:
            count = LibraryDiscoveryService(lambda: db.session(), connector_for(source)).discover(
                source.id
            )
            flash(f"Descoberta concluída: {count} biblioteca(s) compatível(is).", "success")
        except (ConnectorError, SyncSourceUnavailableError):
            flash(
                "Não foi possível atualizar as bibliotecas. A lista anterior foi preservada.",
                "danger",
            )
    return redirect(url_for("web.libraries"))


@blueprint.post("/libraries/discover")
def libraries_discover_legacy() -> Any:
    """Keep old bookmarks and deployment instructions working for Plex."""
    source = _source(ConnectorType.PLEX) or _source()
    if source is None:
        flash("Configure a fonte antes de descobrir bibliotecas.", "warning")
        return redirect(url_for("web.libraries"))
    return libraries_discover(source.id)


@blueprint.post("/libraries/<int:library_id>/selection")
def library_selection(library_id: int) -> Any:
    library = db.session.get(Library, library_id)
    if library is None:
        return Response("Biblioteca não encontrada.", 404)
    enabled = request.form.get("enabled") == "true"
    if enabled and not library.available:
        flash("Uma biblioteca indisponível não pode ser selecionada.", "warning")
    else:
        library.enabled = enabled
        db.session.commit()
        flash("Seleção da biblioteca atualizada.", "success")
    if _htmx():
        return render_template("fragments/library_row.html", library=library)
    return redirect(url_for("web.libraries"))


@blueprint.route("/sync", methods=["GET", "POST"])
def sync_list() -> Any:
    if request.method == "POST":
        source_ids = tuple(
            db.session.scalars(
                select(Source.id)
                .where(
                    Source.enabled.is_(True),
                    exists().where(
                        Library.source_id == Source.id,
                        Library.enabled.is_(True),
                        Library.available.is_(True),
                    ),
                )
                .order_by(Source.id)
            ).all()
        )
        if not source_ids:
            flash("Selecione ao menos uma biblioteca disponível antes de sincronizar.", "warning")
            return redirect(url_for("web.libraries"))
        try:
            executor = get_executor(current_app)
            submit_all = getattr(executor, "submit_all", None)
            run_id = (
                submit_all(source_ids) if callable(submit_all) else executor.submit(source_ids[0])
            )
        except (SyncAlreadyRunningError, SyncSourceUnavailableError):
            flash("Uma sincronização já está ativa ou a fonte está indisponível.", "warning")
            return redirect(url_for("web.sync_list"))
        flash("Sincronização das fontes iniciada em segundo plano.", "success")
        return redirect(url_for("web.sync_detail", run_id=run_id))
    runs = db.session.scalars(
        select(SyncRun).order_by(SyncRun.created_at.desc(), SyncRun.id.desc()).limit(50)
    ).all()
    return render_template("sync_list.html", runs=runs)


@blueprint.get("/sync/active-fragment")
def sync_active_fragment() -> Any:
    active = db.session.scalar(
        select(SyncRun)
        .where(SyncRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
        .order_by(SyncRun.id.desc())
    )
    return render_template("fragments/sync_active.html", run=active)


@blueprint.get("/sync/<int:run_id>")
def sync_detail(run_id: int) -> Any:
    run = db.session.get(SyncRun, run_id)
    if run is None:
        return render_template("errors/404.html"), 404
    libraries = db.session.scalars(
        select(SyncRunLibrary)
        .where(SyncRunLibrary.sync_run_id == run_id)
        .order_by(SyncRunLibrary.id)
    ).all()
    errors = db.session.scalars(
        select(SyncError).where(SyncError.sync_run_id == run_id).order_by(SyncError.id).limit(100)
    ).all()
    return render_template("sync_detail.html", run=run, libraries=libraries, errors=errors)


@blueprint.post("/sync/<int:run_id>/cancel")
def sync_cancel(run_id: int) -> Any:
    run = db.session.get(SyncRun, run_id)
    if run is None:
        return Response("Sincronização não encontrada.", 404)
    if run.status not in {SyncStatus.QUEUED, SyncStatus.RUNNING}:
        flash("Esta sincronização já terminou.", "warning")
    else:
        get_executor(current_app).cancel(run_id)
        flash("Cancelamento solicitado. Os dados já confirmados serão preservados.", "warning")
    return redirect(url_for("web.sync_detail", run_id=run_id))


@blueprint.get("/history")
def history() -> Any:
    query = request.args.get("query", "").strip()[:200]
    kind = request.args.get("kind", "")
    watched = request.args.get("watched", "watched")
    completed_event = exists().where(
        WatchEvent.media_item_id == MediaItem.id,
        WatchEvent.completed.is_(True),
    )
    last_completed = (
        select(func.max(WatchEvent.watched_at))
        .where(
            WatchEvent.media_item_id == MediaItem.id,
            WatchEvent.completed.is_(True),
        )
        .scalar_subquery()
    )
    statement = select(MediaItem)
    if query:
        statement = statement.where(MediaItem.title.ilike(f"%{query}%"))
    if kind in {item.value for item in MediaKind}:
        statement = statement.where(MediaItem.kind == MediaKind(kind))
    if watched == "watched":
        statement = statement.where(completed_event)
    elif watched == "unwatched":
        statement = statement.where(~completed_event)
    raw_page = request.args.get("page", "1")
    page = max(int(raw_page) if raw_page.isdigit() else 1, 1)
    values = db.session.scalars(
        statement.distinct()
        .order_by(last_completed.desc().nullslast(), MediaItem.title, MediaItem.id)
        .limit(51)
        .offset((page - 1) * 50)
    ).all()
    has_more = len(values) > 50
    return render_template(
        "history.html",
        items=values[:50],
        page=page,
        has_more=has_more,
        query=query,
        kind=kind,
        watched=watched,
    )


@blueprint.get("/catalog")
def catalog() -> Any:
    query = request.args.get("query", "").strip()[:200]
    kind = request.args.get("kind", "")
    availability = request.args.get("availability", "all")
    played = request.args.get("played", "all")
    library = request.args.get("library", "")
    genre = request.args.get("genre", "").strip().casefold()
    decade = request.args.get("decade", "")
    sort = request.args.get("sort", "title")
    direction = request.args.get("direction", "asc")
    raw_page = request.args.get("page", "1")
    page = max(int(raw_page) if raw_page.isdigit() else 1, 1)
    allowed_kinds = {
        MediaKind.MOVIE,
        MediaKind.SHOW,
        MediaKind.ARTIST,
        MediaKind.ALBUM,
        MediaKind.TRACK,
    }
    available_ref = exists().where(
        SourceMediaRef.media_item_id == MediaItem.id,
        SourceMediaRef.available.is_(True),
    )
    completion_count = (
        select(func.count())
        .select_from(WatchEvent)
        .where(
            WatchEvent.media_item_id == MediaItem.id,
            WatchEvent.completed.is_(True),
        )
        .scalar_subquery()
    )
    last_completed = (
        select(func.max(WatchEvent.watched_at))
        .where(
            WatchEvent.media_item_id == MediaItem.id,
            WatchEvent.completed.is_(True),
        )
        .scalar_subquery()
    )
    plex_available = exists().where(
        SourceMediaRef.media_item_id == MediaItem.id,
        SourceMediaRef.available.is_(True),
        SourceMediaRef.source_id == Source.id,
        Source.connector_type == ConnectorType.PLEX,
    )
    jellyfin_available = exists().where(
        SourceMediaRef.media_item_id == MediaItem.id,
        SourceMediaRef.available.is_(True),
        SourceMediaRef.source_id == Source.id,
        Source.connector_type == ConnectorType.JELLYFIN,
    )
    statement = select(
        MediaItem,
        completion_count.label("completion_count"),
        last_completed.label("last_completed"),
        plex_available.label("plex_available"),
        jellyfin_available.label("jellyfin_available"),
    )
    if kind in {item.value for item in allowed_kinds}:
        statement = statement.where(MediaItem.kind == MediaKind(kind))
    else:
        statement = statement.where(
            MediaItem.kind.in_([MediaKind.MOVIE, MediaKind.SHOW, MediaKind.ARTIST])
        )
    if query:
        statement = statement.where(MediaItem.title.ilike(f"%{query}%"))
    if library.isdigit():
        statement = statement.where(
            exists().where(
                SourceMediaRef.media_item_id == MediaItem.id,
                SourceMediaRef.library_id == int(library),
            )
        )
    if genre:
        statement = statement.where(
            exists().where(
                MediaGenre.media_item_id == MediaItem.id,
                MediaGenre.genre_id == Genre.id,
                Genre.normalized_name == genre,
            )
        )
    if decade.isdigit():
        decade_start = int(decade)
        if 1800 <= decade_start <= 2200 and decade_start % 10 == 0:
            statement = statement.where(
                MediaItem.year >= decade_start,
                MediaItem.year < decade_start + 10,
            )
    if availability == "available":
        statement = statement.where(available_ref)
    elif availability == "unavailable":
        statement = statement.where(~available_ref)
    if played == "played":
        statement = statement.where(completion_count > 0)
    elif played == "unplayed":
        statement = statement.where(completion_count == 0)
    sort_columns = {
        "title": func.coalesce(MediaItem.sort_title, MediaItem.title),
        "original_title": func.coalesce(MediaItem.original_title, MediaItem.title),
        "year": MediaItem.year,
        "last_played": last_completed,
        "first_played": select(func.min(WatchEvent.watched_at))
        .where(
            WatchEvent.media_item_id == MediaItem.id,
            WatchEvent.completed.is_(True),
        )
        .scalar_subquery(),
        "play_count": completion_count,
        "added": func.coalesce(MediaItem.source_added_at, MediaItem.created_at),
        "updated": MediaItem.updated_at,
        "removed": select(func.max(SourceMediaRef.unavailable_since))
        .where(SourceMediaRef.media_item_id == MediaItem.id)
        .scalar_subquery(),
        "duration": MediaItem.duration_ms,
        "rating": MediaItem.audience_rating,
    }
    sort_column = sort_columns.get(sort, sort_columns["title"])
    ordering = (
        sort_column.desc().nullslast() if direction == "desc" else sort_column.asc().nullslast()
    )
    rows = db.session.execute(
        statement.order_by(ordering, MediaItem.title, MediaItem.id)
        .limit(41)
        .offset((page - 1) * 40)
    ).all()
    return render_template(
        "catalog.html",
        rows=rows[:40],
        page=page,
        has_more=len(rows) > 40,
        query=query,
        kind=kind,
        availability=availability,
        played=played,
        sort=sort,
        direction=direction,
        library=library,
        genre=genre,
        decade=decade,
        libraries=db.session.scalars(select(Library).order_by(Library.name)).all(),
        genres=db.session.scalars(select(Genre).order_by(Genre.name)).all(),
    )


@blueprint.get("/media/<int:media_id>/image")
def media_image(media_id: int) -> Any:
    item = db.session.get(MediaItem, media_id)
    if item is None:
        return Response(status=404)
    image = db.session.scalar(
        select(MediaImage).where(
            MediaImage.media_item_id == media_id,
            MediaImage.image_type == "poster",
        )
    )
    if image is None:
        return _placeholder_image(item.kind)
    cache_directory = Path(current_app.instance_path) / "images"
    if image.provider not in {"plex", "jellyfin"}:
        try:
            path = ensure_external_cached(image, cache_directory)
            db.session.commit()
        except (OSError, ValueError):
            db.session.rollback()
            return _placeholder_image(item.kind)
        response = send_file(path, mimetype=image.mime_type, conditional=True, max_age=86400)
        response.headers["Cache-Control"] = "private, max-age=86400"
        return response
    source = db.session.get(Source, image.source_id)
    if source is None or not source.enabled:
        return _placeholder_image(item.kind)
    connector = connector_for(source)
    square = item.kind in {MediaKind.ARTIST, MediaKind.ALBUM, MediaKind.TRACK}
    try:
        path = ensure_cached(
            image,
            connector,
            cache_directory,
            width=400 if square else 300,
            height=400 if square else 450,
        )
        db.session.commit()
    except (ConnectorError, OSError):
        db.session.rollback()
        return _placeholder_image(item.kind)
    finally:
        close = getattr(connector, "close", None)
        if callable(close):
            close()
    response = send_file(path, mimetype=image.mime_type, conditional=True, max_age=86400)
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


@blueprint.get("/media/<int:media_id>")
def media_detail(media_id: int) -> Any:
    item = db.session.get(MediaItem, media_id)
    if item is None:
        return render_template("errors/404.html"), 404
    item_completion_count = int(
        db.session.scalar(
            select(func.count())
            .select_from(WatchEvent)
            .where(
                WatchEvent.media_item_id == media_id,
                WatchEvent.completed.is_(True),
            )
        )
        or 0
    )
    item_last_completed = db.session.scalar(
        select(func.max(WatchEvent.watched_at)).where(
            WatchEvent.media_item_id == media_id,
            WatchEvent.completed.is_(True),
        )
    )
    children = db.session.scalars(
        select(MediaItem)
        .where(MediaItem.parent_id == media_id)
        .order_by(MediaItem.season_number, MediaItem.episode_number, MediaItem.id)
    ).all()
    grouped_children: dict[int, list[MediaItem]] = {}
    if item.kind in {MediaKind.SHOW, MediaKind.ARTIST} and children:
        descendants = db.session.scalars(
            select(MediaItem)
            .where(MediaItem.parent_id.in_([child.id for child in children]))
            .order_by(
                MediaItem.parent_id,
                MediaItem.disc_number,
                MediaItem.track_number,
                MediaItem.episode_number,
                MediaItem.id,
            )
        ).all()
        for descendant in descendants:
            if descendant.parent_id is not None:
                grouped_children.setdefault(descendant.parent_id, []).append(descendant)
        children_for_states = [*children, *descendants]
    else:
        children_for_states = list(children)
    child_completions = {
        child_id: (int(count), last_completed)
        for child_id, count, last_completed in db.session.execute(
            select(
                WatchEvent.media_item_id,
                func.count(WatchEvent.id),
                func.max(WatchEvent.watched_at),
            )
            .where(
                WatchEvent.media_item_id.in_([child.id for child in children_for_states]),
                WatchEvent.completed.is_(True),
            )
            .group_by(WatchEvent.media_item_id)
        )
    }
    playable = [
        child for child in children_for_states if child.kind in {MediaKind.EPISODE, MediaKind.TRACK}
    ]
    aggregate = None
    if (
        item.kind
        in {
            MediaKind.SHOW,
            MediaKind.SEASON,
            MediaKind.ARTIST,
            MediaKind.ALBUM,
        }
        and playable
    ):
        last_played = max(
            (child_completions[child.id][1] for child in playable if child.id in child_completions),
            default=None,
        )
        aggregate = {
            "total": len(playable),
            "played": sum(1 for child in playable if child.id in child_completions),
            "play_count": sum(child_completions.get(child.id, (0, None))[0] for child in playable),
            "last_played": last_played,
        }
    history_ids = [child.id for child in playable] or [media_id]
    raw_history_page = request.args.get("history_page", "1")
    history_page = max(int(raw_history_page) if raw_history_page.isdigit() else 1, 1)
    history_total = int(
        db.session.scalar(
            select(func.count())
            .select_from(WatchEvent)
            .where(
                WatchEvent.media_item_id.in_(history_ids),
                WatchEvent.completed.is_(True),
            )
        )
        or 0
    )
    event_rows = db.session.execute(
        select(WatchEvent, MediaItem)
        .join(MediaItem, MediaItem.id == WatchEvent.media_item_id)
        .where(
            WatchEvent.media_item_id.in_(history_ids),
            WatchEvent.completed.is_(True),
        )
        .order_by(WatchEvent.watched_at.desc(), WatchEvent.id.desc())
        .limit(51)
        .offset((history_page - 1) * 50)
    ).all()
    child_availability = {
        child_id: (bool(plex), bool(jellyfin))
        for child_id, plex, jellyfin in db.session.execute(
            select(
                MediaItem.id,
                exists().where(
                    SourceMediaRef.media_item_id == MediaItem.id,
                    SourceMediaRef.available.is_(True),
                    SourceMediaRef.source_id == Source.id,
                    Source.connector_type == ConnectorType.PLEX,
                ),
                exists().where(
                    SourceMediaRef.media_item_id == MediaItem.id,
                    SourceMediaRef.available.is_(True),
                    SourceMediaRef.source_id == Source.id,
                    Source.connector_type == ConnectorType.JELLYFIN,
                ),
            ).where(MediaItem.id.in_([child.id for child in children_for_states]))
        )
    }
    item_genres = db.session.scalars(
        select(Genre)
        .join(MediaGenre, MediaGenre.genre_id == Genre.id)
        .where(MediaGenre.media_item_id == media_id)
        .order_by(Genre.name)
    ).all()
    availability_rows = db.session.execute(
        select(SourceMediaRef, Source, Library)
        .join(Source, Source.id == SourceMediaRef.source_id)
        .join(Library, Library.id == SourceMediaRef.library_id)
        .where(SourceMediaRef.media_item_id == media_id)
        .order_by(Source.name, Library.name)
    ).all()
    return render_template(
        "media_detail.html",
        item=item,
        item_completion_count=item_completion_count,
        item_last_completed=item_last_completed,
        event_rows=event_rows[:50],
        history_page=history_page,
        history_has_more=len(event_rows) > 50,
        history_total=history_total,
        children=children,
        grouped_children=grouped_children,
        aggregate=aggregate,
        item_genres=item_genres,
        child_completions=child_completions,
        child_availability=child_availability,
        availability_rows=availability_rows,
    )


@blueprint.get("/about")
def about() -> Any:
    return render_template("about.html")


def _count(model: type[Any], *criteria: Any) -> int:
    return int(db.session.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)


def _watched_count(kind: MediaKind) -> int:
    return int(
        db.session.scalar(
            select(func.count())
            .select_from(MediaItem)
            .where(
                MediaItem.kind == kind,
                exists().where(
                    WatchEvent.media_item_id == MediaItem.id,
                    WatchEvent.completed.is_(True),
                ),
            )
        )
        or 0
    )


def _settings(*keys: str) -> dict[str, str]:
    return {
        item.key: item.value
        for item in db.session.scalars(select(Setting).where(Setting.key.in_(keys)))
    }


def _save_setting(key: str, value: str) -> None:
    setting = db.session.get(Setting, key)
    if setting is None:
        db.session.add(Setting(key=key, value=value))
    else:
        setting.value = value


def _valid_webhook_token(provider: str, supplied: str) -> bool:
    expected = _settings(f"webhook.{provider}.token").get(f"webhook.{provider}.token")
    return bool(expected and secrets.compare_digest(expected, supplied))


def _jellyfin_credentials(source: Source) -> dict[str, str]:
    try:
        raw = json.loads(source.secret)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return {key: str(value) for key, value in raw.items()} if isinstance(raw, dict) else {}


def _persist_webhook_event(
    source: Source,
    *,
    external_id: str,
    library_external_id: str,
    watched_at: datetime,
    source_event_id: str | None,
    duration_ms: int | None,
) -> bool:
    reference = db.session.scalar(
        select(SourceMediaRef).where(
            SourceMediaRef.source_id == source.id,
            SourceMediaRef.external_id == external_id,
        )
    )
    if reference is None:
        _queue_source_sync(source.id)
        return False
    event = ExternalWatchEvent(
        media_external_id=external_id,
        library_external_id=library_external_id,
        watched_at=watched_at,
        completed=True,
        source_event_id=source_event_id,
        duration_ms=duration_ms,
    )
    with UnitOfWork(db.session()) as work:
        inserted = MediaPersistenceService(
            work, source_id=source.id, library_id=reference.library_id
        ).persist_event(event)
        work.commit()
    return inserted


def _queue_source_sync(source_id: int) -> None:
    try:
        get_executor(current_app).submit(source_id)
    except (LookupError, SyncAlreadyRunningError, SyncSourceUnavailableError):
        current_app.logger.info("webhook media will be reconciled by an existing sync")


def _safe_integer(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _ticks_to_ms(value: Any) -> int | None:
    ticks = _safe_integer(value)
    return ticks // 10_000 if ticks is not None else None


def _parse_webhook_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.casefold() == "true")


def _placeholder_image(kind: MediaKind) -> Response:
    square = kind in {MediaKind.ARTIST, MediaKind.ALBUM, MediaKind.TRACK}
    width, height = (400, 400) if square else (300, 450)
    label = "Música" if square else "Sem capa"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        '<rect width="100%" height="100%" fill="#dfe8ec"/>'
        f'<text x="50%" y="50%" text-anchor="middle" dominant-baseline="middle" '
        f'font-family="sans-serif" font-size="24" fill="#607080">{label}</text></svg>'
    )
    response = Response(svg, mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response
