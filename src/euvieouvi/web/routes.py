"""Jinja pages and HTMX fragments for the local interface."""

from __future__ import annotations

import contextlib
import json
import secrets
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
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
from sqlalchemy import and_, case, delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from euvieouvi.api.runtime import connector_for, get_executor
from euvieouvi.api.validation import http_url
from euvieouvi.connectors.dtos import ExternalWatchEvent
from euvieouvi.connectors.errors import ConnectorError
from euvieouvi.database.enums import ConnectorType, MediaKind, SyncStatus
from euvieouvi.database.models import (
    Genre,
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
        "ui_theme": _settings("ui.theme").get("ui.theme", "system"),
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
        next_step = ("Execute a primeira sincronização", "sync_list")
    return render_template(
        "dashboard.html",
        counts=counts,
        source=source,
        next_step=next_step,
        last_run=last_run,
        recent=recent,
        series_titles=_series_titles([row[1] for row in recent]),
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


@blueprint.route("/settings/appearance", methods=["GET", "POST"])
def settings_appearance() -> Any:
    theme = _settings("ui.theme").get("ui.theme", "system")
    if theme not in {"system", "light", "dark"}:
        theme = "system"
    if request.method == "POST":
        selected = request.form.get("theme", "system").strip()
        if selected not in {"system", "light", "dark"}:
            flash("Selecione uma preferência de tema válida.", "danger")
        else:
            _save_setting("ui.theme", selected)
            db.session.commit()
            flash("Preferência de aparência atualizada.", "success")
            return redirect(url_for("web.settings_appearance"))
    return render_template("settings_appearance.html", theme=theme)


@blueprint.route("/settings/sync", methods=["GET", "POST"])
def settings_sync() -> Any:
    values = _settings(
        "sync.schedule.enabled",
        "sync.schedule.time",
        "sync.schedule.mode",
        "sync.schedule.time.plex",
        "sync.schedule.time.jellyfin",
        "sync.schedule.enabled.plex",
        "sync.schedule.enabled.jellyfin",
        "sync.schedule.last_date",
        "sync.schedule.last_date.plex",
        "sync.schedule.last_date.jellyfin",
        "watch_sync.enabled",
    )
    # Compat: legacy single enabled/time -> migrate view defaults
    legacy_enabled = values.get("sync.schedule.enabled", "false") == "true"
    legacy_time = values.get("sync.schedule.time", "03:00")
    errors: dict[str, str] = {}
    if request.method == "POST":
        # Backwards compat: legacy fields 'enabled' / 'scheduled_time'
        if "enabled" in request.form or "scheduled_time" in request.form:
            legacy_post_enabled = request.form.get("enabled") == "on"
            legacy_sched_raw = request.form.get("scheduled_time", "").strip()
            # always validate legacy scheduled_time if provided (even when disabled)
            parsed_legacy: datetime | None = None
            if legacy_sched_raw:
                try:
                    parsed_legacy = datetime.strptime(legacy_sched_raw, "%H:%M")
                except ValueError:
                    errors["scheduled_time"] = "Informe um horário válido entre 00:00 e 23:59."
                    errors["time_shared"] = "Informe um horário válido entre 00:00 e 23:59."
            elif legacy_post_enabled:
                errors["scheduled_time"] = "Informe um horário válido entre 00:00 e 23:59."
                errors["time_shared"] = "Informe um horário válido entre 00:00 e 23:59."
            if not errors and parsed_legacy is not None:
                _save_setting("sync.schedule.enabled", "true" if legacy_post_enabled else "false")
                _save_setting(
                    "sync.schedule.enabled.plex", "true" if legacy_post_enabled else "false"
                )
                _save_setting("sync.schedule.time", parsed_legacy.strftime("%H:%M"))
                _save_setting("sync.schedule.time.plex", parsed_legacy.strftime("%H:%M"))
                # keep shared mode for legacy
                _save_setting("sync.schedule.mode", "shared")
                db.session.commit()
                flash("Agendamento diário atualizado.", "success")
                return redirect(url_for("web.settings_sync"))
            # if errors, fall through to render with errors
        else:
            mode = request.form.get("mode", "shared").strip()
            if mode not in {"shared", "per_source"}:
                mode = "shared"
            enabled_plex = request.form.get("enabled_plex") == "on"
            enabled_jellyfin = request.form.get("enabled_jellyfin") == "on"
            watch_sync_enabled = request.form.get("watch_sync_enabled") == "on"
            time_shared = request.form.get("time_shared", "").strip() or legacy_time
            time_plex = request.form.get("time_plex", "").strip() or values.get(
                "sync.schedule.time.plex", legacy_time
            )
            time_jellyfin = request.form.get("time_jellyfin", "").strip() or values.get(
                "sync.schedule.time.jellyfin", legacy_time
            )

            # validate times that are actually used
            def _parse(t: str, field: str) -> datetime | None:
                try:
                    return datetime.strptime(t, "%H:%M")
                except ValueError:
                    errors[field] = "Informe um horário válido entre 00:00 e 23:59."
                    return None

            parsed_shared = (
                _parse(time_shared, "time_shared")
                if (enabled_plex or enabled_jellyfin) and mode == "shared"
                else None
            )
            parsed_plex = (
                _parse(time_plex, "time_plex") if enabled_plex and mode == "per_source" else None
            )
            parsed_jelly = (
                _parse(time_jellyfin, "time_jellyfin")
                if enabled_jellyfin and mode == "per_source"
                else None
            )
            if not (enabled_plex or enabled_jellyfin):
                # allow disabling all -> still valid, no time needed
                pass
            elif mode == "shared" and parsed_shared is None and "time_shared" not in errors:
                errors["time_shared"] = "Informe um horário válido entre 00:00 e 23:59."
            elif mode == "per_source":
                if enabled_plex and parsed_plex is None:
                    pass
                if enabled_jellyfin and parsed_jelly is None:
                    pass
            if not errors:
                _save_setting("sync.schedule.mode", mode)
                _save_setting("sync.schedule.enabled.plex", "true" if enabled_plex else "false")
                _save_setting(
                    "sync.schedule.enabled.jellyfin", "true" if enabled_jellyfin else "false"
                )
                _save_setting("watch_sync.enabled", "true" if watch_sync_enabled else "false")
                # keep legacy keys for compat
                _save_setting(
                    "sync.schedule.enabled",
                    "true" if (enabled_plex or enabled_jellyfin) else "false",
                )
                if mode == "shared" and parsed_shared is not None:
                    _save_setting("sync.schedule.time", parsed_shared.strftime("%H:%M"))
                    _save_setting("sync.schedule.time.plex", parsed_shared.strftime("%H:%M"))
                    _save_setting("sync.schedule.time.jellyfin", parsed_shared.strftime("%H:%M"))
                else:
                    if parsed_plex is not None:
                        _save_setting("sync.schedule.time.plex", parsed_plex.strftime("%H:%M"))
                    if parsed_jelly is not None:
                        _save_setting("sync.schedule.time.jellyfin", parsed_jelly.strftime("%H:%M"))
                    # keep shared time for fallback
                    if parsed_shared is not None:
                        _save_setting("sync.schedule.time", parsed_shared.strftime("%H:%M"))
                db.session.commit()
                if watch_sync_enabled:
                    with contextlib.suppress(Exception):
                        get_executor(current_app).submit_pending_watch_sync()
                flash("Agendamento diário atualizado.", "success")
                return redirect(url_for("web.settings_sync"))
    # GET defaults
    mode_val = values.get("sync.schedule.mode", "shared")
    if mode_val not in {"shared", "per_source"}:
        mode_val = "shared"
    enabled_plex_val = (
        values.get("sync.schedule.enabled.plex", "true" if legacy_enabled else "false") == "true"
    )
    enabled_jelly_val = values.get("sync.schedule.enabled.jellyfin", "false") == "true"
    # if per-source keys absent, fallback to legacy
    if (
        "sync.schedule.enabled.plex" not in values
        and "sync.schedule.enabled.jellyfin" not in values
    ):
        enabled_plex_val = legacy_enabled
        enabled_jelly_val = False
    return render_template(
        "settings_sync.html",
        mode=mode_val,
        enabled_plex=enabled_plex_val,
        enabled_jellyfin=enabled_jelly_val,
        watch_sync_enabled=values.get("watch_sync.enabled", "false") == "true",
        time_shared=values.get("sync.schedule.time", legacy_time),
        time_plex=values.get("sync.schedule.time.plex", values.get("sync.schedule.time", "03:00")),
        time_jellyfin=values.get(
            "sync.schedule.time.jellyfin", values.get("sync.schedule.time", "03:00")
        ),
        timezone=current_app.config["TIMEZONE"],
        errors=errors,
        legacy_enabled=legacy_enabled,
        legacy_time=legacy_time,
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
    return render_template(
        "settings_backup.html",
        backup_enabled=values.get("backup.schedule.enabled", "false") == "true",
        backup_time=values.get("backup.schedule.time", "04:00"),
        backup_keep=values.get("backup.retention.keep_last", "15"),
        sync_keep=values.get("sync.retention.keep_last", "15"),
        backups=backups,
        timezone=current_app.config["TIMEZONE"],
        errors=errors,
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


@blueprint.route("/settings/webhooks", methods=["GET", "POST"])
def settings_webhooks() -> Any:
    tokens = _settings("webhook.plex.token", "webhook.jellyfin.token", "webhook.history_limit")
    changed = False
    for provider in ("plex", "jellyfin"):
        key = f"webhook.{provider}.token"
        if not tokens.get(key):
            tokens[key] = secrets.token_urlsafe(32)
            _save_setting(key, tokens[key])
            changed = True
    if changed:
        db.session.commit()
    if request.method == "POST":
        raw_limit = (request.form.get("history_limit") or "20").strip()
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
        history_limit=history_limit,
        recent_events=recent_events,
        current_events=current_events,
    )


@blueprint.get("/settings/webhooks/activity-fragment")
def webhook_activity_fragment() -> Any:
    history_limit = _webhook_history_limit()
    recent_events, current_events = _webhook_activity(history_limit)
    return render_template(
        "fragments/webhook_activity.html",
        history_limit=history_limit,
        recent_events=recent_events,
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
    external_id = str(metadata.get("ratingKey") or "").strip()
    library_external_id = str(metadata.get("librarySectionID") or "").strip()
    if not external_id:
        return Response("Identidade da mídia ausente.", 400)
    watched_at = _parse_webhook_datetime(payload.get("eventTime")) or datetime.now(UTC)
    title = str(metadata.get("title") or metadata.get("grandparentTitle") or external_id)
    media_kind = metadata.get("type")
    series_title = metadata.get("grandparentTitle") if media_kind == "episode" else None
    active = event_type in {"media.play", "media.resume"}
    completed = event_type == "media.scrobble"
    _record_webhook_activity(
        source,
        external_id=external_id,
        title=title,
        series_title=series_title,
        media_kind=media_kind,
        event_type=event_type,
        occurred_at=watched_at,
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
    _persist_webhook_event(
        source,
        external_id=external_id,
        library_external_id=library_external_id,
        watched_at=watched_at,
        source_event_id=None,
        duration_ms=_safe_integer(metadata.get("duration")),
    )
    db.session.commit()
    _request_watch_propagation()
    return Response(status=204)


@blueprint.post("/webhooks/jellyfin/<token>")
def jellyfin_webhook(token: str) -> Any:
    if not _valid_webhook_token("jellyfin", token):
        return Response(status=404)
    source = _source(ConnectorType.JELLYFIN)
    payload = request.get_json(silent=True)
    if source is None or not isinstance(payload, dict):
        return Response(status=204)
    notification_type = "".join(
        char for char in str(payload.get("NotificationType") or "").casefold() if char.isalnum()
    )
    if notification_type not in {"playbackstart", "playbackstop"}:
        return Response(status=204)
    credentials = _jellyfin_credentials(source)
    user_id = str(payload.get("UserId") or "").strip()
    if credentials.get("user_id") and user_id != credentials["user_id"]:
        return Response(status=204)
    payload_item = payload.get("Item")
    raw_item: dict[str, Any] = payload_item if isinstance(payload_item, dict) else {}
    external_id = str(payload.get("ItemId") or raw_item.get("Id") or "").strip()
    timestamp_value = payload.get("UtcTimestamp")
    watched_at = _parse_webhook_datetime(timestamp_value)
    if timestamp_value and watched_at is None:
        return Response("Identidade ou data ausente.", 400)
    watched_at = watched_at or datetime.now(UTC)
    if not external_id:
        return Response("Identidade ou data ausente.", 400)
    completed = notification_type == "playbackstop" and _truthy(payload.get("PlayedToCompletion"))
    media_kind = str(payload.get("ItemType") or raw_item.get("Type") or "") or None
    series_title = (
        (payload.get("SeriesName") or raw_item.get("SeriesName"))
        if media_kind and media_kind.casefold() == "episode"
        else None
    )
    _record_webhook_activity(
        source,
        external_id=external_id,
        title=str(
            payload.get("Name") or payload.get("ItemName") or raw_item.get("Name") or external_id
        ),
        series_title=series_title,
        media_kind=media_kind,
        event_type=notification_type,
        occurred_at=watched_at,
        completed=completed,
        active=notification_type == "playbackstart",
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
        )
        # also queue a sync to enrich metadata, but don't block webhook response
        with contextlib.suppress(Exception):
            _queue_source_sync(source.id)
        db.session.commit()
        _request_watch_propagation()
        return Response(status=204)
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
    db.session.commit()
    _request_watch_propagation()
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
        target = (request.form.get("targets") or "both").strip().lower()
        allowed_targets = {"plex", "jellyfin", "both"}
        if target not in allowed_targets:
            target = "both"
        type_filter = None
        if target == "plex":
            type_filter = ConnectorType.PLEX
        elif target == "jellyfin":
            type_filter = ConnectorType.JELLYFIN
        stmt = select(Source.id).where(
            Source.enabled.is_(True),
            exists().where(
                Library.source_id == Source.id,
                Library.enabled.is_(True),
                Library.available.is_(True),
            ),
        )
        if type_filter is not None:
            stmt = stmt.where(Source.connector_type == type_filter)
        source_ids = tuple(db.session.scalars(stmt.order_by(Source.id)).all())
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
    runs = db.session.execute(
        select(SyncRun, Source)
        .join(Source, Source.id == SyncRun.source_id)
        .order_by(SyncRun.created_at.desc(), SyncRun.id.desc())
        .limit(50)
    ).all()
    active = db.session.execute(
        select(SyncRun, Source)
        .join(Source, Source.id == SyncRun.source_id)
        .where(SyncRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
        .order_by(SyncRun.id.desc())
    ).first()
    watch_sync_run = next(
        (run for run, _source_value in runs if run.status is SyncStatus.SUCCEEDED),
        None,
    )
    return render_template(
        "sync_list.html",
        runs=runs,
        active_run=active[0] if active else None,
        active_source=active[1] if active else None,
        watch_sync_run=watch_sync_run,
    )


@blueprint.get("/sync/active-fragment")
def sync_active_fragment() -> Any:
    active = db.session.execute(
        select(SyncRun, Source)
        .join(Source, Source.id == SyncRun.source_id)
        .where(SyncRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
        .order_by(SyncRun.id.desc())
    ).first()
    return render_template(
        "fragments/sync_active.html",
        run=active[0] if active else None,
        source=active[1] if active else None,
    )


@blueprint.get("/sync/<int:run_id>")
def sync_detail(run_id: int) -> Any:
    context = _sync_detail_context(run_id)
    if context is None:
        return render_template("errors/404.html"), 404
    return render_template("sync_detail.html", **context)


@blueprint.get("/sync/<int:run_id>/fragment")
def sync_detail_fragment(run_id: int) -> Any:
    context = _sync_detail_context(run_id)
    if context is None:
        return Response("Sincronização não encontrada.", 404)
    return render_template("fragments/sync_detail_content.html", **context)


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
        "watch_sync_enabled": _settings("watch_sync.enabled").get("watch_sync.enabled", "false")
        == "true",
    }


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


@blueprint.post("/sync/<int:run_id>/watch-sync")
def sync_watch_sync(run_id: int) -> Any:
    run = db.session.get(SyncRun, run_id)
    if run is None:
        return Response("Sincronização não encontrada.", 404)
    if get_executor(current_app).submit_watch_sync(run_id):
        flash("Propagação de conclusões iniciada.", "success")
    else:
        flash("A propagação não pode ser iniciada para esta sincronização.", "warning")
    return redirect(url_for("web.sync_detail", run_id=run_id))


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
        MediaKind.EPISODE,
        MediaKind.ARTIST,
        MediaKind.ALBUM,
        MediaKind.TRACK,
    }
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
        (event_stats.c.event_last.is_(None), state_stats.c.state_last),
        (state_stats.c.state_last.is_(None), event_stats.c.event_last),
        (event_stats.c.event_last >= state_stats.c.state_last, event_stats.c.event_last),
        else_=state_stats.c.state_last,
    )
    completed_known = completion_count > 0
    statement = (
        select(
            MediaItem,
            completion_count.label("completion_count"),
            last_completed.label("last_completed"),
        )
        .outerjoin(event_stats, event_stats.c.media_item_id == MediaItem.id)
        .outerjoin(state_stats, state_stats.c.media_item_id == MediaItem.id)
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
        statement = statement.where(completed_known)
    elif played == "unplayed":
        statement = statement.where(~completed_known)
    sort_columns = {
        "title": func.coalesce(MediaItem.sort_title, MediaItem.title),
        "original_title": func.coalesce(MediaItem.original_title, MediaItem.title),
        "year": MediaItem.year,
        "last_played": last_completed,
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
        availability = availability_by_item[item.id]
        display_row = (
            item,
            row[1],
            row[2],
            ConnectorType.PLEX in availability,
            ConnectorType.JELLYFIN in availability,
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
    return render_template(
        "catalog.html",
        rows=merged_rows,
        page=page,
        has_more=has_more,
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
        series_titles=_series_titles([row[0] for row in merged_rows]),
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
        known_last = max(
            (value for value in (event_last, state_last) if value is not None),
            default=None,
        )
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
    last_known = case(
        (event_last.is_(None), state_last),
        (state_last.is_(None), event_last),
        (event_last >= state_last, event_last),
        else_=state_last,
    )
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
        ).persist_event(event, origin="webhook")
        work.commit()
    return inserted


def _request_watch_propagation() -> None:
    _save_setting("watch_sync.pending", "true")
    db.session.commit()
    with contextlib.suppress(Exception):
        get_executor(current_app).submit_pending_watch_sync()


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
    series_title: Any,
    media_kind: Any,
    event_type: str,
    occurred_at: datetime,
    completed: bool,
    active: bool,
    event_key: str | None,
) -> None:
    # Any terminal event closes prior playback rows for the same item.
    prior = db.session.scalars(
        select(WebhookEvent).where(
            WebhookEvent.source_id == source.id,
            WebhookEvent.external_id == external_id,
            WebhookEvent.active.is_(True),
        )
    ).all()
    for row in prior:
        row.active = False
    if not active and not completed:
        return
    db.session.add(
        WebhookEvent(
            source_id=source.id,
            external_id=external_id,
            event_key=event_key,
            title=title[:500],
            series_title=str(series_title)[:500] if series_title else None,
            media_kind=str(media_kind)[:32] if media_kind else None,
            event_type=event_type[:32],
            occurred_at=occurred_at,
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
