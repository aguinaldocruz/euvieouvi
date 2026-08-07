"""Jinja pages and HTMX fragments for the local interface."""

from __future__ import annotations

import contextlib
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
from sqlalchemy import case, exists, func, or_, select
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
    WatchState,
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
        "sync.schedule.mode",
        "sync.schedule.time.plex",
        "sync.schedule.time.jellyfin",
        "sync.schedule.enabled.plex",
        "sync.schedule.enabled.jellyfin",
        "sync.schedule.last_date",
        "sync.schedule.last_date.plex",
        "sync.schedule.last_date.jellyfin",
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
    if not isinstance(payload, dict) or payload.get("event") != "media.scrobble":
        return Response(status=204)
    metadata = payload.get("Metadata")
    if not isinstance(metadata, dict):
        return Response("Metadata ausente.", 400)
    external_id = str(metadata.get("ratingKey") or "").strip()
    library_external_id = str(metadata.get("librarySectionID") or "").strip()
    if not external_id or not library_external_id:
        return Response("Identidade da mídia ausente.", 400)
    # process even if source.enabled is False
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
    libraries = db.session.scalars(
        select(SyncRunLibrary)
        .where(SyncRunLibrary.sync_run_id == run_id)
        .order_by(SyncRunLibrary.id)
    ).all()
    errors = db.session.scalars(
        select(SyncError).where(SyncError.sync_run_id == run_id).order_by(SyncError.id).limit(100)
    ).all()
    return {"run": run, "libraries": libraries, "errors": errors}


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
    completion_count, last_completed, completed_known = _completion_expressions(MediaItem.id)
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
        statement = statement.where(completed_known)
    elif played == "unplayed":
        statement = statement.where(~completed_known)
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
        .limit(80)
        .offset((page - 1) * 40)
    ).all()
    # Deduplicate by title/year/kind merging Plex/Jellyfin badges.
    # When same media exists in both sources as separate rows (e.g., Plex
    # without identifiers), show single card with both badges.
    # Keep DB order and merge badges via OR.
    deduped: dict[tuple[str, int | None, str], Any] = {}
    ordered_keys: list[tuple[str, int | None, str]] = []
    for row in rows:
        item = row[0]
        key = (item.title.strip().casefold(), item.year, item.kind.value)
        if key not in deduped:
            deduped[key] = row
            ordered_keys.append(key)
        else:
            prev = deduped[key]
            merged_plex = bool(prev[3] or row[3])
            merged_jelly = bool(prev[4] or row[4])
            keep = prev if prev[0].id < item.id else row
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
