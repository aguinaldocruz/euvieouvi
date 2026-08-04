"""Jinja pages and HTMX fragments for the local interface."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from euvieouvi.api.runtime import connector_for, get_executor
from euvieouvi.api.validation import http_url
from euvieouvi.connectors.errors import ConnectorError
from euvieouvi.database.enums import ConnectorType, MediaKind, SyncStatus
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
from euvieouvi.errors import AppError
from euvieouvi.extensions import db
from euvieouvi.sync.discovery import LibraryDiscoveryService
from euvieouvi.sync.errors import SyncAlreadyRunningError, SyncSourceUnavailableError
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


def _source() -> Source | None:
    return db.session.scalar(select(Source).order_by(Source.id))


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
        .order_by(WatchEvent.watched_at.desc(), WatchEvent.id.desc())
        .limit(8)
    ).all()
    counts = {
        "movies": _count(MediaItem, MediaItem.kind == MediaKind.MOVIE),
        "shows": _count(MediaItem, MediaItem.kind == MediaKind.SHOW),
        "episodes": _count(MediaItem, MediaItem.kind == MediaKind.EPISODE),
        "watched_movies": _watched_count(MediaKind.MOVIE),
        "watched_episodes": _watched_count(MediaKind.EPISODE),
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
    source = _source()
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


@blueprint.post("/settings/plex/test")
def settings_plex_test() -> Any:
    source = _source()
    if source is None:
        flash("Salve a configuração antes de testar.", "warning")
        return redirect(url_for("web.settings_plex"))
    try:
        info = connector_for(source).test_connection()
        source.last_connection_status = "succeeded"
        source.last_connection_test_at = datetime.now(UTC)
        db.session.commit()
        flash(f"Conexão com o Plex realizada com sucesso ({info.server_name}).", "success")
    except ConnectorError:
        source.last_connection_status = "failed"
        source.last_connection_test_at = datetime.now(UTC)
        db.session.commit()
        flash("O Plex não respondeu ao teste. Verifique URL, token e disponibilidade.", "danger")
    return redirect(url_for("web.settings_plex"))


@blueprint.get("/libraries")
def libraries() -> Any:
    source = _source()
    values = db.session.scalars(select(Library).order_by(Library.name, Library.id)).all()
    return render_template("libraries.html", source=source, libraries=values)


@blueprint.post("/libraries/discover")
def libraries_discover() -> Any:
    source = _source()
    if source is None:
        flash("Configure o Plex antes de descobrir bibliotecas.", "warning")
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
        source = _source()
        if source is None:
            flash("Configure o Plex antes de sincronizar.", "warning")
            return redirect(url_for("web.settings_plex"))
        enabled_count = _count(
            Library,
            Library.source_id == source.id,
            Library.enabled.is_(True),
            Library.available.is_(True),
        )
        if enabled_count == 0:
            flash("Selecione ao menos uma biblioteca disponível antes de sincronizar.", "warning")
            return redirect(url_for("web.libraries"))
        try:
            run_id = get_executor(current_app).submit(source.id)
        except (SyncAlreadyRunningError, SyncSourceUnavailableError):
            flash("Uma sincronização já está ativa ou a fonte está indisponível.", "warning")
            return redirect(url_for("web.sync_list"))
        flash("Sincronização iniciada em segundo plano.", "success")
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
    statement = select(MediaItem).outerjoin(WatchState, WatchState.media_item_id == MediaItem.id)
    if query:
        statement = statement.where(MediaItem.title.ilike(f"%{query}%"))
    if kind in {item.value for item in MediaKind}:
        statement = statement.where(MediaItem.kind == MediaKind(kind))
    if watched == "watched":
        statement = statement.where(WatchState.completed.is_(True))
    elif watched == "progress":
        statement = statement.where(
            WatchState.completed.is_(False), WatchState.progress_ms.is_not(None)
        )
    elif watched == "unwatched":
        statement = statement.where(WatchState.id.is_(None))
    raw_page = request.args.get("page", "1")
    page = max(int(raw_page) if raw_page.isdigit() else 1, 1)
    values = db.session.scalars(
        statement.distinct()
        .order_by(WatchState.last_watched_at.desc().nullslast(), MediaItem.title, MediaItem.id)
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


@blueprint.get("/media/<int:media_id>")
def media_detail(media_id: int) -> Any:
    item = db.session.get(MediaItem, media_id)
    if item is None:
        return render_template("errors/404.html"), 404
    state = db.session.scalar(
        select(WatchState)
        .where(WatchState.media_item_id == media_id)
        .order_by(WatchState.id.desc())
    )
    events = db.session.scalars(
        select(WatchEvent)
        .where(WatchEvent.media_item_id == media_id)
        .order_by(WatchEvent.watched_at.desc(), WatchEvent.id.desc())
    ).all()
    children = db.session.scalars(
        select(MediaItem)
        .where(MediaItem.parent_id == media_id)
        .order_by(MediaItem.season_number, MediaItem.episode_number, MediaItem.id)
    ).all()
    child_states = {
        child_state.media_item_id: child_state
        for child_state in db.session.scalars(
            select(WatchState).where(WatchState.media_item_id.in_([child.id for child in children]))
        ).all()
    }
    refs = db.session.scalars(
        select(SourceMediaRef).where(SourceMediaRef.media_item_id == media_id)
    ).all()
    return render_template(
        "media_detail.html",
        item=item,
        state=state,
        events=events,
        children=children,
        child_states=child_states,
        refs=refs,
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
            .select_from(WatchState)
            .join(MediaItem, MediaItem.id == WatchState.media_item_id)
            .where(WatchState.completed.is_(True), MediaItem.kind == kind)
        )
        or 0
    )
