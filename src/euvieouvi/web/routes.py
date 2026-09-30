"""Jinja pages and HTMX fragments for the local interface."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from html import unescape
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Blueprint,
    Flask,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    stream_with_context,
    url_for,
)
from sqlalchemy import case, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import aliased

from euvieouvi.api.runtime import connector_for, get_executor
from euvieouvi.api.validation import http_url
from euvieouvi.connectors.dtos import ExternalUser, ExternalWatchEvent
from euvieouvi.connectors.errors import ConnectorError, ConnectorNotFoundError
from euvieouvi.connectors.plex.connector import PlexConnector
from euvieouvi.credentials import (
    decrypt_text,
    encrypt_text,
    save_setting_value,
    setting_value,
)
from euvieouvi.database.enums import ConnectorType, MediaKind, SyncStatus, SyncTrigger
from euvieouvi.database.models import (
    CatalogEntry,
    CatalogEntryMember,
    EnrichmentRecord,
    Genre,
    JobRun,
    Library,
    MediaFilePath,
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
from euvieouvi.diagnostics import build_diagnostic_archive
from euvieouvi.enrichment.providers import EnrichmentError, TmdbClient, TmdbMatch
from euvieouvi.enrichment.runtime import get_enrichment_executor
from euvieouvi.errors import AppError
from euvieouvi.extensions import db, new_session
from euvieouvi.jellyfin_auth import JellyfinQuickConnectClient
from euvieouvi.logging import configure_logging
from euvieouvi.media_images import ensure_external_cached
from euvieouvi.observability import operational_snapshot
from euvieouvi.plex_auth import PlexPinClient
from euvieouvi.sync.async_tasks import enqueue_watch_update, get_async_task_executor
from euvieouvi.sync.catalog_projection import ensure_catalog_projection
from euvieouvi.sync.discovery import LibraryDiscoveryService
from euvieouvi.sync.errors import SyncAlreadyRunningError, SyncSourceUnavailableError
from euvieouvi.sync.jobs import (
    JOBS,
    get_image_executor,
    reconcile_completed_sync_job,
    request_job_cancellation,
    setting_key,
    submit_job,
)
from euvieouvi.sync.manual_match import apply_manual_match
from euvieouvi.sync.persistence import MediaPersistenceService
from euvieouvi.sync.physical_identity import ROOTS_KEY, normalize_path, remap_paths
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


def _bounded_form_integer(
    field: str,
    *,
    minimum: int,
    maximum: int,
    errors: dict[str, str],
    message: str,
) -> int | None:
    raw = request.form.get(field, "").strip()
    try:
        value = int(raw)
    except ValueError:
        errors[field] = message
        return None
    if value < minimum or value > maximum:
        errors[field] = message
        return None
    return value


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
        if episode.parent_id is None:
            continue
        parent = parents.get(episode.parent_id)
        show = parent if parent is not None and parent.kind == MediaKind.SHOW else None
        if parent is not None and parent.kind == MediaKind.SEASON and parent.parent_id is not None:
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
        operations=operational_snapshot(),
    )


@blueprint.route("/setup", methods=["GET", "POST"])
@blueprint.route("/settings/system", methods=["GET", "POST"])
def setup() -> Any:
    keys = (
        "system.log_level",
        "system.timezone",
        "system.gunicorn_threads",
        "system.sqlite_busy_timeout_ms",
    )
    values = _settings(*keys)
    defaults = {
        "system.log_level": str(current_app.config["LOG_LEVEL"]),
        "system.timezone": str(current_app.config["TIMEZONE"]),
        "system.gunicorn_threads": str(current_app.config["GUNICORN_THREADS"]),
        "system.sqlite_busy_timeout_ms": str(current_app.config["SQLITE_BUSY_TIMEOUT_MS"]),
    }
    for key, fallback in defaults.items():
        values.setdefault(key, fallback)
    errors: dict[str, str] = {}
    if request.method == "POST":
        log_level = request.form.get("log_level", "").strip().upper()
        timezone = request.form.get("timezone", "").strip()
        threads = _bounded_form_integer(
            "gunicorn_threads",
            minimum=1,
            maximum=32,
            errors=errors,
            message="Informe um número de threads entre 1 e 32.",
        )
        busy_timeout = _bounded_form_integer(
            "sqlite_busy_timeout_ms",
            minimum=1,
            maximum=60000,
            errors=errors,
            message="Informe um timeout entre 1 e 60000 ms.",
        )
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            errors["log_level"] = "Selecione um nível de log válido."
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError):
            errors["timezone"] = "Informe um fuso horário IANA válido."
        if not errors and threads is not None and busy_timeout is not None:
            restart_required = threads != int(
                current_app.config["GUNICORN_THREADS"]
            ) or busy_timeout != int(current_app.config["SQLITE_BUSY_TIMEOUT_MS"])
            _save_setting("system.log_level", log_level)
            _save_setting("system.timezone", timezone)
            _save_setting("system.gunicorn_threads", str(threads))
            _save_setting("system.sqlite_busy_timeout_ms", str(busy_timeout))
            _save_setting("setup.completed", "true")
            db.session.commit()
            current_app.config.update(
                LOG_LEVEL=log_level,
                TIMEZONE=timezone,
                GUNICORN_THREADS=threads,
                SQLITE_BUSY_TIMEOUT_MS=busy_timeout,
            )
            configure_logging(log_level)
            if restart_required:
                flash(
                    "Configuração salva. Reinicie o container para aplicar threads "
                    "e timeout do SQLite.",
                    "success",
                )
            else:
                flash("Configuração do sistema salva.", "success")
            return redirect(url_for("web.setup"))
        values = {
            "system.log_level": log_level,
            "system.timezone": timezone,
            "system.gunicorn_threads": request.form.get("gunicorn_threads", ""),
            "system.sqlite_busy_timeout_ms": request.form.get("sqlite_busy_timeout_ms", ""),
        }
    return render_template("settings_system.html", values=values, errors=errors)


@blueprint.route("/settings/plex", methods=["GET", "POST"])
def settings_plex() -> Any:
    source = _source(ConnectorType.PLEX)
    user_values = _settings("plex.user_id", "webhook.plex.user_filter")
    configured_user = user_values.get("plex.user_id") or user_values.get(
        "webhook.plex.user_filter", ""
    )
    plex_users: tuple[ExternalUser, ...] = ()
    plex_connected = bool(source is not None and decrypt_text(source.secret).strip())
    if source is not None and plex_connected and source.last_connection_status == "succeeded":
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
        user_id = request.form.get("user_id", "").strip()
        enabled = request.form.get("enabled") == "on"
        if not name or len(name) > 255:
            errors["name"] = "Informe um nome com até 255 caracteres."
        try:
            normalized_url = http_url(base_url)
        except AppError:
            errors["base_url"] = "Informe uma URL HTTP ou HTTPS válida, sem credenciais."
            normalized_url = base_url
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
                    secret=encrypt_text(""),
                    enabled=enabled,
                )
                db.session.add(source)
            else:
                source_changed = source.base_url != normalized_url or source.enabled != enabled
                source.name = name
                source.base_url = normalized_url
                source.enabled = enabled
                if source_changed:
                    reset_source_incremental_state(db.session(), source.id)
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
        plex_connected=plex_connected,
    )


@blueprint.post("/settings/plex/auth/start")
def settings_plex_auth_start() -> Any:
    source = _source(ConnectorType.PLEX)
    if source is None:
        flash("Salve o endereço do servidor antes de entrar com o Plex.", "warning")
        return redirect(url_for("web.settings_plex"))
    client = _plex_pin_client()
    try:
        pin = client.start()
        forward_url = url_for("web.settings_plex_auth_complete", _external=True)
        destination = client.authorization_url(pin, forward_url=forward_url)
    except ConnectorError:
        current_app.logger.exception("Plex hosted authorization could not be started")
        flash("Não foi possível iniciar o login do Plex. Tente novamente.", "danger")
        return redirect(url_for("web.settings_plex"))
    finally:
        client.close()
    session["plex_pin_id"] = pin.identifier
    session["plex_pin_source_id"] = source.id
    return redirect(destination)


@blueprint.get("/settings/plex/auth/complete")
def settings_plex_auth_complete() -> Any:
    pin_identifier = session.get("plex_pin_id")
    source_identifier = session.get("plex_pin_source_id")
    if not isinstance(pin_identifier, int) or not isinstance(source_identifier, int):
        flash("A autorização do Plex expirou. Inicie o login novamente.", "warning")
        return redirect(url_for("web.settings_plex"))
    source = db.session.get(Source, source_identifier)
    if source is None or source.connector_type is not ConnectorType.PLEX:
        session.pop("plex_pin_id", None)
        session.pop("plex_pin_source_id", None)
        flash("A fonte Plex desta autorização não existe mais.", "danger")
        return redirect(url_for("web.settings_plex"))
    client = _plex_pin_client()
    try:
        token = client.claim(pin_identifier)
    except ConnectorError:
        current_app.logger.exception("Plex hosted authorization could not be completed")
        flash("Não foi possível concluir o login do Plex. Tente novamente.", "danger")
        return redirect(url_for("web.settings_plex"))
    finally:
        client.close()
    if token is None:
        flash("O login ainda não foi confirmado no Plex. Conclua-o e tente novamente.", "warning")
        return redirect(url_for("web.settings_plex"))
    changed = decrypt_text(source.secret) != token
    source.secret = encrypt_text(token)
    source.last_connection_status = None
    source.last_connection_test_at = None
    if changed:
        reset_source_incremental_state(db.session(), source.id)
    db.session.commit()
    session.pop("plex_pin_id", None)
    session.pop("plex_pin_source_id", None)
    flash("Conta Plex conectada com segurança. Agora teste a conexão.", "success")
    return redirect(url_for("web.settings_plex"))


def _plex_pin_client() -> PlexPinClient:
    client_identifier = _settings("plex.client_identifier").get("plex.client_identifier")
    if not client_identifier:
        client_identifier = secrets.token_hex(16)
        _save_setting("plex.client_identifier", client_identifier)
        db.session.commit()
    return PlexPinClient(
        client_identifier=client_identifier,
        application_version=version("euvieouvi"),
    )


@blueprint.route("/settings/jellyfin", methods=["GET", "POST"])
def settings_jellyfin() -> Any:
    source = _source(ConnectorType.JELLYFIN)
    errors: dict[str, str] = {}
    persisted: dict[str, str] = {}
    if source is not None:
        try:
            raw = json.loads(decrypt_text(source.secret))
            if isinstance(raw, dict):
                persisted = {key: str(value) for key, value in raw.items()}
        except (TypeError, ValueError, json.JSONDecodeError):
            persisted = {}
    access_token = persisted.get("access_token") or persisted.get("api_key", "")
    jellyfin_connected = bool(access_token and persisted.get("user_id"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        base_url = request.form.get("base_url", "").strip()
        enabled = request.form.get("enabled") == "on"
        if not name or len(name) > 255:
            errors["name"] = "Informe um nome com até 255 caracteres."
        try:
            normalized_url = http_url(base_url)
        except AppError:
            errors["base_url"] = "Informe uma URL HTTP ou HTTPS válida."
            normalized_url = base_url
        if not errors:
            if source is None:
                credentials = {
                    "access_token": "",
                    "user_id": "",
                    "user_name": "",
                    "auth_method": "quick_connect",
                }
                source = Source(
                    connector_type=ConnectorType.JELLYFIN,
                    name=name,
                    base_url=normalized_url,
                    secret=encrypt_text(json.dumps(credentials)),
                    enabled=enabled,
                )
                db.session.add(source)
            else:
                base_changed = source.base_url != normalized_url
                source_changed = base_changed or source.enabled != enabled
                source.name = name
                source.base_url = normalized_url
                source.enabled = enabled
                if base_changed:
                    source.secret = encrypt_text(
                        json.dumps(
                            {
                                "access_token": "",
                                "user_id": "",
                                "user_name": "",
                                "auth_method": "quick_connect",
                            },
                            sort_keys=True,
                        )
                    )
                    source.last_connection_status = None
                    source.last_connection_test_at = None
                    _clear_jellyfin_quick_connect()
                if source_changed:
                    reset_source_incremental_state(db.session(), source.id)
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
        jellyfin_connected=jellyfin_connected,
        quick_connect_code=(
            session.get("jellyfin_qc_code")
            if session.get("jellyfin_qc_source_id") == (source.id if source is not None else None)
            else None
        ),
    )


@blueprint.post("/settings/jellyfin/auth/start")
def settings_jellyfin_auth_start() -> Any:
    source = _source(ConnectorType.JELLYFIN)
    if source is None:
        flash("Salve o endereço do servidor antes de conectar ao Jellyfin.", "warning")
        return redirect(url_for("web.settings_jellyfin"))
    client = _jellyfin_quick_connect_client(source)
    try:
        if not client.enabled():
            flash("Ative o Quick Connect no painel do servidor Jellyfin.", "warning")
            return redirect(url_for("web.settings_jellyfin"))
        authorization = client.initiate()
    except ConnectorError:
        current_app.logger.exception("Jellyfin Quick Connect could not be started")
        flash("Não foi possível iniciar o Quick Connect do Jellyfin.", "danger")
        return redirect(url_for("web.settings_jellyfin"))
    finally:
        client.close()
    session["jellyfin_qc_secret"] = encrypt_text(authorization.secret)
    session["jellyfin_qc_code"] = authorization.code
    session["jellyfin_qc_source_id"] = source.id
    return redirect(url_for("web.settings_jellyfin"))


@blueprint.get("/settings/jellyfin/auth/status")
def settings_jellyfin_auth_status() -> Any:
    source_identifier = session.get("jellyfin_qc_source_id")
    encrypted_secret = session.get("jellyfin_qc_secret")
    code = session.get("jellyfin_qc_code")
    if (
        not isinstance(source_identifier, int)
        or not isinstance(encrypted_secret, str)
        or not isinstance(code, str)
    ):
        return render_template("fragments/jellyfin_quick_connect.html", error=True)
    source = db.session.get(Source, source_identifier)
    if source is None or source.connector_type is not ConnectorType.JELLYFIN:
        _clear_jellyfin_quick_connect()
        return render_template("fragments/jellyfin_quick_connect.html", error=True)
    try:
        secret = decrypt_text(encrypted_secret)
    except ValueError:
        _clear_jellyfin_quick_connect()
        return render_template("fragments/jellyfin_quick_connect.html", error=True)
    client = _jellyfin_quick_connect_client(source)
    try:
        if not client.is_authenticated(secret):
            return render_template("fragments/jellyfin_quick_connect.html", code=code, error=False)
        authentication = client.authenticate(secret)
    except ConnectorError:
        current_app.logger.exception("Jellyfin Quick Connect status check failed")
        _clear_jellyfin_quick_connect()
        flash("A autorização do Jellyfin expirou ou falhou. Inicie novamente.", "danger")
        if not _htmx():
            return redirect(url_for("web.settings_jellyfin"))
        response = Response("", status=200)
        response.headers["HX-Redirect"] = url_for("web.settings_jellyfin")
        return response
    finally:
        client.close()
    old_credentials = _jellyfin_credentials(source)
    credentials = {
        "access_token": authentication.access_token,
        "device_id": _jellyfin_device_id(),
        "user_id": authentication.user_id,
        "user_name": authentication.user_name,
        "auth_method": "quick_connect",
    }
    changed = (
        old_credentials.get("access_token") or old_credentials.get("api_key")
    ) != authentication.access_token or old_credentials.get("user_id") != authentication.user_id
    source.secret = encrypt_text(json.dumps(credentials, sort_keys=True))
    source.last_connection_status = None
    source.last_connection_test_at = None
    if changed:
        reset_source_incremental_state(db.session(), source.id)
    db.session.commit()
    _clear_jellyfin_quick_connect()
    flash(
        f"Conta Jellyfin conectada com segurança ({authentication.user_name}).",
        "success",
    )
    if not _htmx():
        return redirect(url_for("web.settings_jellyfin"))
    response = Response("", status=200)
    response.headers["HX-Redirect"] = url_for("web.settings_jellyfin")
    return response


def _jellyfin_quick_connect_client(source: Source) -> JellyfinQuickConnectClient:
    return JellyfinQuickConnectClient(
        source.base_url,
        device_id=_jellyfin_device_id(),
        application_version=version("euvieouvi"),
    )


def _jellyfin_device_id() -> str:
    device_id = _settings("jellyfin.device_id").get("jellyfin.device_id")
    if not device_id:
        device_id = secrets.token_hex(16)
        _save_setting("jellyfin.device_id", device_id)
        db.session.commit()
    return device_id


def _clear_jellyfin_quick_connect() -> None:
    session.pop("jellyfin_qc_secret", None)
    session.pop("jellyfin_qc_code", None)
    session.pop("jellyfin_qc_source_id", None)


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
        "jobs.retry.max_attempts",
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
        attempts_raw = request.form.get("retry_max_attempts", "3").strip()
        try:
            retry_max_attempts = int(attempts_raw)
            if not 1 <= retry_max_attempts <= 10:
                raise ValueError
        except ValueError:
            retry_max_attempts = 3
            errors["retry"] = "Informe um número entre 1 e 10."
        if not errors:
            for job in JOBS:
                _save_setting(
                    setting_key(job.id, "enabled"),
                    "true" if request.form.get(f"enabled_{job.id}") == "on" else "false",
                )
                _save_setting(setting_key(job.id, "time"), parsed[job.id])
            _save_setting("jobs.retention.keep_last", str(retention_keep))
            _save_setting("jobs.retry.max_attempts", str(retry_max_attempts))
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

    metadata = get_enrichment_executor(current_app).snapshot
    images = get_image_executor(current_app).snapshot
    sync_runs = _latest_sync_runs_by_job()
    latest_runs = _latest_job_runs()
    states = {
        job.id: (
            "running"
            if latest_runs.get(job.id) is not None
            and latest_runs[job.id].status in {SyncStatus.QUEUED, SyncStatus.RUNNING}
            else "idle"
        )
        for job in JOBS
    }
    return render_template(
        "jobs.html",
        jobs=JOBS,
        values=values,
        errors=errors,
        states=states,
        metadata=metadata,
        images=images,
        latest_runs=latest_runs,
        sync_runs=sync_runs,
        recent_runs=db.session.scalars(
            select(JobRun).order_by(JobRun.created_at.desc(), JobRun.id.desc()).limit(20)
        ).all(),
        operations=operational_snapshot(),
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
        flash("Job colocado na fila persistente.", "success")
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


@blueprint.get("/jobs/statuses")
def job_statuses() -> Any:
    return jsonify(_cached_job_status_payload())


def _cached_job_status_payload(*, max_age_seconds: float = 0.75) -> dict[str, Any]:
    app = current_app
    cache = app.extensions.get("euvieouvi.job_status_cache")
    if not isinstance(cache, dict):
        cache = {"lock": threading.Lock(), "created": 0.0, "payload": None}
        app.extensions["euvieouvi.job_status_cache"] = cache
    lock = cache["lock"]
    if not isinstance(lock, type(threading.Lock())):
        return _job_status_payload()
    with lock:
        now = time.monotonic()
        payload = cache.get("payload")
        created = float(cache.get("created", 0.0))
        if isinstance(payload, dict) and now - created <= max_age_seconds:
            return payload
        payload = _job_status_payload()
        cache["payload"] = payload
        cache["created"] = now
        return payload


def _job_status_payload() -> dict[str, Any]:
    latest = _latest_job_runs()
    sync_runs = _latest_sync_runs_by_job()
    payload: list[dict[str, Any]] = []
    active = False
    for job in JOBS:
        run = latest.get(job.id)
        if run is not None:
            reconcile_completed_sync_job(run)
            active = active or run.status in {SyncStatus.QUEUED, SyncStatus.RUNNING}
        run_active = run is not None and run.status in {SyncStatus.QUEUED, SyncStatus.RUNNING}
        payload.append(
            {
                "id": job.id,
                "html": render_template("fragments/job_status.html", job=job, run=run),
                "last_run_html": render_template("fragments/job_last_run.html", run=run),
                "actions_html": render_template(
                    "fragments/job_actions.html",
                    job=job,
                    sync_run=sync_runs.get(job.id),
                    active=run_active,
                    run=run,
                ),
            }
        )
    operations = operational_snapshot()
    return {
        "active": active,
        "jobs": payload,
        "dashboard_html": render_template(
            "fragments/dashboard_jobs.html",
            job_definitions=JOBS,
            job_runs=latest,
            operations=operations,
        ),
        "operations_html": render_template(
            "fragments/operational_status.html", operations=operations
        ),
        "operations": operations,
    }


@blueprint.get("/jobs/events")
def job_events() -> Response:
    """Stream job and queue changes without repeated browser requests."""
    once = request.args.get("once") == "1"

    def generate() -> Any:
        last_fingerprint = ""
        last_keepalive = time.monotonic()
        deadline = time.monotonic() + (1 if once else 55)
        while time.monotonic() < deadline:
            db.session.expire_all()
            try:
                payload = _cached_job_status_payload()
            finally:
                # Streaming responses outlive ordinary requests. Return the SQLite
                # connection before waiting so every open browser tab does not pin
                # a read transaction or consume a pool slot for the full stream.
                db.session.remove()
            signature = {
                "active": payload["active"],
                "jobs": payload["jobs"],
                "dashboard_html": payload["dashboard_html"],
                "operations": {
                    key: value
                    for key, value in payload["operations"].items()
                    if key != "generated_at"
                },
            }
            fingerprint = hashlib.sha256(
                json.dumps(signature, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            if fingerprint != last_fingerprint:
                yield "event: jobs\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                last_fingerprint = fingerprint
                last_keepalive = time.monotonic()
            elif time.monotonic() - last_keepalive >= 10:
                yield "event: ping\ndata: {}\n\n"
                last_keepalive = time.monotonic()
            if once:
                return
            time.sleep(1)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@blueprint.get("/jobs/dashboard-fragment")
def dashboard_jobs_fragment() -> Any:
    return render_template(
        "fragments/dashboard_jobs.html",
        job_definitions=JOBS,
        job_runs=_latest_job_runs(),
        operations=operational_snapshot(),
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


@blueprint.get("/diagnostics/download")
def diagnostics_download() -> Any:
    archive = build_diagnostic_archive(current_app)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return send_file(
        archive,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"euvieouvi-diagnostics-{timestamp}.zip",
        max_age=0,
    )


@blueprint.post("/job-runs/<int:run_id>/cancel")
def job_cancel(run_id: int) -> Any:
    if request_job_cancellation(current_app, run_id):
        flash(
            "Cancelamento persistente solicitado; o job parará no próximo ponto seguro.",
            "warning",
        )
    else:
        flash("Este job não está mais ativo.", "warning")
    return redirect(url_for("web.jobs"))


def _latest_job_runs() -> dict[str, JobRun]:
    latest_ids = select(func.max(JobRun.id)).group_by(JobRun.job_id)
    return {
        run.job_id: run
        for run in db.session.scalars(select(JobRun).where(JobRun.id.in_(latest_ids)))
    }


def _latest_sync_runs_by_job() -> dict[str, SyncRun]:
    result: dict[str, SyncRun] = {}
    for job_id, connector_type in (
        ("sync_plex", ConnectorType.PLEX),
        ("sync_jellyfin", ConnectorType.JELLYFIN),
    ):
        latest = db.session.scalar(
            select(SyncRun)
            .join(Source, Source.id == SyncRun.source_id)
            .where(Source.connector_type == connector_type)
            .order_by(SyncRun.created_at.desc(), SyncRun.id.desc())
        )
        if latest is not None:
            result[job_id] = latest
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
        apply_server_identity(db.session(), source, info.server_identifier, now=datetime.now(UTC))
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
        apply_server_identity(db.session(), source, info.server_identifier, now=datetime.now(UTC))
        source.last_connection_status = "succeeded"
        source.last_connection_test_at = datetime.now(UTC)
        db.session.commit()
        flash(f"Conexão com o Jellyfin realizada com sucesso ({info.server_name}).", "success")
    except (ConnectorError, OSError, ValueError):
        source.last_connection_status = "failed"
        source.last_connection_test_at = datetime.now(UTC)
        db.session.commit()
        flash("O Jellyfin não respondeu. Verifique URL, autorização e usuário.", "danger")
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
    application = cast(
        Flask,
        current_app._get_current_object(),  # type: ignore[attr-defined]
    )

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
    plex_users: tuple[ExternalUser, ...] = ()
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
            if plex_users and plex_user_filter not in {user.external_id for user in plex_users}:
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
    has_catalog_reference = (
        db.session.scalar(
            select(SourceMediaRef.id).where(
                SourceMediaRef.source_id == source.id,
                SourceMediaRef.external_id == external_id,
            )
        )
        is not None
    )
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
        progress_ms = _safe_integer(metadata.get("viewOffset"))
        if user_matches and progress_ms is not None and progress_ms > 0:
            _persist_and_enqueue_progress(
                source,
                external_id,
                progress_ms,
                watched_at,
                library_external_id,
            )
            db.session.commit()
            _request_watch_propagation()
        else:
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
    position_ticks = _first_present(
        payload.get("PlaybackPositionTicks"),
        payload.get("PositionTicks"),
        raw_item.get("PlaybackPositionTicks"),
        raw_item.get("PositionTicks"),
    )
    position_ms = _ticks_to_ms(position_ticks)
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
            position_ticks,
            _first_present(payload.get("RunTimeTicks"), raw_item.get("RunTimeTicks")),
        ),
        completed=completed,
        active=notification_type in {"playbackstart", "playbackprogress"},
        event_key=str(payload.get("NotificationId") or "").strip() or None,
    )
    if not completed:
        if configured_user_matches and position_ms is not None and position_ms > 0:
            _persist_and_enqueue_progress(source, external_id, position_ms, watched_at)
            db.session.commit()
            _request_watch_propagation()
        else:
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
            library_external_id=library_external_id,
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
            count = LibraryDiscoveryService(new_session, connector_for(source)).discover(source.id)
            flash(f"Descoberta concluída: {count} biblioteca(s) compatível(is).", "success")
        except (ConnectorError, SyncSourceUnavailableError):
            flash(
                "Não foi possível atualizar as bibliotecas. A lista anterior foi preservada.",
                "danger",
            )
    return redirect(url_for("web.libraries"))


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
            reset_library_incremental_state(db.session(), library.id)
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
        job = db.session.scalar(
            select(JobRun)
            .where(
                JobRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]),
                JobRun.execution_reference == f"sync:{run_id}",
            )
            .order_by(JobRun.id.desc())
        )
        if job is not None:
            request_job_cancellation(current_app, job.id)
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
    availability = request.args.get("availability", str(saved_filters.get("availability", "all")))
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
        "unidentified",
        "title",
        "original_title",
        "year",
        "last_played",
        "first_played",
        "play_count",
        "added",
        "updated",
        "removed",
        "duration",
        "rating",
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
    if ensure_catalog_projection(db.session()) is not None:
        db.session.commit()
    statement = select(MediaItem, CatalogEntry).join(
        CatalogEntry, CatalogEntry.media_item_id == MediaItem.id
    )
    if kind in {item.value for item in allowed_kinds}:
        statement = statement.where(CatalogEntry.kind == kind)
    else:
        statement = statement.where(CatalogEntry.kind.in_(["movie", "show", "artist"]))
    if query:
        query_member = aliased(CatalogEntryMember)
        query_item = aliased(MediaItem)
        statement = statement.where(
            exists(
                select(query_member.media_item_id)
                .select_from(query_member)
                .join(query_item, query_item.id == query_member.media_item_id)
                .where(
                    query_member.catalog_media_item_id == CatalogEntry.media_item_id,
                    query_item.title.ilike(f"%{query}%"),
                )
            )
        )
    if genre:
        genre_member = aliased(CatalogEntryMember)
        statement = statement.where(
            exists(
                select(MediaGenre.id)
                .join(genre_member, genre_member.media_item_id == MediaGenre.media_item_id)
                .join(Genre, Genre.id == MediaGenre.genre_id)
                .where(
                    genre_member.catalog_media_item_id == CatalogEntry.media_item_id,
                    Genre.normalized_name == genre,
                )
            )
        )
    if availability == "available":
        statement = statement.where(CatalogEntry.available.is_(True))
    elif availability == "unavailable":
        statement = statement.where(CatalogEntry.available.is_(False))
    if played == "played":
        statement = statement.where(CatalogEntry.completion_count > 0)
    elif played == "unplayed":
        statement = statement.where(CatalogEntry.completion_count == 0)
    removed_member = aliased(CatalogEntryMember)
    reliable_identity = (
        select(MediaIdentifier.id)
        .join(CatalogEntryMember, CatalogEntryMember.media_item_id == MediaIdentifier.media_item_id)
        .where(
            CatalogEntryMember.catalog_media_item_id == CatalogEntry.media_item_id,
            MediaIdentifier.provider.in_(
                ("tmdb", "tvdb", "imdb", "musicbrainz", "mbid", "mbid-release", "mbid-artist")
            ),
            MediaIdentifier.external_id != "",
        )
        .correlate(CatalogEntry)
        .exists()
    )
    sort_columns = {
        "title": func.coalesce(MediaItem.sort_title, MediaItem.title),
        "original_title": func.coalesce(MediaItem.original_title, MediaItem.title),
        "year": MediaItem.year,
        "last_played": CatalogEntry.last_played_at,
        "first_played": CatalogEntry.first_played_at,
        "play_count": CatalogEntry.completion_count,
        "added": func.coalesce(MediaItem.source_added_at, MediaItem.created_at),
        "updated": MediaItem.updated_at,
        "removed": select(func.max(SourceMediaRef.unavailable_since))
        .join(removed_member, removed_member.media_item_id == SourceMediaRef.media_item_id)
        .where(removed_member.catalog_media_item_id == CatalogEntry.media_item_id)
        .scalar_subquery(),
        "duration": MediaItem.duration_ms,
        "rating": MediaItem.audience_rating,
    }
    sort_column = sort_columns.get(sort, sort_columns["title"])
    ordering = (
        sort_column.desc().nullslast() if direction == "desc" else sort_column.asc().nullslast()
    )
    orderings = [ordering, MediaItem.title, MediaItem.id]
    if sort == "unidentified":
        orderings = [case((reliable_identity, 1), else_=0).asc(), ordering, MediaItem.id]
    result_rows = db.session.execute(
        statement.order_by(*orderings).limit(41).offset((page - 1) * 40)
    ).all()
    has_more = len(result_rows) > 40
    visible_rows = result_rows[:40]
    merged_rows = [
        (
            item,
            entry.completion_count,
            entry.last_completed_at,
            entry.available_plex,
            entry.available_jellyfin,
        )
        for item, entry in visible_rows
    ]
    entries_by_item = {item.id: entry for item, entry in visible_rows}
    watch_progress: dict[int, dict[str, int | str]] = {}
    for item, entry in visible_rows:
        if item.kind is not MediaKind.SHOW:
            continue
        total = entry.child_count
        completed = entry.completed_child_count
        percent = min(100, (completed * 100 + total // 2) // total) if total else 0
        if completed == 0:
            state, icon, label = "unwatched", "—", "Não assistida"
        elif completed < total:
            state, icon, label = "watching", "◐", "Assistindo"
        else:
            state, icon, label = "watched", "✓", "Assistida"
        watch_progress[item.id] = {
            "state": state,
            "icon": icon,
            "percent": percent,
            "title": f"{label} · {completed} de {total} episódios · Progresso: {percent}%",
        }

    displayed_movies = {
        row[0].id: (row[0], int(row[1])) for row in merged_rows if row[0].kind is MediaKind.MOVIE
    }
    for media_id, (item, completions) in displayed_movies.items():
        progress_ms = entries_by_item[media_id].progress_ms
        if completions > 0:
            state, icon, percent = "watched", "✓", 100
            action = f"Assistido {completions} {'vez' if completions == 1 else 'vezes'}"
        elif progress_ms > 0:
            state, icon = "watching", "◐"
            percent = (
                min(99, max(1, round(progress_ms * 100 / item.duration_ms)))
                if item.duration_ms
                else 0
            )
            action = "Assistindo"
        else:
            state, icon, percent, action = "unwatched", "—", 0, "Não assistido"
        progress_label = (
            f"Progresso: {percent}%"
            if item.duration_ms or percent in {0, 100}
            else "Progresso indisponível"
        )
        watch_progress[media_id] = {
            "state": state,
            "icon": icon,
            "percent": percent,
            "title": f"{action} · {progress_label}",
        }
    template_values = dict(
        artwork_versions=_artwork_versions([item.id for item, _ in visible_rows]),
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
        watch_progress=watch_progress,
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
    refresh_values = {key: value for key, value in request.args.items() if key != "fragment"}
    refresh_values["fragment"] = "1"
    template_values["catalog_refresh_url"] = f"{url_for('web.catalog')}?{urlencode(refresh_values)}"
    template_values["catalog_revision_url"] = url_for("web.catalog_revision")
    template_values["catalog_revision"] = _catalog_revision_value()
    return render_template("catalog.html", **template_values)


@blueprint.get("/catalog/revision")
def catalog_revision() -> Any:
    return jsonify({"revision": _catalog_revision_value()})


def _catalog_revision_value() -> str:
    values = db.session.execute(
        select(
            select(func.max(MediaItem.updated_at)).scalar_subquery(),
            select(func.max(SourceMediaRef.updated_at)).scalar_subquery(),
            select(func.max(MediaImage.updated_at)).scalar_subquery(),
            select(func.max(WatchState.updated_at)).scalar_subquery(),
            select(func.max(WatchEvent.id)).scalar_subquery(),
        )
    ).one()
    return "|".join(str(value or "") for value in values)


@blueprint.get("/media/<int:media_id>/image")
def media_image(media_id: int) -> Any:
    item = db.session.get(MediaItem, media_id)
    if item is None:
        return Response(status=404)
    cache_directory = Path(current_app.instance_path) / "images"
    square = item.kind in {MediaKind.ARTIST, MediaKind.ALBUM, MediaKind.TRACK}
    candidates = _artwork_candidate_items(item)
    for image in _artwork_candidate_images(candidates):
        if image.provider not in {"plex", "jellyfin"}:
            try:
                path = ensure_external_cached(image, cache_directory)
                db.session.commit()
            except (OSError, ValueError):
                db.session.rollback()
                continue
            response = send_file(path, mimetype=image.mime_type, conditional=True, max_age=86400)
            response.headers["Cache-Control"] = "private, max-age=86400"
            return response
        source = db.session.get(Source, image.source_id)
        if source is None or not source.enabled:
            db.session.delete(image)
            db.session.commit()
            continue
        available = db.session.scalar(
            select(SourceMediaRef.id).where(
                SourceMediaRef.media_item_id == image.media_item_id,
                SourceMediaRef.source_id == source.id,
                SourceMediaRef.available.is_(True),
            )
        )
        if available is None or image.source_path is None:
            db.session.delete(image)
            db.session.commit()
            continue
        connector = None
        try:
            connector = connector_for(source)
            content, mime_type = connector.fetch_image(
                image.source_path,
                width=400 if square else 300,
                height=400 if square else 450,
            )
            response = Response(content, mimetype=mime_type)
            response.headers["Cache-Control"] = "private, max-age=86400"
            response.set_etag(hashlib.sha256(content).hexdigest())
            return response.make_conditional(request)
        except ConnectorNotFoundError:
            db.session.rollback()
            stale = db.session.get(MediaImage, image.id)
            if stale is not None:
                db.session.delete(stale)
                db.session.commit()
        except (ConnectorError, OSError, ValueError):
            db.session.rollback()
        finally:
            close = getattr(connector, "close", None)
            if callable(close):
                close()
    recovery_item = next(
        (
            candidate
            for candidate in candidates
            if candidate.kind in {MediaKind.MOVIE, MediaKind.SHOW, MediaKind.ALBUM, MediaKind.TRACK}
        ),
        item,
    )
    _queue_missing_artwork_recovery(recovery_item)
    return _placeholder_image(item.kind)


def _artwork_candidate_items(item: MediaItem) -> list[MediaItem]:
    """Return direct, canonical-equivalent, then parent artwork candidates."""
    candidates = [item]
    seen = {item.id}
    catalog_media_id = db.session.scalar(
        select(CatalogEntryMember.catalog_media_item_id).where(
            CatalogEntryMember.media_item_id == item.id
        )
    )
    if catalog_media_id is None and db.session.get(CatalogEntry, item.id) is not None:
        catalog_media_id = item.id
    if catalog_media_id is not None:
        member_ids = db.session.scalars(
            select(CatalogEntryMember.media_item_id)
            .where(CatalogEntryMember.catalog_media_item_id == catalog_media_id)
            .order_by(CatalogEntryMember.media_item_id)
        ).all()
        for member_id in member_ids:
            member = db.session.get(MediaItem, member_id)
            if member is not None and member.id not in seen:
                candidates.append(member)
                seen.add(member.id)
    frontier = list(candidates)
    for _ in range(3):
        parents: list[MediaItem] = []
        for candidate in frontier:
            if candidate.parent_id is None or candidate.parent_id in seen:
                continue
            parent = db.session.get(MediaItem, candidate.parent_id)
            if parent is not None:
                candidates.append(parent)
                parents.append(parent)
                seen.add(parent.id)
        if not parents:
            break
        frontier = parents
    return candidates


def _artwork_candidate_images(candidates: Sequence[MediaItem]) -> list[MediaImage]:
    if not candidates:
        return []
    images = {
        image.media_item_id: image
        for image in db.session.scalars(
            select(MediaImage).where(
                MediaImage.media_item_id.in_([candidate.id for candidate in candidates]),
                MediaImage.image_type == "poster",
            )
        )
    }
    return [images[candidate.id] for candidate in candidates if candidate.id in images]


def _queue_missing_artwork_recovery(item: MediaItem) -> None:
    tmdb_enabled = db.session.get(Setting, "metadata.tmdb.enabled")
    tmdb_token = setting_value(db.session.get(Setting, "metadata.tmdb.token"))
    musicbrainz_enabled = db.session.get(Setting, "metadata.musicbrainz.enabled")
    allowed: set[str] = set()
    if tmdb_enabled is not None and tmdb_enabled.value == "true" and tmdb_token:
        if item.kind is MediaKind.MOVIE:
            allowed.update(("tmdb", "imdb"))
        elif item.kind is MediaKind.SHOW:
            allowed.update(("tmdb", "imdb", "tvdb"))
    if musicbrainz_enabled is not None and musicbrainz_enabled.value == "true":
        if item.kind is MediaKind.ALBUM:
            allowed.add("mbid-release")
        elif item.kind is MediaKind.TRACK:
            allowed.add("mbid")
    if not allowed:
        return
    providers = set(
        db.session.scalars(
            select(MediaIdentifier.provider).where(
                MediaIdentifier.media_item_id == item.id,
                MediaIdentifier.provider.in_(allowed),
            )
        )
    )
    if not providers:
        return
    permanently_missing = set(
        db.session.scalars(
            select(EnrichmentRecord.provider).where(
                EnrichmentRecord.media_item_id == item.id,
                EnrichmentRecord.provider.in_([f"artwork:{value}" for value in providers]),
                EnrichmentRecord.status == "not_found",
            )
        )
    )
    if all(f"artwork:{provider}" in permanently_missing for provider in providers):
        return
    try:
        submit_job(current_app, "catalog_images", trigger=SyncTrigger.API)
    except OperationalError:
        db.session.rollback()
        current_app.logger.warning(
            "Could not queue external artwork recovery for media %s due to SQLite contention",
            item.id,
        )


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
        artwork_version=_artwork_versions([item.id]).get(item.id, "0"),
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
        physical_paths=db.session.execute(
            select(MediaFilePath, Source.name)
            .join(SourceMediaRef, SourceMediaRef.id == MediaFilePath.reference_id)
            .join(Source, Source.id == SourceMediaRef.source_id)
            .where(
                SourceMediaRef.media_item_id.in_(
                    [media_id, *(child.id for child in children_for_states)]
                )
            )
            .order_by(Source.name, MediaFilePath.original_path)
            .limit(100)
        ).all(),
        series_title=_series_titles([item]).get(item.id),
    )


@blueprint.route("/media/<int:media_id>/match", methods=["GET", "POST"])
def media_match(media_id: int) -> Any:
    item = db.session.get(MediaItem, media_id)
    if item is None or item.kind not in {MediaKind.MOVIE, MediaKind.SHOW}:
        return Response(status=404)
    values = _settings("metadata.tmdb.token", "metadata.language")
    token = values.get("metadata.tmdb.token", "")
    query = request.args.get("q", "").strip()[:200]
    matches: tuple[TmdbMatch, ...] = ()
    if not token:
        flash("Configure a credencial TMDB em Metadados para corrigir a identificação.", "warning")
    elif request.method == "POST" or query:
        client = TmdbClient(token)
        try:
            media_type = "movie" if item.kind is MediaKind.MOVIE else "tv"
            language = values.get("metadata.language", "en-US")
            if request.method == "POST":
                if _active_sync_exists():
                    flash(
                        "Aguarde a sincronização terminar antes de corrigir a identificação.",
                        "warning",
                    )
                    return redirect(url_for("web.media_match", media_id=media_id))
                match = client.match_details(
                    media_type, request.form.get("tmdb_id", ""), language=language
                )
                media_id = apply_manual_match(db.session(), item, match)
                db.session.commit()
                current_app.logger.info(
                    "Manual TMDB match media=%s kind=%s tmdb=%s title=%r",
                    media_id,
                    item.kind.value,
                    match.external_id,
                    match.title,
                )
                submit_job(current_app, "catalog_images")
                flash(
                    "Identificação corrigida. Histórico preservado; "
                    "imagens adicionadas à fila de download.",
                    "success",
                )
                return redirect(url_for("web.media_detail", media_id=media_id))
            matches = client.search(media_type, query, language=language)
        except (EnrichmentError, ValueError) as error:
            db.session.rollback()
            current_app.logger.warning(
                "TMDB match failed media=%s error=%s", media_id, error, exc_info=True
            )
            flash(
                "Não foi possível consultar o TMDB. Verifique a credencial e tente novamente.",
                "danger",
            )
        finally:
            client.close()
    return render_template(
        "media_match.html", item=item, query=query, matches=matches, configured=bool(token)
    )


@blueprint.route("/settings/media-identity", methods=["GET", "POST"])
def settings_media_identity() -> Any:
    sources = db.session.scalars(select(Source).order_by(Source.name)).all()
    if request.method == "POST":
        if _active_sync_exists():
            flash("Aguarde a sincronização terminar antes de alterar os caminhos.", "warning")
            return redirect(url_for("web.settings_media_identity"))
        roots: dict[str, list[dict[str, str]]] = {}
        try:
            for source in sources:
                mappings: list[dict[str, str]] = []
                for line in request.form.get(f"roots_{source.id}", "").splitlines():
                    if not line.strip():
                        continue
                    parts = line.split("=>")
                    if len(parts) != 2:
                        raise ValueError("mapping needs source => canonical")
                    original, canonical = normalize_path(parts[0]), normalize_path(parts[1])
                    if not original or not canonical:
                        raise ValueError("mapping paths must be absolute")
                    if any(row["source"] == original for row in mappings):
                        raise ValueError("duplicate mapping root")
                    mappings.append({"source": original, "canonical": canonical})
                roots[str(source.id)] = mappings
            _save_setting(ROOTS_KEY, json.dumps(roots))
            remap_paths(db.session())
            for source in sources:
                reset_source_incremental_state(db.session(), source.id)
            db.session.commit()
            flash(
                "Caminhos salvos. A próxima sincronização coletará "
                "as referências físicas completas.",
                "success",
            )
        except ValueError:
            db.session.rollback()
            flash(
                "Use um caminho absoluto por linha no formato origem => compartilhado, "
                "sem raízes repetidas.",
                "danger",
            )
        return redirect(url_for("web.settings_media_identity"))
    setting = db.session.get(Setting, ROOTS_KEY)
    roots = json.loads(setting.value) if setting and setting.value else {}
    return render_template("settings_media_identity.html", sources=sources, roots=roots)


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
        item.key: value
        for item in db.session.scalars(select(Setting).where(Setting.key.in_(keys)))
        if (value := setting_value(item)) is not None
    }


def _save_setting(key: str, value: str) -> None:
    save_setting_value(db.session(), key, value)


def _valid_webhook_token(provider: str, supplied: str) -> bool:
    expected = _settings(f"webhook.{provider}.token").get(f"webhook.{provider}.token")
    return bool(expected and secrets.compare_digest(expected, supplied))


def _jellyfin_credentials(source: Source) -> dict[str, str]:
    try:
        raw = json.loads(decrypt_text(source.secret))
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


def _persist_and_enqueue_progress(
    source: Source,
    external_id: str,
    progress_ms: int,
    observed_at: datetime,
    library_external_id: str | None = None,
) -> None:
    resolved_library_external_id = library_external_id
    reference = db.session.scalar(
        select(SourceMediaRef).where(
            SourceMediaRef.source_id == source.id,
            SourceMediaRef.external_id == external_id,
            SourceMediaRef.available.is_(True),
        )
    )
    if reference is not None:
        if not resolved_library_external_id:
            library = db.session.get(Library, reference.library_id)
            resolved_library_external_id = library.external_id if library is not None else None
        state = db.session.scalar(
            select(WatchState).where(
                WatchState.media_item_id == reference.media_item_id,
                WatchState.source_id == source.id,
            )
        )
        if state is None:
            state = WatchState(
                media_item_id=reference.media_item_id,
                source_id=source.id,
                view_count=0,
                completed=False,
                observed_at=observed_at,
            )
            db.session.add(state)
        current_played = state.last_played_at
        if current_played is None or observed_at > current_played.replace(tzinfo=UTC):
            state.last_played_at = observed_at
        if state.completed:
            return
        state.completed = False
        state.progress_ms = progress_ms
        state.observed_at = observed_at
    enqueue_watch_update(
        db.session(),
        source_id=source.id,
        external_id=external_id,
        watched_at=observed_at,
        progress_ms=progress_ms,
        completed=False,
        library_external_id=resolved_library_external_id,
    )


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
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _artwork_versions(media_ids: list[int]) -> dict[int, str]:
    """Version artwork URLs in one bounded query, including grouped and inherited covers."""
    if not media_ids:
        return {}
    member = aliased(MediaItem)
    parent = aliased(MediaItem)
    grandparent = aliased(MediaItem)
    versions = db.session.execute(
        select(MediaItem.id, func.max(MediaImage.updated_at))
        .outerjoin(CatalogEntryMember, CatalogEntryMember.catalog_media_item_id == MediaItem.id)
        .outerjoin(member, member.id == CatalogEntryMember.media_item_id)
        .outerjoin(parent, parent.id == func.coalesce(member.parent_id, MediaItem.parent_id))
        .outerjoin(grandparent, grandparent.id == parent.parent_id)
        .outerjoin(
            MediaImage,
            MediaImage.media_item_id.in_((MediaItem.id, member.id, parent.id, grandparent.id)),
        )
        .where(MediaItem.id.in_(media_ids))
        .group_by(MediaItem.id)
    )
    return {
        media_id: hashlib.sha256(str(updated_at).encode()).hexdigest()[:12]
        for media_id, updated_at in versions
    }
