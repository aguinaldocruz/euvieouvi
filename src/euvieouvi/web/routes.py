"""Jinja pages and HTMX fragments for the local interface."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from html import unescape
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
    session,
    url_for,
)
from sqlalchemy import and_, case, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from euvieouvi.api.runtime import connector_for, get_executor
from euvieouvi.api.validation import http_url
from euvieouvi.connectors.dtos import ExternalWatchEvent
from euvieouvi.connectors.errors import ConnectorError
from euvieouvi.connectors.plex.connector import PlexConnector
from euvieouvi.database.enums import ConnectorType, MediaKind, SyncStatus
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
    SyncCheckpoint,
    SyncError,
    SyncRun,
    SyncRunLibrary,
    WatchEvent,
    WatchState,
    WebhookEvent,
)
from euvieouvi.database.unit_of_work import UnitOfWork
from euvieouvi.enrichment.runtime import get_enrichment_executor
from euvieouvi.errors import AppError
from euvieouvi.extensions import db
from euvieouvi.media_images import ensure_cached, ensure_external_cached
from euvieouvi.sync.async_tasks import enqueue_watch_update, get_async_task_executor
from euvieouvi.sync.discovery import LibraryDiscoveryService
from euvieouvi.sync.errors import SyncAlreadyRunningError, SyncSourceUnavailableError
from euvieouvi.sync.jobs import (
    JOBS,
    get_image_executor,
    reconcile_completed_sync_job,
    setting_key,
    submit_job,
)
from euvieouvi.sync.persistence import MediaPersistenceService
from euvieouvi.sync.source_identity import (
    apply_server_identity,
    reset_library_incremental_state,
    reset_source_incremental_state,
)
from euvieouvi.web.formatting import duration_ms, elapsed_time, local_datetime

blueprint = Blueprint("web", __name__)

_TRAKT_LOCK = threading.Lock()
_TRAKT_STATUS: dict[str, Any] = {
    "active": False,
    "state": "idle",
    "percent": 0,
    "message": "Pronto para importar.",
}


def _trakt_progress(message: str) -> None:
    percent = int(_TRAKT_STATUS["percent"])
    phases = {"Fase 1/4": 5, "Fase 2/4": 20, "Fase 3/4": 50, "Fase 4/4": 85}
    for prefix, value in phases.items():
        if message.startswith(prefix):
            percent = value
    match = re.match(r"^\s*(associação|eventos|estados):\s*(\d+)/(\d+)", message)
    if match:
        ranges = {"associação": (20, 50), "eventos": (50, 85), "estados": (85, 98)}
        start, end = ranges[match.group(1)]
        percent = min(
            end, start + round((end - start) * int(match.group(2)) / max(int(match.group(3)), 1))
        )
    with _TRAKT_LOCK:
        _TRAKT_STATUS.update(percent=percent, message=message)


@blueprint.app_context_processor
def template_helpers() -> dict[str, Any]:
    active = db.session.scalar(
        select(SyncRun)
        .where(SyncRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
        .order_by(SyncRun.id.desc())
    )
    language = _settings("ui.language").get("ui.language", "en")
    if language not in {"en", "pt-BR"}:
        language = "en"
    return {
        "app_version": version("euvieouvi"),
        "active_sync": active,
        "format_datetime": local_datetime,
        "format_duration": duration_ms,
        "format_elapsed": elapsed_time,
        "ui_theme": _settings("ui.theme").get("ui.theme", "system"),
        "ui_language": language,
    }


def _source(connector_type: ConnectorType | None = None) -> Source | None:
    statement = select(Source)
    if connector_type is not None:
        statement = statement.where(Source.connector_type == connector_type)
    return db.session.scalar(statement.order_by(Source.id))


def _htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _series_titles(items: Sequence[MediaItem]) -> dict[int, str]:
    """Return series titles for episodes without issuing per-item queries."""
    episodes = [item for item in items if item.kind == MediaKind.EPISODE]
    parent_ids = {item.parent_id for item in episodes if item.parent_id is not None}
    if not parent_ids:
        return {}
    parents = {
        parent.id: parent
        for parent in db.session.scalars(select(MediaItem).where(MediaItem.id.in_(parent_ids)))
    }
    show_ids = {
        parent.parent_id
        for parent in parents.values()
        if parent.kind == MediaKind.SEASON and parent.parent_id is not None
    }
    shows = {
        show.id: show
        for show in db.session.scalars(select(MediaItem).where(MediaItem.id.in_(show_ids)))
    }
    result: dict[int, str] = {}
    for episode in episodes:
        parent = parents.get(episode.parent_id)
        show = parent if parent is not None and parent.kind == MediaKind.SHOW else None
        if parent is not None and parent.kind == MediaKind.SEASON:
            show = shows.get(parent.parent_id)
        if show is not None:
            result[episode.id] = show.title
    return result


@blueprint.get("/")
def dashboard() -> Any:
    source = _source()
    libraries = db.session.scalars(select(Library).order_by(Library.name)).all()
    last_run = db.session.scalar(
        select(SyncRun).order_by(SyncRun.created_at.desc(), SyncRun.id.desc())
    )
    recent = db.session.execute(
        select(WatchEvent, MediaItem, Source)
        .join(MediaItem, MediaItem.id == WatchEvent.media_item_id)
        .join(Source, Source.id == WatchEvent.source_id)
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
        next_step = ("Execute a primeira sincronização", "jobs")
    _, current_events = _webhook_activity(_webhook_history_limit())
    return render_template(
        "dashboard.html",
        counts=counts,
        source=source,
        next_step=next_step,
        last_run=last_run,
        recent=recent,
        current_events=current_events,
        series_titles=_series_titles([row[1] for row in recent]),
        job_definitions=JOBS,
        job_runs=_latest_job_runs(),
    )


@blueprint.get("/setup")
def setup() -> Any:
    return redirect(url_for("web.settings_plex"))


@blueprint.route("/settings/plex", methods=["GET", "POST"])
def settings_plex() -> Any:
    source = _source(ConnectorType.PLEX)
    user_values = _settings("plex.user_id", "webhook.plex.user_filter")
    configured_user = user_values.get("plex.user_id") or user_values.get(
        "webhook.plex.user_filter", ""
    )
    plex_users = ()
    if source is not None and source.last_connection_status == "succeeded":
        connector = connector_for(source)
        if isinstance(connector, PlexConnector):
            try:
                plex_users = connector.list_users()
            except ConnectorError:
                flash(
                    "Não foi possível carregar os usuários do Plex. Teste a conexão novamente.",
                    "warning",
                )
            finally:
                connector.close()
    selected_account = next(
        (
            user
            for user in plex_users
            if configured_user.casefold() in {user.external_id.casefold(), user.name.casefold()}
        ),
        None,
    )
    selected_user = (
        selected_account.external_id if selected_account is not None else configured_user
    )
    if selected_account is not None:
        stored_name = _settings("plex.user_name").get("plex.user_name", "")
        if stored_name != selected_account.name:
            _save_setting("plex.user_name", selected_account.name)
            db.session.commit()
    errors: dict[str, str] = {}
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        base_url = request.form.get("base_url", "").strip()
        secret = request.form.get("secret", "")
        user_id = request.form.get("user_id", "").strip()
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
        if plex_users and not user_id:
            errors["user_id"] = "Selecione o usuário Plex acompanhado."
        elif user_id and (
            not user_id.isdigit()
            or (plex_users and user_id not in {user.external_id for user in plex_users})
        ):
            errors["user_id"] = "Selecione um usuário disponível no servidor Plex."
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
                source_changed = (
                    source.base_url != normalized_url
                    or source.enabled != enabled
                    or bool(secret.strip() and source.secret != secret.strip())
                )
                source.name = name
                source.base_url = normalized_url
                source.enabled = enabled
                if secret.strip():
                    source.secret = secret.strip()
                if source_changed:
                    reset_source_incremental_state(db.session, source.id)
            if user_id:
                selected = next((user for user in plex_users if user.external_id == user_id), None)
                _save_setting("plex.user_id", user_id)
                _save_setting("plex.user_name", selected.name if selected is not None else user_id)
                _save_setting("webhook.plex.user_filter", user_id)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                errors["name"] = "Já existe uma fonte com este nome."
            else:
                flash("Configuração do Plex salva com segurança.", "success")
                return redirect(url_for("web.settings_plex"))
    return render_template(
        "settings_plex.html",
        source=source,
        errors=errors,
        plex_users=plex_users,
        selected_user=selected_user,
    )


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
    jellyfin_users = ()
    if source is not None and source.last_connection_status == "succeeded":
        connector = None
        try:
            connector = connector_for(source)
            list_users = getattr(connector, "list_users", None)
            if callable(list_users):
                jellyfin_users = list_users()
        except (ConnectorError, OSError, ValueError):
            flash(
                "Não foi possível carregar os usuários do Jellyfin. Teste a conexão novamente.",
                "warning",
            )
        finally:
            if connector is not None:
                close = getattr(connector, "close", None)
                if callable(close):
                    close()
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
        elif jellyfin_users and user_id not in {
            user.external_id for user in jellyfin_users
        }:
            errors["user_id"] = "Selecione um usuário disponível no servidor Jellyfin."
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
                serialized_credentials = json.dumps(credentials)
                source_changed = (
                    source.base_url != normalized_url
                    or source.secret != serialized_credentials
                    or source.enabled != enabled
                )
                source.name = name
                source.base_url = normalized_url
                source.secret = serialized_credentials
                source.enabled = enabled
                if source_changed:
                    reset_source_incremental_state(db.session, source.id)
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
        jellyfin_users=jellyfin_users,
    )


@blueprint.route("/settings/appearance", methods=["GET", "POST"])
def settings_appearance() -> Any:
    overlay_keys = (
        "catalog.overlay.media_type",
        "catalog.overlay.plex",
        "catalog.overlay.jellyfin",
        "catalog.overlay.played",
    )
    appearance = _settings("ui.theme", "ui.language", *overlay_keys)
    theme = appearance.get("ui.theme", "system")
    if theme not in {"system", "light", "dark"}:
        theme = "system"
    if request.method == "POST":
        selected = request.form.get("theme", "system").strip()
        language = request.form.get("language", "en").strip()
        if selected not in {"system", "light", "dark"}:
            flash("Selecione uma preferência de tema válida.", "danger")
        elif language not in {"en", "pt-BR"}:
            flash("Selecione um idioma válido.", "danger")
        else:
            _save_setting("ui.theme", selected)
            _save_setting("ui.language", language)
            for key in overlay_keys:
                _save_setting(key, "true" if request.form.get(key) == "on" else "false")
            db.session.commit()
            flash("Preferência de aparência atualizada.", "success")
            return redirect(url_for("web.settings_appearance"))
    language = appearance.get("ui.language", "en")
    if language not in {"en", "pt-BR"}:
        language = "en"
    return render_template(
        "settings_appearance.html",
        theme=theme,
        language=language,
        appearance=appearance,
    )


@blueprint.route("/jobs", methods=["GET", "POST"])
def jobs() -> Any:
    """List, configure, and operate independent background jobs."""
    keys = tuple(
        setting_key(job.id, field) for job in JOBS for field in ("enabled", "time", "last_date")
    )
    values = _settings(
        *keys,
        "jobs.retention.keep_last",
        "jobs.catalog_reconcile.apply",
        "watch_sync.enabled",
    )
    errors: dict[str, str] = {}
    if request.method == "POST":
        parsed: dict[str, str] = {}
        for job in JOBS:
            raw = request.form.get(f"time_{job.id}", job.default_time).strip()
            try:
                parsed[job.id] = datetime.strptime(raw, "%H:%M").strftime("%H:%M")
            except ValueError:
                errors[job.id] = "Informe um horário válido entre 00:00 e 23:59."
        keep_raw = request.form.get("retention_keep", "20").strip()
        try:
            retention_keep = int(keep_raw)
            if not 1 <= retention_keep <= 500:
                raise ValueError
        except ValueError:
            retention_keep = 20
            errors["retention"] = "Informe um número entre 1 e 500."
        if not errors:
            for job in JOBS:
                _save_setting(
                    setting_key(job.id, "enabled"),
                    "true" if request.form.get(f"enabled_{job.id}") == "on" else "false",
                )
                _save_setting(setting_key(job.id, "time"), parsed[job.id])
            _save_setting("jobs.retention.keep_last", str(retention_keep))
            _save_setting(
                "jobs.catalog_reconcile.apply",
                "true" if request.form.get("catalog_reconcile_apply") == "on" else "false",
            )
            watch_sync_enabled = request.form.get("watch_sync_enabled") == "on"
            _save_setting("watch_sync.enabled", "true" if watch_sync_enabled else "false")
            db.session.commit()
            if watch_sync_enabled:
                with contextlib.suppress(Exception):
                    get_executor(current_app).submit_pending_watch_sync()
            flash("Agendamentos dos jobs atualizados.", "success")
            return redirect(url_for("web.jobs"))

    active_syncs = {
        source.connector_type.value
        for _run, source in db.session.execute(
            select(SyncRun, Source)
            .join(Source, Source.id == SyncRun.source_id)
            .where(SyncRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
        )
    }
    metadata = get_enrichment_executor(current_app).snapshot
    images = get_image_executor(current_app).snapshot
    watch_active = get_executor(current_app).watch_sync_active
    states = {
        "sync_plex": "running" if "plex" in active_syncs else "idle",
        "sync_jellyfin": "running" if "jellyfin" in active_syncs else "idle",
        "metadata": "running" if metadata["active"] else "idle",
        "catalog_images": "running" if images["active"] else "idle",
        "watched_plex_to_jellyfin": "running" if watch_active else "idle",
        "watched_jellyfin_to_plex": "running" if watch_active else "idle",
    }
    sync_runs: dict[str, SyncRun] = {}
    for job_id, connector_type in (
        ("sync_plex", ConnectorType.PLEX),
        ("sync_jellyfin", ConnectorType.JELLYFIN),
    ):
        latest_sync = db.session.scalar(
            select(SyncRun)
            .join(Source, Source.id == SyncRun.source_id)
            .where(Source.connector_type == connector_type)
            .order_by(SyncRun.created_at.desc(), SyncRun.id.desc())
        )
        if latest_sync is not None:
            sync_runs[job_id] = latest_sync
    return render_template(
        "jobs.html",
        jobs=JOBS,
        values=values,
        errors=errors,
        states=states,
        metadata=metadata,
        images=images,
        latest_runs=_latest_job_runs(),
        sync_runs=sync_runs,
        recent_runs=db.session.scalars(
            select(JobRun).order_by(JobRun.created_at.desc(), JobRun.id.desc()).limit(20)
        ).all(),
        timezone=current_app.config["TIMEZONE"],
    )


@blueprint.post("/jobs/<job_id>/run")
def job_run(job_id: str) -> Any:
    if job_id not in {job.id for job in JOBS}:
        return render_template("errors/404.html"), 404
    try:
        started = submit_job(current_app, job_id)
    except (SyncAlreadyRunningError, SyncSourceUnavailableError):
        started = False
    if started:
        flash("Job iniciado em segundo plano.", "success")
    else:
        flash("O job já está ativo ou sua fonte não está disponível.", "warning")
    return redirect(url_for("web.jobs"))


@blueprint.get("/jobs/<job_id>/status-fragment")
def job_status_fragment(job_id: str) -> Any:
    job = next((item for item in JOBS if item.id == job_id), None)
    if job is None:
        return Response("Job não encontrado.", 404)
    run = db.session.scalar(
        select(JobRun)
        .where(JobRun.job_id == job_id)
        .order_by(JobRun.created_at.desc(), JobRun.id.desc())
    )
    if run is not None:
        reconcile_completed_sync_job(run)
    return render_template("fragments/job_status.html", job=job, run=run)


@blueprint.get("/jobs/dashboard-fragment")
def dashboard_jobs_fragment() -> Any:
    return render_template(
        "fragments/dashboard_jobs.html",
        job_definitions=JOBS,
        job_runs=_latest_job_runs(),
    )


@blueprint.get("/jobs/<job_id>/history")
def job_history(job_id: str) -> Any:
    job = next((item for item in JOBS if item.id == job_id), None)
    if job is None:
        return render_template("errors/404.html"), 404
    runs = db.session.scalars(
        select(JobRun)
        .where(JobRun.job_id == job_id)
        .order_by(JobRun.created_at.desc(), JobRun.id.desc())
        .limit(500)
    ).all()
    return render_template("job_history.html", job=job, runs=runs)


@blueprint.get("/job-runs/<int:run_id>/log")
def job_log(run_id: int) -> Any:
    run = db.session.get(JobRun, run_id)
    if run is None or not run.log_filename:
        return Response("Log não encontrado.", 404, mimetype="text/plain")
    log_dir = (Path(current_app.instance_path) / "job-logs").resolve()
    path = (log_dir / run.log_filename).resolve()
    if path.parent != log_dir or not path.is_file():
        return Response("Log não encontrado.", 404, mimetype="text/plain")
    return send_file(path, mimetype="text/plain", as_attachment=False, conditional=True)


def _latest_job_runs() -> dict[str, JobRun]:
    result: dict[str, JobRun] = {}
    rows = db.session.scalars(select(JobRun).order_by(JobRun.created_at.desc(), JobRun.id.desc()))
    for run in rows:
        result.setdefault(run.job_id, run)
        if len(result) == len(JOBS):
            break
    return result


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
    executor = get_enrichment_executor(current_app)
    return render_template(
        "settings_metadata.html",
        values=values,
        enrichment_active=executor.active,
        enrichment=executor.snapshot,
    )


@blueprint.get("/metadata/enrichment-status")
def metadata_enrichment_status() -> Any:
    values = _settings("metadata.last_summary")
    return render_template(
        "fragments/enrichment_status.html",
        enrichment=get_enrichment_executor(current_app).snapshot,
        last_summary=values.get("metadata.last_summary", "ainda não executado"),
    )


@blueprint.post("/metadata/enrich")
def metadata_enrich() -> Any:
    if get_enrichment_executor(current_app).submit():
        flash("Enriquecimento iniciado em segundo plano.", "success")
    else:
        flash("O enriquecimento já está em execução.", "warning")
    return redirect(url_for("web.settings_metadata"))


@blueprint.post("/metadata/enrich/cancel")
def metadata_enrich_cancel() -> Any:
    if get_enrichment_executor(current_app).cancel():
        flash("Cancelamento do enriquecimento solicitado.", "warning")
    else:
        flash("Nenhum enriquecimento está em execução.", "info")
    return redirect(url_for("web.settings_metadata"))


@blueprint.post("/settings/plex/test")
def settings_plex_test() -> Any:
    source = _source(ConnectorType.PLEX)
    if source is None:
        flash("Salve a configuração antes de testar.", "warning")
        return redirect(url_for("web.settings_plex"))
    try:
        info = connector_for(source).test_connection()
        apply_server_identity(
            db.session, source, info.server_identifier, now=datetime.now(UTC)
        )
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
        apply_server_identity(
            db.session, source, info.server_identifier, now=datetime.now(UTC)
        )
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


def _backup_dir() -> Path:
    return Path(current_app.instance_path) / "backups"


def _list_backups() -> list[dict[str, Any]]:
    d = _backup_dir()
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("euvieouvi-*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
            out.append(
                {
                    "name": p.name,
                    "path": p,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz=UTC),
                }
            )
        except OSError:
            continue
    return out


def _prune_backups(keep: int) -> int:
    items = _list_backups()
    removed = 0
    for item in items[keep:]:
        try:
            Path(item["path"]).unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _prune_sync_runs(keep: int) -> int:
    if keep < 1:
        return 0
    ids = db.session.scalars(
        select(SyncRun.id).order_by(SyncRun.created_at.desc(), SyncRun.id.desc()).limit(keep)
    ).all()
    if not ids:
        return 0
    to_keep = set(ids)
    # delete older runs not in keep set (and cascading children via FK CASCADE)
    old_ids = db.session.scalars(
        select(SyncRun.id).where(SyncRun.id.notin_(to_keep)).order_by(SyncRun.id)
    ).all()
    if not old_ids:
        return 0
    # delete older runs (FK CASCADE handles children)
    from sqlalchemy import delete

    db.session.execute(delete(SyncRun).where(SyncRun.id.in_(old_ids)))
    db.session.commit()
    return len(old_ids)


@blueprint.route("/settings/backup", methods=["GET", "POST"])
def settings_backup() -> Any:
    values = _settings(
        "backup.schedule.enabled",
        "backup.schedule.time",
        "backup.retention.keep_last",
        "sync.retention.keep_last",
        "backup.retention.keep_last",
    )
    errors: dict[str, str] = {}
    if request.method == "POST":
        backup_enabled = request.form.get("backup_enabled") == "on"
        backup_time = request.form.get("backup_time", "").strip() or "04:00"
        try:
            datetime.strptime(backup_time, "%H:%M")
        except ValueError:
            errors["backup_time"] = "Informe um horário válido entre 00:00 e 23:59."
        backup_keep_raw = request.form.get("backup_keep", "15").strip()
        sync_keep_raw = request.form.get("sync_keep", "15").strip()
        try:
            backup_keep = int(backup_keep_raw)
            if not 1 <= backup_keep <= 500:
                raise ValueError
        except ValueError:
            errors["backup_keep"] = "Informe um número entre 1 e 500."
            backup_keep = 15
        try:
            sync_keep = int(sync_keep_raw)
            if not 1 <= sync_keep <= 500:
                raise ValueError
        except ValueError:
            errors["sync_keep"] = "Informe um número entre 1 e 500."
            sync_keep = 15
        if not errors:
            _save_setting("backup.schedule.enabled", "true" if backup_enabled else "false")
            _save_setting("backup.schedule.time", backup_time)
            _save_setting("backup.retention.keep_last", str(backup_keep))
            _save_setting("sync.retention.keep_last", str(sync_keep))
            db.session.commit()
            # prune immediately on save
            _prune_backups(backup_keep)
            _prune_sync_runs(sync_keep)
            flash("Configurações de backup e retenção salvas.", "success")
            return redirect(url_for("web.settings_backup"))
    backups = _list_backups()
    plex_identity = _settings("plex.user_id", "plex.user_name")
    trakt_plex_user_id = plex_identity.get("plex.user_id", "")
    trakt_plex_user_name = plex_identity.get("plex.user_name", "")
    plex_source = _source(ConnectorType.PLEX)
    if trakt_plex_user_id and not trakt_plex_user_name and plex_source is not None:
        connector = connector_for(plex_source)
        if isinstance(connector, PlexConnector):
            try:
                trakt_plex_user_name = next(
                    (
                        user.name
                        for user in connector.list_users()
                        if user.external_id == trakt_plex_user_id
                    ),
                    trakt_plex_user_id,
                )
            except (ConnectorError, OSError, ValueError):
                trakt_plex_user_name = trakt_plex_user_id
            finally:
                connector.close()
    return render_template(
        "settings_backup.html",
        backup_enabled=values.get("backup.schedule.enabled", "false") == "true",
        backup_time=values.get("backup.schedule.time", "04:00"),
        backup_keep=values.get("backup.retention.keep_last", "15"),
        sync_keep=values.get("sync.retention.keep_last", "15"),
        backups=backups,
        timezone=current_app.config["TIMEZONE"],
        trakt_sources=db.session.scalars(
            select(Source).where(Source.connector_type == ConnectorType.PLEX).order_by(Source.name)
        ).all(),
        errors=errors,
        trakt_plex_user_id=trakt_plex_user_id,
        trakt_plex_user_name=trakt_plex_user_name,
    )


@blueprint.post("/backups/backup-now")
def backup_now() -> Any:
    from euvieouvi.database.backup import backup_database

    # ensure instance dir exists
    db_path = Path(current_app.instance_path) / "euvieouvi.db"
    # fallback to sqlite uri path
    uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if uri.startswith("sqlite:///"):
        with contextlib.suppress(Exception):
            db_path = Path(uri.removeprefix("sqlite:///"))
    dest_dir = _backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"euvieouvi-{ts}.db"
    try:
        backup_database(db_path, dest)
    except Exception as exc:
        flash(f"Backup falhou: {exc}", "danger")
        return redirect(url_for("web.settings_backup"))
    # prune per retention
    keep_raw = _settings("backup.retention.keep_last").get("backup.retention.keep_last", "15")
    try:
        keep = int(keep_raw)
    except ValueError:
        keep = 15
    _prune_backups(keep)
    flash(f"Backup criado: {dest.name}", "success")
    return redirect(url_for("web.settings_backup"))


@blueprint.post("/backups/<path:filename>/delete")
def backup_delete(filename: str) -> Any:
    # sanitize
    if "/" in filename or "\\" in filename or not filename.endswith(".db"):
        return Response("Nome inválido.", 400)
    p = _backup_dir() / filename
    if not p.is_file():
        flash("Backup não encontrado.", "warning")
        return redirect(url_for("web.settings_backup"))
    try:
        p.unlink()
        flash(f"Backup apagado: {filename}", "success")
    except OSError as exc:
        flash(f"Falha ao apagar: {exc}", "danger")
    return redirect(url_for("web.settings_backup"))


@blueprint.get("/backups/<path:filename>/download")
def backup_download(filename: str) -> Any:
    if "/" in filename or "\\" in filename or not filename.endswith(".db"):
        return Response("Nome inválido.", 400)
    p = _backup_dir() / filename
    if not p.is_file():
        return Response("Backup não encontrado.", 404)
    return send_file(
        p, as_attachment=True, download_name=filename, mimetype="application/octet-stream"
    )


@blueprint.post("/backups/<path:filename>/restore")
def backup_restore(filename: str) -> Any:
    if "/" in filename or "\\" in filename or not filename.endswith(".db"):
        return Response("Nome inválido.", 400)
    src = _backup_dir() / filename
    if not src.is_file():
        flash("Backup não encontrado.", "warning")
        return redirect(url_for("web.settings_backup"))
    # block if sync active
    active = db.session.scalar(
        select(SyncRun).where(SyncRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
    )
    if active is not None:
        flash("Há sincronização ativa; aguarde terminar antes de restaurar.", "warning")
        return redirect(url_for("web.settings_backup"))
    from euvieouvi.database.backup import restore_database

    uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    dest = Path(current_app.instance_path) / "euvieouvi.db"
    if uri.startswith("sqlite:///"):
        with contextlib.suppress(Exception):
            dest = Path(uri.removeprefix("sqlite:///"))
    try:
        restore_database(src, dest)
        flash(
            f"Restauração concluída a partir de {filename}. Reinicie o serviço se necessário.",
            "success",
        )
    except Exception as exc:
        flash(f"Restauração falhou: {exc}", "danger")
    return redirect(url_for("web.settings_backup"))


@blueprint.post("/backups/restore-upload")
def backup_restore_upload() -> Any:
    if "file" not in request.files:
        flash("Selecione um arquivo .db para restaurar.", "warning")
        return redirect(url_for("web.settings_backup"))
    f = request.files["file"]
    if not f.filename or not f.filename.endswith(".db"):
        flash("Arquivo deve ser .db SQLite.", "warning")
        return redirect(url_for("web.settings_backup"))
    active = db.session.scalar(
        select(SyncRun).where(SyncRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
    )
    if active is not None:
        flash("Há sincronização ativa; aguarde terminar antes de restaurar.", "warning")
        return redirect(url_for("web.settings_backup"))
    tmp = _backup_dir() / f"upload-{secrets.token_hex(6)}.db"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        f.save(tmp)
        from euvieouvi.database.backup import restore_database

        uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
        dest = Path(current_app.instance_path) / "euvieouvi.db"
        if uri.startswith("sqlite:///"):
            with contextlib.suppress(Exception):
                dest = Path(uri.removeprefix("sqlite:///"))
        restore_database(tmp, dest)
        flash("Restauração por upload concluída e sobrescrita.", "success")
    except Exception as exc:
        flash(f"Restauração por upload falhou: {exc}", "danger")
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    return redirect(url_for("web.settings_backup"))


def _active_sync_exists() -> bool:
    return (
        db.session.scalar(
            select(SyncRun.id).where(SyncRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
        )
        is not None
    )


@blueprint.post("/backups/reset-catalog-history")
def reset_catalog_history() -> Any:
    if request.form.get("confirmation", "").strip() != "RECARREGAR":
        flash("Digite RECARREGAR para confirmar a limpeza.", "warning")
        return redirect(url_for("web.settings_backup"))
    if _active_sync_exists():
        flash("Há sincronização ativa; aguarde terminar antes de limpar os dados.", "warning")
        return redirect(url_for("web.settings_backup"))
    try:
        # Sources, libraries and settings are setup/configuration and are intentionally preserved.
        db.session.execute(delete(SyncCheckpoint))
        db.session.execute(delete(SyncRun))
        db.session.execute(delete(WebhookEvent))
        db.session.execute(delete(WatchEvent))
        db.session.execute(delete(WatchState))
        db.session.execute(delete(MediaGenre))
        db.session.execute(delete(MediaIdentifier))
        db.session.execute(delete(MediaImage))
        from euvieouvi.database.models import EnrichmentRecord

        db.session.execute(delete(EnrichmentRecord))
        db.session.execute(delete(SourceMediaRef))
        db.session.execute(update(MediaItem).values(parent_id=None))
        db.session.execute(delete(MediaItem))
        db.session.execute(delete(Genre))
        for key in ("watch_sync.pending",):
            setting = db.session.get(Setting, key)
            if setting is not None:
                db.session.delete(setting)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("catalog and history reset failed")
        flash("Não foi possível limpar catálogo e histórico.", "danger")
    else:
        flash(
            "Catálogo e histórico apagados. Configuração e bibliotecas foram preservadas.",
            "success",
        )
    return redirect(url_for("web.settings_backup"))


@blueprint.get("/backups/trakt-import/status")
def trakt_import_status() -> Any:
    with _TRAKT_LOCK:
        return dict(_TRAKT_STATUS)


@blueprint.post("/backups/trakt-import")
def trakt_import() -> Any:
    from scripts import import_trakt_export as importer

    if _active_sync_exists():
        flash("Há sincronização ativa; aguarde terminar antes de importar.", "warning")
        return redirect(url_for("web.settings_backup"))
    archive_file = request.files.get("archive")
    if archive_file is None or not archive_file.filename:
        flash("Selecione o arquivo ZIP exportado pelo Trakt.", "warning")
        return redirect(url_for("web.settings_backup"))
    if not archive_file.filename.casefold().endswith(".zip"):
        flash("O export do Trakt deve ser um arquivo .zip.", "warning")
        return redirect(url_for("web.settings_backup"))
    raw_source_id = request.form.get("source_id", "")
    if not raw_source_id.isdigit():
        flash("Selecione a fonte à qual o histórico será associado.", "warning")
        return redirect(url_for("web.settings_backup"))
    source_id = int(raw_source_id)
    selected_source = db.session.get(Source, source_id)
    if selected_source is None or selected_source.connector_type is not ConnectorType.PLEX:
        flash("A fonte selecionada não existe.", "warning")
        return redirect(url_for("web.settings_backup"))
    apply_import = request.form.get("mode") == "apply"
    configured_plex_user = _configured_source_user(selected_source)
    plex_user = request.form.get("plex_user", "").strip() or configured_plex_user
    if not plex_user or plex_user != configured_plex_user:
        flash(
            "O usuário do Trakt deve ser o Account.id configurado para o Plex.",
            "warning",
        )
        return redirect(url_for("web.settings_backup"))
    progress_raw = request.form.get("progress_every", "1000")
    progress_every = int(progress_raw) if progress_raw.isdigit() else 1000
    progress_every = min(max(progress_every, 1), 100000)
    uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if not uri.startswith("sqlite:///"):
        flash("A importação offline do Trakt requer o banco SQLite local.", "danger")
        return redirect(url_for("web.settings_backup"))
    database = Path(uri.removeprefix("sqlite:///"))
    upload = _backup_dir() / ("trakt-" + secrets.token_hex(8) + ".zip")
    upload.parent.mkdir(parents=True, exist_ok=True)
    with _TRAKT_LOCK:
        if _TRAKT_STATUS["active"]:
            return {"error": "Já existe uma importação Trakt em andamento."}, 409
        _TRAKT_STATUS.update(
            active=True,
            state="processing",
            percent=1,
            message="Upload concluído. Preparando o arquivo…",
        )
    try:
        archive_file.save(upload)
    except Exception:
        with _TRAKT_LOCK:
            _TRAKT_STATUS.update(
                active=False, state="failed", percent=100, message="Falha ao salvar o upload."
            )
        raise
    db.session.remove()
    db.engine.dispose()
    application = current_app._get_current_object()

    def execute_import() -> None:
        try:
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                importer._validate_database(connection)
                report = importer.import_archive(
                    connection,
                    upload,
                    database,
                    source_id=source_id,
                    plex_user=plex_user,
                    apply=apply_import,
                    progress=_trakt_progress,
                    progress_every=progress_every,
                )
            finally:
                connection.close()
            action = "importados" if report.committed else "validados em dry-run"
            with _TRAKT_LOCK:
                _TRAKT_STATUS.update(
                    active=False,
                    state="succeeded",
                    percent=100,
                    message=(
                        f"{report.events_valid} eventos {action}; "
                        f"{report.events_inserted} novos e "
                        f"{report.events_already_imported} já existentes."
                    ),
                )
        except Exception as exc:
            application.logger.exception("Trakt background import failed")
            with _TRAKT_LOCK:
                _TRAKT_STATUS.update(
                    active=False, state="failed", percent=100, message=f"Importação falhou: {exc}"
                )
        finally:
            with contextlib.suppress(OSError):
                upload.unlink(missing_ok=True)

    threading.Thread(target=execute_import, name="euvieouvi-trakt-import", daemon=True).start()
    return {"accepted": True, "status_url": url_for("web.trakt_import_status")}, 202


@blueprint.route("/settings/webhooks", methods=["GET", "POST"])
def settings_webhooks() -> Any:
    tokens = _settings(
        "webhook.plex.token",
        "webhook.jellyfin.token",
        "webhook.history_limit",
        "webhook.plex.user_filter",
    )
    changed = False
    for provider in ("plex", "jellyfin"):
        key = f"webhook.{provider}.token"
        if not tokens.get(key):
            tokens[key] = secrets.token_urlsafe(32)
            _save_setting(key, tokens[key])
            changed = True
    if changed:
        db.session.commit()
    plex_users = ()
    plex_source = _source(ConnectorType.PLEX)
    if plex_source is not None and plex_source.last_connection_status == "succeeded":
        connector = None
        try:
            connector = connector_for(plex_source)
            if isinstance(connector, PlexConnector):
                plex_users = connector.list_users()
        except (ConnectorError, OSError, ValueError):
            flash("Não foi possível carregar os usuários do Plex.", "warning")
        finally:
            if connector is not None:
                close = getattr(connector, "close", None)
                if callable(close):
                    close()
    if request.method == "POST":
        raw_limit = (request.form.get("history_limit") or "20").strip()
        if "plex_user_filter" in request.form:
            plex_user_filter = (request.form.get("plex_user_filter") or "").strip()[:255]
            if plex_users and plex_user_filter not in {
                user.external_id for user in plex_users
            }:
                flash("Selecione um usuário disponível no servidor Plex.", "danger")
                return redirect(url_for("web.settings_webhooks"))
            _save_setting("webhook.plex.user_filter", plex_user_filter)
            if plex_user_filter:
                selected = next(
                    (user for user in plex_users if user.external_id == plex_user_filter), None
                )
                _save_setting("plex.user_id", plex_user_filter)
                _save_setting(
                    "plex.user_name",
                    selected.name if selected is not None else plex_user_filter,
                )
            db.session.commit()
            flash("Filtro de usuário Plex atualizado.", "success")
            return redirect(url_for("web.settings_webhooks"))
        history_limit = min(max(int(raw_limit) if raw_limit.isdigit() else 20, 1), 200)
        _save_setting("webhook.history_limit", str(history_limit))
        _prune_webhook_events(history_limit)
        db.session.commit()
        flash("Quantidade de eventos recentes atualizada.", "success")
        return redirect(url_for("web.settings_webhooks"))
    history_limit = _webhook_history_limit(tokens)
    recent_events, current_events = _webhook_activity(history_limit)
    return render_template(
        "settings_webhooks.html",
        plex_url=url_for("web.plex_webhook", token=tokens["webhook.plex.token"], _external=True),
        jellyfin_url=url_for(
            "web.jellyfin_webhook", token=tokens["webhook.jellyfin.token"], _external=True
        ),
        plex_user_filter=tokens.get("webhook.plex.user_filter", ""),
        plex_users=plex_users,
        history_limit=history_limit,
        recent_events=recent_events,
        current_events=current_events,
    )


@blueprint.get("/settings/webhooks/activity-fragment")
def webhook_activity_fragment() -> Any:
    _, current_events = _webhook_activity(_webhook_history_limit())
    return render_template(
        "fragments/webhook_activity.html",
        current_events=current_events,
    )


def _webhook_activity(history_limit: int) -> tuple[Sequence[Any], Sequence[Any]]:
    # Playback-stop webhooks can be lost during restarts or connectivity failures.
    stale_before = datetime.now(UTC) - timedelta(hours=1)
    stale_events = db.session.scalars(
        select(WebhookEvent).where(
            WebhookEvent.active.is_(True), WebhookEvent.occurred_at < stale_before
        )
    ).all()
    for event in stale_events:
        event.active = False
    if stale_events:
        db.session.commit()

    recent_events = db.session.execute(
        select(WebhookEvent, Source)
        .join(Source, Source.id == WebhookEvent.source_id)
        .where(WebhookEvent.completed.is_(True))
        .order_by(WebhookEvent.occurred_at.desc(), WebhookEvent.id.desc())
        .limit(history_limit)
    ).all()
    current_events = db.session.execute(
        select(WebhookEvent, Source)
        .join(Source, Source.id == WebhookEvent.source_id)
        .where(WebhookEvent.active.is_(True))
        .order_by(WebhookEvent.occurred_at.desc(), WebhookEvent.id.desc())
    ).all()
    return recent_events, current_events


@blueprint.post("/webhooks/plex/<token>")
def plex_webhook(token: str) -> Any:
    if not _valid_webhook_token("plex", token):
        return Response(status=404)
    source = _source(ConnectorType.PLEX)
    payload_text = request.form.get("payload")
    if not payload_text and request.is_json:
        raw_payload = request.get_json(silent=True)
        payload_text = json.dumps(raw_payload) if isinstance(raw_payload, dict) else None
    # Webhook is processed even if source is disabled/missing sync config — independent of sync
    if source is None or not payload_text:
        # if no source configured, store minimal to avoid losing event? just acknowledge
        if source is None:
            return Response(status=204)
        return Response(status=204)
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return Response("Payload inválido.", 400)
    if not isinstance(payload, dict):
        return Response("Payload inválido.", 400)
    event_type = str(payload.get("event") or "").casefold()
    if event_type not in {
        "media.play",
        "media.resume",
        "media.pause",
        "media.stop",
        "media.scrobble",
    }:
        return Response(status=204)
    metadata = payload.get("Metadata")
    if not isinstance(metadata, dict):
        if event_type == "media.scrobble":
            return Response("Metadata ausente.", 400)
        return Response(status=204)
    if _plex_live_tv_metadata(metadata):
        current_app.logger.info(
            "Ignoring Plex Live TV webhook event=%s media=%s",
            event_type,
            str(metadata.get("ratingKey") or "")[:64],
        )
        return Response(status=204)
    external_id = str(metadata.get("ratingKey") or "").strip()
    library_external_id = str(metadata.get("librarySectionID") or "").strip()
    if not external_id:
        return Response("Identidade da mídia ausente.", 400)
    has_catalog_reference = db.session.scalar(
        select(SourceMediaRef.id).where(
            SourceMediaRef.source_id == source.id,
            SourceMediaRef.external_id == external_id,
        )
    ) is not None
    watched_at = _parse_webhook_datetime(payload.get("eventTime")) or datetime.now(UTC)
    title = str(metadata.get("title") or metadata.get("grandparentTitle") or external_id)
    media_kind = metadata.get("type")
    series_title = metadata.get("grandparentTitle") if media_kind == "episode" else None
    account = payload.get("Account")
    raw_account: dict[str, Any] = account if isinstance(account, dict) else {}
    playback_user = str(raw_account.get("title") or raw_account.get("id") or "").strip() or None
    active = event_type in {"media.play", "media.resume"}
    progress_percent = _playback_percent(metadata.get("viewOffset"), metadata.get("duration"))
    plex_view_number: int | None = None
    user_matches = _plex_webhook_user_matches(raw_account)
    completed = (
        event_type == "media.scrobble"
        or (event_type == "media.stop" and progress_percent is not None and progress_percent >= 90)
    ) and user_matches
    if event_type == "media.stop" and not completed and user_matches and has_catalog_reference:
        try:
            connector = connector_for(source)
            if not isinstance(connector, PlexConnector):
                raise TypeError("configured Plex source returned an unexpected connector")
            try:
                stored_item = connector.get_media_item(external_id, library_external_id)
            finally:
                connector.close()
            plex_view_number = stored_item.view_count
            if stored_item.last_viewed_at is not None and (stored_item.view_count or 0) > 0:
                stored_at = stored_item.last_viewed_at
                if stored_at.tzinfo is None:
                    stored_at = stored_at.replace(tzinfo=UTC)
                completed = abs((stored_at - watched_at).total_seconds()) <= 600
                if completed:
                    watched_at = stored_at
        except (ConnectorError, TypeError, ValueError):
            current_app.logger.exception(
                "Plex webhook completion could not be verified for media %s", external_id
            )
    if event_type in {"media.stop", "media.scrobble"}:
        current_app.logger.info(
            "Plex terminal webhook event=%s media=%s account_id=%s account_title=%s "
            "progress=%s user_match=%s completed=%s",
            event_type,
            external_id,
            str(raw_account.get("id") or "")[:64],
            str(raw_account.get("title") or "")[:128],
            progress_percent,
            user_matches,
            completed,
        )
    _record_webhook_activity(
        source,
        external_id=external_id,
        title=title,
        series_title=series_title,
        playback_user=playback_user,
        media_kind=media_kind,
        event_type=event_type,
        occurred_at=watched_at,
        progress_percent=progress_percent,
        completed=completed,
        active=active,
        event_key=str(payload.get("event_id") or "").strip() or None,
    )
    if not completed:
        db.session.commit()
        return Response(status=204)
    if not library_external_id:
        return Response("Biblioteca da mídia ausente.", 400)
    # process even if source.enabled is False
    inserted = _persist_webhook_event(
        source,
        external_id=external_id,
        library_external_id=library_external_id,
        watched_at=watched_at,
        source_event_id=None,
        duration_ms=_safe_integer(metadata.get("duration")),
        playback_user=_configured_source_user(source),
        view_number=plex_view_number,
    )
    db.session.commit()
    if not inserted:
        _queue_source_sync(source.id)
        return Response(status=204)
    _request_watch_propagation()
    return Response(status=204)


@blueprint.post("/webhooks/jellyfin/<token>")
def jellyfin_webhook(token: str) -> Any:
    if not _valid_webhook_token("jellyfin", token):
        return Response(status=404)
    source = _source(ConnectorType.JELLYFIN)
    payload = _jellyfin_webhook_payload()
    if source is None or not isinstance(payload, dict):
        return Response(status=204)
    notification_type = "".join(
        char for char in str(payload.get("NotificationType") or "").casefold() if char.isalnum()
    )
    if notification_type not in {"playbackstart", "playbackprogress", "playbackstop"}:
        return Response(status=204)
    credentials = _jellyfin_credentials(source)
    configured_user_matches = _jellyfin_webhook_user_matches(credentials, payload)
    payload_item = payload.get("Item")
    raw_item: dict[str, Any] = payload_item if isinstance(payload_item, dict) else {}
    if _jellyfin_live_tv_payload(payload, raw_item):
        current_app.logger.info(
            "Ignoring Jellyfin Live TV webhook event=%s media=%s",
            notification_type,
            str(payload.get("ItemId") or raw_item.get("Id") or "")[:64],
        )
        return Response(status=204)
    external_id = str(payload.get("ItemId") or raw_item.get("Id") or "").strip()
    timestamp_value = payload.get("UtcTimestamp")
    watched_at = _parse_webhook_datetime(timestamp_value)
    if timestamp_value and watched_at is None:
        if not configured_user_matches:
            return Response(status=204)
        return Response("Identidade ou data ausente.", 400)
    watched_at = watched_at or datetime.now(UTC)
    if not external_id:
        if not configured_user_matches:
            return Response(status=204)
        return Response("Identidade ou data ausente.", 400)
    completed = (
        notification_type == "playbackstop"
        and _truthy(payload.get("PlayedToCompletion"))
        and configured_user_matches
    )
    media_kind = str(payload.get("ItemType") or raw_item.get("Type") or "") or None
    series_title = (
        (payload.get("SeriesName") or raw_item.get("SeriesName"))
        if media_kind and media_kind.casefold() == "episode"
        else None
    )
    _record_webhook_activity(
        source,
        external_id=external_id,
        playback_user=(
            str(
                payload.get("NotificationUsername")
                or payload.get("Username")
                or payload.get("UserId")
                or ""
            ).strip()
            or None
        ),
        title=str(
            payload.get("Name") or payload.get("ItemName") or raw_item.get("Name") or external_id
        ),
        series_title=series_title,
        media_kind=media_kind,
        event_type=notification_type,
        occurred_at=watched_at,
        progress_percent=_playback_percent(
            _first_present(
                payload.get("PlaybackPositionTicks"),
                payload.get("PositionTicks"),
                raw_item.get("PlaybackPositionTicks"),
                raw_item.get("PositionTicks"),
            ),
            _first_present(payload.get("RunTimeTicks"), raw_item.get("RunTimeTicks")),
        ),
        completed=completed,
        active=notification_type in {"playbackstart", "playbackprogress"},
        event_key=str(payload.get("NotificationId") or "").strip() or None,
    )
    if not completed:
        db.session.commit()
        return Response(status=204)
    reference = db.session.scalar(
        select(SourceMediaRef).where(
            SourceMediaRef.source_id == source.id,
            SourceMediaRef.external_id == external_id,
        )
    )
    # Independent of sync config: persist even if no prior SourceMediaRef
    if reference is None:
        # try to infer library from any library of this source
        fallback_lib = db.session.scalar(
            select(Library.external_id).where(Library.source_id == source.id).order_by(Library.id)
        )
        library_external_id = fallback_lib or "unknown"
        _persist_webhook_event(
            source,
            external_id=external_id,
            library_external_id=library_external_id,
            watched_at=watched_at,
            source_event_id=str(payload.get("NotificationId") or "").strip() or None,
            duration_ms=_ticks_to_ms(payload.get("RunTimeTicks")),
            playback_user=str(credentials.get("user_id") or "").strip() or None,
            view_number=None,
        )
        enqueue_watch_update(
            db.session(),
            source_id=source.id,
            external_id=external_id,
            watched_at=watched_at,
        )
        source_id = source.id
        db.session.commit()
        # also queue a sync to enrich metadata, but don't block webhook response
        with contextlib.suppress(Exception):
            _queue_source_sync(source_id)
        db.session.commit()
        _request_watch_propagation()
        return Response(status=204)
    library = db.session.get(Library, reference.library_id)
    if library is None:
        db.session.commit()
        return Response(status=204)
    _persist_webhook_event(
        source,
        external_id=external_id,
        library_external_id=library.external_id,
        watched_at=watched_at,
        source_event_id=str(payload.get("NotificationId") or "").strip() or None,
        duration_ms=_ticks_to_ms(payload.get("RunTimeTicks")),
        playback_user=str(credentials.get("user_id") or "").strip() or None,
        view_number=None,
    )
    db.session.commit()
    _request_watch_propagation()
    return Response(status=204)


@blueprint.get("/libraries")
def libraries() -> Any:
    sources = db.session.scalars(select(Source).order_by(Source.name)).all()
    values = db.session.scalars(
        select(Library).where(Library.available.is_(True)).order_by(Library.name, Library.id)
    ).all()
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
        if library.enabled != enabled:
            reset_library_incremental_state(db.session, library.id)
        library.enabled = enabled
        db.session.commit()
        flash("Seleção da biblioteca atualizada.", "success")
    if _htmx():
        return render_template("fragments/library_row.html", library=library)
    return redirect(url_for("web.libraries"))


@blueprint.get("/jobs/sync-runs/<int:run_id>/fragment")
def job_sync_detail_fragment(run_id: int) -> Any:
    context = _sync_detail_context(run_id)
    if context is None:
        return Response("Sincronização não encontrada.", 404)
    return render_template(
        "fragments/sync_detail_content.html",
        **context,
        poll_url=url_for("web.job_sync_detail_fragment", run_id=run_id),
    )


def _sync_detail_context(run_id: int) -> dict[str, Any] | None:
    run = db.session.get(SyncRun, run_id)
    if run is None:
        return None
    libraries = db.session.execute(
        select(SyncRunLibrary, Library)
        .join(Library, Library.id == SyncRunLibrary.library_id)
        .where(SyncRunLibrary.sync_run_id == run_id)
        .order_by(SyncRunLibrary.id)
    ).all()
    errors = db.session.scalars(
        select(SyncError)
        .where(SyncError.sync_run_id == run_id)
        .order_by(
            case((SyncError.category == "view_count_regression", 1), else_=0),
            SyncError.id,
        )
        .limit(100)
    ).all()
    return {
        "run": run,
        "source": db.session.get(Source, run.source_id),
        "libraries": libraries,
        "errors": errors,
    }


@blueprint.post("/jobs/sync-runs/<int:run_id>/cancel")
def job_sync_cancel(run_id: int) -> Any:
    run = db.session.get(SyncRun, run_id)
    if run is None:
        return Response("Sincronização não encontrada.", 404)
    if run.status not in {SyncStatus.QUEUED, SyncStatus.RUNNING}:
        flash("Esta sincronização já terminou.", "warning")
    else:
        get_executor(current_app).cancel(run_id)
        flash("Cancelamento solicitado. Os dados já confirmados serão preservados.", "warning")
    return redirect(url_for("web.jobs"))


@blueprint.get("/history")
def history() -> Any:
    query = request.args.get("query", "").strip()[:200]
    kind = request.args.get("kind", "")
    watched = request.args.get("watched", "watched")
    # Performance: use aggregated outer joins instead of correlated scalar subqueries
    evt_agg = (
        select(
            WatchEvent.media_item_id.label("mid"),
            func.max(WatchEvent.watched_at).label("evt_last"),
        )
        .where(WatchEvent.completed.is_(True))
        .group_by(WatchEvent.media_item_id)
        .subquery()
    )
    st_agg = (
        select(
            WatchState.media_item_id.label("mid"),
            func.max(WatchState.last_watched_at).label("st_last"),
        )
        .where(WatchState.completed.is_(True))
        .group_by(WatchState.media_item_id)
        .subquery()
    )
    last_completed_expr = func.coalesce(
        case(
            (evt_agg.c.evt_last.is_(None), st_agg.c.st_last),
            (st_agg.c.st_last.is_(None), evt_agg.c.evt_last),
            (evt_agg.c.evt_last >= st_agg.c.st_last, evt_agg.c.evt_last),
            else_=st_agg.c.st_last,
        ),
        evt_agg.c.evt_last,
        st_agg.c.st_last,
    )
    # For watched filter, use exists is still cheap, but we can use join non-null
    has_evt = evt_agg.c.evt_last.is_not(None)
    has_st = st_agg.c.st_last.is_not(None)
    statement = (
        select(MediaItem)
        .outerjoin(evt_agg, evt_agg.c.mid == MediaItem.id)
        .outerjoin(st_agg, st_agg.c.mid == MediaItem.id)
    )
    if query:
        statement = statement.where(MediaItem.title.ilike(f"%{query}%"))
    if kind in {item.value for item in MediaKind}:
        statement = statement.where(MediaItem.kind == MediaKind(kind))
    if watched == "watched":
        statement = statement.where(or_(has_evt, has_st))
    elif watched == "unwatched":
        statement = statement.where(~or_(has_evt, has_st))
    raw_page = request.args.get("page", "1")
    page = max(int(raw_page) if raw_page.isdigit() else 1, 1)
    values = db.session.scalars(
        statement.order_by(last_completed_expr.desc().nullslast(), MediaItem.title, MediaItem.id)
        .limit(51)
        .offset((page - 1) * 50)
    ).all()
    has_more = len(values) > 50
    visible = values[:50]
    history_details: dict[int, list[tuple[WatchEvent, Source]]] = {}
    if visible:
        for event, event_source in db.session.execute(
            select(WatchEvent, Source)
            .join(Source, Source.id == WatchEvent.source_id)
            .where(
                WatchEvent.media_item_id.in_([item.id for item in visible]),
                WatchEvent.completed.is_(True),
            )
            .order_by(WatchEvent.watched_at.desc(), WatchEvent.id.desc())
        ):
            history_details.setdefault(event.media_item_id, []).append((event, event_source))
    return render_template(
        "history.html",
        items=visible,
        history_details=history_details,
        series_titles=_series_titles(visible),
        page=page,
        has_more=has_more,
        query=query,
        kind=kind,
        watched=watched,
    )


@blueprint.get("/catalog")
def catalog() -> Any:
    saved_filters = session.get("catalog.filters", {})
    if not isinstance(saved_filters, dict):
        saved_filters = {}
    query = request.args.get("query", "").strip()[:200]
    kind = request.args.get("kind", str(saved_filters.get("kind", "movie")))
    availability = request.args.get(
        "availability", str(saved_filters.get("availability", "all"))
    )
    played = request.args.get("played", str(saved_filters.get("played", "all")))
    genre = request.args.get("genre", str(saved_filters.get("genre", ""))).strip().casefold()
    sort = request.args.get("sort", str(saved_filters.get("sort", "title")))
    direction = request.args.get("direction", str(saved_filters.get("direction", "asc")))
    raw_page = request.args.get("page", "1")
    page = max(int(raw_page) if raw_page.isdigit() else 1, 1)
    allowed_kinds = {
        MediaKind.MOVIE,
        MediaKind.SHOW,
        MediaKind.EPISODE,
        MediaKind.ARTIST,
        MediaKind.ALBUM,
        MediaKind.TRACK,
    }
    if kind not in {item.value for item in allowed_kinds}:
        kind = "movie"
    if availability not in {"all", "available", "unavailable"}:
        availability = "all"
    if played not in {"all", "played", "unplayed"}:
        played = "all"
    if direction not in {"asc", "desc"}:
        direction = "asc"
    allowed_sorts = {
        "title", "original_title", "year", "last_played", "first_played", "play_count",
        "added", "updated", "removed", "duration", "rating",
    }
    if sort not in allowed_sorts:
        sort = "title"
    persisted_kind = (
        kind if kind in {"movie", "show", "artist"} else saved_filters.get("kind", "movie")
    )
    session["catalog.filters"] = {
        "kind": persisted_kind,
        "availability": availability,
        "played": played,
        "genre": genre,
        "sort": sort,
        "direction": direction,
    }
    active_kinds = (
        [MediaKind(kind)]
        if kind in {item.value for item in allowed_kinds}
        else [MediaKind.MOVIE, MediaKind.SHOW, MediaKind.ARTIST]
    )
    available_ref = exists().where(
        SourceMediaRef.media_item_id == MediaItem.id,
        SourceMediaRef.available.is_(True),
    )
    event_stats = (
        select(
            WatchEvent.media_item_id.label("media_item_id"),
            func.count(WatchEvent.id).label("event_count"),
            func.min(WatchEvent.watched_at).label("event_first"),
            func.max(WatchEvent.watched_at).label("event_last"),
        )
        .where(WatchEvent.completed.is_(True))
        .group_by(WatchEvent.media_item_id)
        .subquery()
    )
    state_stats = (
        select(
            WatchState.media_item_id.label("media_item_id"),
            func.max(WatchState.view_count).label("state_count"),
            func.max(WatchState.last_watched_at).label("state_last"),
        )
        .where(WatchState.completed.is_(True))
        .group_by(WatchState.media_item_id)
        .subquery()
    )
    event_count = func.coalesce(event_stats.c.event_count, 0)
    state_count = func.coalesce(state_stats.c.state_count, 0)
    completion_count = case((event_count >= state_count, event_count), else_=state_count)
    last_completed = case(
        (event_stats.c.event_last.is_not(None), event_stats.c.event_last),
        else_=state_stats.c.state_last,
    )
    completed_known = completion_count > 0
    activity_item = aliased(MediaItem)
    activity_parent = aliased(MediaItem)
    event_activity = (
        select(
            func.coalesce(
                activity_parent.parent_id, activity_item.parent_id, activity_item.id
            ).label("root_id"),
            WatchEvent.watched_at.label("played_at"),
        )
        .join(activity_item, activity_item.id == WatchEvent.media_item_id)
        .outerjoin(activity_parent, activity_parent.id == activity_item.parent_id)
    ).subquery()
    state_activity = (
        select(
            func.coalesce(
                activity_parent.parent_id, activity_item.parent_id, activity_item.id
            ).label("root_id"),
            WatchState.last_watched_at.label("played_at"),
            WatchState.completed.label("completed"),
            WatchState.progress_ms.label("progress_ms"),
        )
        .join(activity_item, activity_item.id == WatchState.media_item_id)
        .outerjoin(activity_parent, activity_parent.id == activity_item.parent_id)
        .where(
            WatchState.last_watched_at.is_not(None),
            or_(
                WatchState.completed.is_(True),
                WatchState.progress_ms > 0,
            ),
        )
    ).subquery()
    event_activity_stats = (
        select(
            event_activity.c.root_id,
            func.max(event_activity.c.played_at).label("event_last"),
        )
        .group_by(event_activity.c.root_id)
        .subquery()
    )
    state_activity_stats = (
        select(
            state_activity.c.root_id,
            func.max(state_activity.c.played_at).label("state_last"),
            func.max(
                case(
                    (
                        and_(
                            state_activity.c.completed.is_(False),
                            state_activity.c.progress_ms > 0,
                        ),
                        state_activity.c.played_at,
                    ),
                    else_=None,
                )
            ).label("partial_last"),
        )
        .group_by(state_activity.c.root_id)
        .subquery()
    )
    last_played = case(
        (
            event_activity_stats.c.event_last.is_not(None),
            case(
                (state_activity_stats.c.partial_last.is_(None), event_activity_stats.c.event_last),
                (
                    event_activity_stats.c.event_last >= state_activity_stats.c.partial_last,
                    event_activity_stats.c.event_last,
                ),
                else_=state_activity_stats.c.partial_last,
            ),
        ),
        else_=state_activity_stats.c.state_last,
    )
    statement = (
        select(
            MediaItem,
            completion_count.label("completion_count"),
            last_completed.label("last_completed"),
        )
        .outerjoin(event_stats, event_stats.c.media_item_id == MediaItem.id)
        .outerjoin(state_stats, state_stats.c.media_item_id == MediaItem.id)
        .outerjoin(event_activity_stats, event_activity_stats.c.root_id == MediaItem.id)
        .outerjoin(state_activity_stats, state_activity_stats.c.root_id == MediaItem.id)
    )
    if kind in {item.value for item in allowed_kinds}:
        statement = statement.where(MediaItem.kind == MediaKind(kind))
    else:
        statement = statement.where(
            MediaItem.kind.in_([MediaKind.MOVIE, MediaKind.SHOW, MediaKind.ARTIST])
        )
    if query:
        statement = statement.where(MediaItem.title.ilike(f"%{query}%"))
    if genre:
        statement = statement.where(
            exists().where(
                MediaGenre.media_item_id == MediaItem.id,
                MediaGenre.genre_id == Genre.id,
                Genre.normalized_name == genre,
            )
        )
    if availability == "available":
        statement = statement.where(available_ref)
    elif availability == "unavailable":
        statement = statement.where(~available_ref)
    if played == "played":
        statement = statement.where(completed_known)
    elif played == "unplayed":
        statement = statement.where(~completed_known)
    sort_columns = {
        "title": func.coalesce(MediaItem.sort_title, MediaItem.title),
        "original_title": func.coalesce(MediaItem.original_title, MediaItem.title),
        "year": MediaItem.year,
        "last_played": last_played,
        "first_played": event_stats.c.event_first,
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
        .limit(80)
        .offset((page - 1) * 40)
    ).all()
    candidate_ids = [row[0].id for row in rows]
    availability_by_item: dict[int, set[ConnectorType]] = {
        media_id: set() for media_id in candidate_ids
    }
    canonical_identifier_by_item: dict[int, tuple[str, str]] = {}
    if candidate_ids:
        identifier_rows = db.session.execute(
            select(
                MediaIdentifier.media_item_id,
                MediaIdentifier.provider,
                MediaIdentifier.external_id,
            ).where(
                MediaIdentifier.media_item_id.in_(candidate_ids),
                MediaIdentifier.provider.in_(("tmdb", "tvdb", "imdb")),
            )
        )
        provider_priority = {"tmdb": 0, "tvdb": 1, "imdb": 2}
        ranked_identifiers: dict[int, tuple[int, str, str]] = {}
        for media_id, provider, external_id in identifier_rows:
            candidate = (provider_priority[provider], provider, external_id)
            current = ranked_identifiers.get(int(media_id))
            if current is None or candidate < current:
                ranked_identifiers[int(media_id)] = candidate
        canonical_identifier_by_item = {
            media_id: (ranked[1], ranked[2]) for media_id, ranked in ranked_identifiers.items()
        }

        direct_rows = db.session.execute(
            select(SourceMediaRef.media_item_id, Source.connector_type)
            .join(Source, Source.id == SourceMediaRef.source_id)
            .where(
                SourceMediaRef.media_item_id.in_(candidate_ids),
                SourceMediaRef.available.is_(True),
            )
            .distinct()
        )
        for media_id, connector_type in direct_rows:
            availability_by_item[int(media_id)].add(connector_type)

        current_identifier = aliased(MediaIdentifier)
        matching_identifier = aliased(MediaIdentifier)
        matching_item = aliased(MediaItem)
        current_item = aliased(MediaItem)
        shared_rows = db.session.execute(
            select(current_identifier.media_item_id, Source.connector_type)
            .join(current_item, current_item.id == current_identifier.media_item_id)
            .join(
                matching_identifier,
                and_(
                    matching_identifier.provider == current_identifier.provider,
                    matching_identifier.external_id == current_identifier.external_id,
                ),
            )
            .join(matching_item, matching_item.id == matching_identifier.media_item_id)
            .join(SourceMediaRef, SourceMediaRef.media_item_id == matching_item.id)
            .join(Source, Source.id == SourceMediaRef.source_id)
            .where(
                current_identifier.media_item_id.in_(candidate_ids),
                matching_item.kind == current_item.kind,
                SourceMediaRef.available.is_(True),
            )
            .distinct()
        )
        for media_id, connector_type in shared_rows:
            availability_by_item[int(media_id)].add(connector_type)

    # Deduplicate by title/year/kind merging Plex/Jellyfin badges.
    # When same media exists in both sources as separate rows (e.g., Plex
    # without identifiers), show single card with both badges.
    # Keep DB order and merge badges via OR.
    deduped: dict[tuple[str, int | None, str], Any] = {}
    ordered_keys: list[tuple[str, int | None, str]] = []
    for row in rows:
        item = row[0]
        source_availability = availability_by_item[item.id]
        display_row = (
            item,
            row[1],
            row[2],
            ConnectorType.PLEX in source_availability,
            ConnectorType.JELLYFIN in source_availability,
        )
        identifier = canonical_identifier_by_item.get(item.id)
        key = (
            f"{identifier[0]}:{identifier[1]}"
            if identifier is not None
            else _normalized_catalog_title(item.title),
            None if identifier is not None else item.year,
            item.kind.value,
        )
        if key not in deduped:
            deduped[key] = display_row
            ordered_keys.append(key)
        else:
            prev = deduped[key]
            merged_plex = bool(prev[3] or display_row[3])
            merged_jelly = bool(prev[4] or display_row[4])
            keep = prev if prev[0].id < item.id else display_row
            deduped[key] = (keep[0], keep[1], keep[2], merged_plex, merged_jelly)
    merged_rows = [deduped[k] for k in ordered_keys]
    has_more = len(merged_rows) > 40
    merged_rows = merged_rows[:40]
    template_values = dict(
        rows=merged_rows,
        page=page,
        has_more=has_more,
        query=query,
        kind=kind,
        availability=availability,
        played=played,
        sort=sort,
        direction=direction,
        genre=genre,
        genres=db.session.scalars(
            select(Genre)
            .where(
                exists().where(
                    MediaGenre.genre_id == Genre.id,
                    MediaGenre.media_item_id == MediaItem.id,
                    MediaItem.kind.in_(active_kinds),
                )
            )
            .order_by(Genre.name)
        ).all(),
        series_titles=_series_titles([row[0] for row in merged_rows]),
        catalog_overlays={
            key.removeprefix("catalog.overlay."): value == "true"
            for key, value in _settings(
                "catalog.overlay.media_type",
                "catalog.overlay.plex",
                "catalog.overlay.jellyfin",
                "catalog.overlay.played",
            ).items()
        },
    )
    if request.args.get("fragment") == "1":
        return render_template("fragments/catalog_results.html", **template_values)
    refresh_values = {
        key: value for key, value in request.args.items() if key != "fragment"
    }
    template_values["catalog_refresh_url"] = url_for(
        "web.catalog", **refresh_values, fragment="1"
    )
    return render_template("catalog.html", **template_values)


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
    available = db.session.scalar(
        select(SourceMediaRef.id).where(
            SourceMediaRef.media_item_id == media_id,
            SourceMediaRef.source_id == source.id,
            SourceMediaRef.available.is_(True),
        )
    )
    try:
        if available is not None:
            if image.source_path is None:
                return _placeholder_image(item.kind)
            content, mime_type = connector.fetch_image(
                image.source_path,
                width=400 if square else 300,
                height=400 if square else 450,
            )
            response = Response(content, mimetype=mime_type)
            response.headers["Cache-Control"] = "private, max-age=86400"
            response.set_etag(hashlib.sha256(content).hexdigest())
            return response.make_conditional(request)
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
        if image.local_filename:
            existing = cache_directory / image.local_filename
            if existing.is_file():
                return send_file(
                    existing, mimetype=image.mime_type, conditional=True, max_age=86400
                )
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
    item_count_expression, item_last_expression, _ = _completion_expressions(media_id)
    item_completion_count = int(db.session.scalar(select(item_count_expression)) or 0)
    item_last_completed = db.session.scalar(select(item_last_expression))
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
    child_state_facts = {
        child_id: (int(count), last_completed)
        for child_id, count, last_completed in db.session.execute(
            select(
                WatchState.media_item_id,
                func.max(WatchState.view_count),
                func.max(WatchState.last_watched_at),
            )
            .where(
                WatchState.media_item_id.in_([child.id for child in children_for_states]),
                WatchState.completed.is_(True),
            )
            .group_by(WatchState.media_item_id)
        )
    }
    for child_id, (state_count, state_last) in child_state_facts.items():
        event_count, event_last = child_completions.get(child_id, (0, None))
        known_last = event_last if event_last is not None else state_last
        child_completions[child_id] = (max(event_count, state_count), known_last)
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
        select(WatchEvent, MediaItem, Source)
        .join(MediaItem, MediaItem.id == WatchEvent.media_item_id)
        .join(Source, Source.id == WatchEvent.source_id)
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
        series_title=_series_titles([item]).get(item.id),
    )


@blueprint.get("/about")
def about() -> Any:
    return render_template("about.html")


def _count(model: type[Any], *criteria: Any) -> int:
    return int(db.session.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)


def _completion_expressions(media_item_id: Any) -> tuple[Any, Any, Any]:
    event_count = (
        select(func.count(WatchEvent.id))
        .where(
            WatchEvent.media_item_id == media_item_id,
            WatchEvent.completed.is_(True),
        )
        .scalar_subquery()
    )
    state_count = (
        select(func.coalesce(func.max(WatchState.view_count), 0))
        .where(
            WatchState.media_item_id == media_item_id,
            WatchState.completed.is_(True),
        )
        .scalar_subquery()
    )
    event_last = (
        select(func.max(WatchEvent.watched_at))
        .where(
            WatchEvent.media_item_id == media_item_id,
            WatchEvent.completed.is_(True),
        )
        .scalar_subquery()
    )
    state_last = (
        select(func.max(WatchState.last_watched_at))
        .where(
            WatchState.media_item_id == media_item_id,
            WatchState.completed.is_(True),
        )
        .scalar_subquery()
    )
    known_count = case((event_count >= state_count, event_count), else_=state_count)
    last_known = case((event_last.is_not(None), event_last), else_=state_last)
    completed_known = or_(
        exists().where(
            WatchEvent.media_item_id == media_item_id,
            WatchEvent.completed.is_(True),
        ),
        exists().where(
            WatchState.media_item_id == media_item_id,
            WatchState.completed.is_(True),
        ),
    )
    return known_count, last_known, completed_known


def _watched_count(kind: MediaKind) -> int:
    # Optimized: count distinct media with completed history via union
    # instead of correlated EXISTS per row (was 4-9s on 36k items).
    evt_ids = select(WatchEvent.media_item_id).where(WatchEvent.completed.is_(True))
    state_ids = select(WatchState.media_item_id).where(WatchState.completed.is_(True))
    combined = evt_ids.union(state_ids).subquery()
    return int(
        db.session.scalar(
            select(func.count())
            .select_from(MediaItem)
            .where(
                MediaItem.kind == kind,
                MediaItem.id.in_(select(combined.c.media_item_id)),
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


def _configured_source_user(source: Source) -> str | None:
    if source.connector_type is ConnectorType.PLEX:
        values = _settings("plex.user_id", "webhook.plex.user_filter")
        return values.get("plex.user_id") or values.get("webhook.plex.user_filter") or None
    credentials = _jellyfin_credentials(source)
    return credentials.get("user_id") or None


def _jellyfin_webhook_payload() -> dict[str, Any] | None:
    """Accept both Jellyfin Webhook's JSON and Generic Form formats."""
    if request.is_json:
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else None
    payload_text = request.form.get("payload")
    if payload_text:
        try:
            payload = json.loads(payload_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
    if request.form:
        return request.form.to_dict(flat=True)
    return None


def _jellyfin_webhook_user_matches(credentials: dict[str, str], payload: dict[str, Any]) -> bool:
    configured = credentials.get("user_id", "").strip().casefold()
    if not configured:
        return True
    webhook_users = (
        payload.get("UserId"),
        payload.get("NotificationUsername"),
        payload.get("Username"),
    )
    return any(str(value or "").strip().casefold() == configured for value in webhook_users)


def _jellyfin_live_tv_payload(payload: dict[str, Any], item: dict[str, Any]) -> bool:
    if _truthy(_first_present(payload.get("IsLive"), item.get("IsLive"))):
        return True
    kinds = {
        "".join(char for char in str(value or "").casefold() if char.isalnum())
        for value in (
            payload.get("ItemType"),
            payload.get("MediaType"),
            item.get("Type"),
            item.get("MediaType"),
        )
    }
    return bool(kinds & {"livetv", "livetvchannel", "livetvprogram", "tvchannel"})


def _plex_webhook_user_matches(account: dict[str, Any]) -> bool:
    values = _settings("webhook.plex.user_filter", "plex.user_id", "plex.user_name")
    configured = {
        str(values.get(key) or "").strip().casefold()
        for key in ("webhook.plex.user_filter", "plex.user_id", "plex.user_name")
        if str(values.get(key) or "").strip()
    }
    if not configured:
        return True
    incoming = {
        str(account.get(key) or "").strip().casefold()
        for key in ("id", "title")
        if str(account.get(key) or "").strip()
    }
    return bool(configured & incoming)


def _plex_live_tv_metadata(metadata: dict[str, Any]) -> bool:
    live = str(metadata.get("live") or "").strip().casefold()
    if live in {"1", "true", "yes"}:
        return True
    section_type = str(metadata.get("librarySectionType") or "").strip().casefold()
    return section_type in {"live", "livetv", "live-tv"}


def _persist_webhook_event(
    source: Source,
    *,
    external_id: str,
    library_external_id: str,
    watched_at: datetime,
    source_event_id: str | None,
    duration_ms: int | None,
    playback_user: str | None,
    view_number: int | None,
) -> bool:
    reference = db.session.scalar(
        select(SourceMediaRef).where(
            SourceMediaRef.source_id == source.id,
            SourceMediaRef.external_id == external_id,
        )
    )
    if reference is None:
        return False
    event = ExternalWatchEvent(
        media_external_id=external_id,
        library_external_id=library_external_id,
        watched_at=watched_at,
        completed=True,
        source_event_id=source_event_id,
        duration_ms=duration_ms,
        playback_user=playback_user,
        view_number=view_number,
    )
    with UnitOfWork(db.session()) as work:
        inserted = MediaPersistenceService(
            work, source_id=source.id, library_id=reference.library_id
        ).persist_event(event, origin="webhook")
        work.commit()
    return inserted


def _request_watch_propagation() -> None:
    if current_app.config.get("TESTING"):
        return
    with contextlib.suppress(Exception):
        get_async_task_executor(current_app).submit(force=True)


def _queue_source_sync(source_id: int) -> None:
    try:
        get_executor(current_app).submit(source_id)
    except (LookupError, SyncAlreadyRunningError, SyncSourceUnavailableError):
        current_app.logger.info("webhook media will be reconciled by an existing sync")


def _webhook_history_limit(values: dict[str, str] | None = None) -> int:
    raw = (values or _settings("webhook.history_limit")).get("webhook.history_limit", "20")
    return min(max(int(raw) if raw.isdigit() else 20, 1), 200)


def _record_webhook_activity(
    source: Source,
    *,
    external_id: str,
    title: str,
    playback_user: str | None,
    series_title: Any,
    media_kind: Any,
    event_type: str,
    occurred_at: datetime,
    progress_percent: int | None,
    completed: bool,
    active: bool,
    event_key: str | None,
) -> None:
    title = unescape(title)
    series_title = unescape(str(series_title)) if series_title else None
    # Any terminal event closes prior playback rows for the same item.
    prior = db.session.scalars(
        select(WebhookEvent).where(
            WebhookEvent.source_id == source.id,
            WebhookEvent.active.is_(True),
        )
    ).all()
    same_user = [row for row in prior if row.playback_user == playback_user]
    matching = [row for row in same_user if row.external_id == external_id]
    if active and matching:
        current = max(matching, key=lambda row: (row.occurred_at, row.id or 0))
        for row in same_user:
            if row is not current:
                row.active = False
        current.event_key = event_key or current.event_key
        current.playback_user = playback_user[:255] if playback_user else None
        current.title = title[:500]
        current.series_title = str(series_title)[:500] if series_title else None
        current.media_kind = str(media_kind)[:32] if media_kind else None
        current.event_type = event_type[:32]
        if progress_percent is not None:
            current.progress_percent = progress_percent
        return
    for row in same_user if active else matching:
        row.active = False
    if not active and not completed and event_type not in {"media.stop", "media.scrobble"}:
        return
    db.session.add(
        WebhookEvent(
            source_id=source.id,
            external_id=external_id,
            playback_user=playback_user[:255] if playback_user else None,
            event_key=event_key,
            title=title[:500],
            series_title=str(series_title)[:500] if series_title else None,
            media_kind=str(media_kind)[:32] if media_kind else None,
            event_type=event_type[:32],
            occurred_at=occurred_at,
            progress_percent=progress_percent,
            completed=completed,
            active=active,
        )
    )
    if completed:
        db.session.flush()
        _prune_webhook_events(_webhook_history_limit())


def _prune_webhook_events(limit: int) -> None:
    retained = (
        select(WebhookEvent.id)
        .where(WebhookEvent.completed.is_(True))
        .order_by(WebhookEvent.occurred_at.desc(), WebhookEvent.id.desc())
        .limit(limit)
    )
    db.session.execute(
        delete(WebhookEvent).where(
            WebhookEvent.completed.is_(True), WebhookEvent.id.not_in(retained)
        )
    )


def _safe_integer(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _ticks_to_ms(value: Any) -> int | None:
    ticks = _safe_integer(value)
    return ticks // 10_000 if ticks is not None else None


def _playback_percent(position: Any, duration: Any) -> int | None:
    position_value = _safe_integer(position)
    duration_value = _safe_integer(duration)
    if position_value is None or duration_value is None or duration_value <= 0:
        return None
    return min(position_value * 100 // duration_value, 100)


def _first_present(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


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


def _normalized_catalog_title(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return " ".join(
        "".join(char for char in decomposed if not unicodedata.combining(char)).split()
    ).casefold()


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
