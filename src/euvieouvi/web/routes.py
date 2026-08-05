"""Jinja pages and HTMX fragments for the local interface."""

from __future__ import annotations

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
from euvieouvi.connectors.errors import ConnectorError
from euvieouvi.connectors.plex.connector import PlexConnector
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
from euvieouvi.errors import AppError
from euvieouvi.extensions import db
from euvieouvi.media_images import ensure_cached
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
    except (ConnectorError, OSError):
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
    statement = select(MediaItem, WatchState, available_ref.label("available")).outerjoin(
        WatchState, WatchState.media_item_id == MediaItem.id
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
        statement = statement.where(WatchState.view_count > 0)
    elif played == "progress":
        statement = statement.where(
            WatchState.completed.is_(False), WatchState.progress_ms.is_not(None)
        )
    elif played == "unplayed":
        statement = statement.where((WatchState.id.is_(None)) | (WatchState.view_count == 0))
    sort_columns = {
        "title": func.coalesce(MediaItem.sort_title, MediaItem.title),
        "original_title": func.coalesce(MediaItem.original_title, MediaItem.title),
        "year": MediaItem.year,
        "last_played": WatchState.last_watched_at,
        "first_played": select(func.min(WatchEvent.watched_at))
        .where(WatchEvent.media_item_id == MediaItem.id)
        .scalar_subquery(),
        "play_count": WatchState.view_count,
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
        sort_column.desc().nullslast()
        if direction == "desc"
        else sort_column.asc().nullslast()
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
    source = db.session.get(Source, image.source_id)
    if source is None or not source.enabled:
        return _placeholder_image(item.kind)
    connector = connector_for(source)
    if not isinstance(connector, PlexConnector):
        return _placeholder_image(item.kind)
    square = item.kind in {MediaKind.ARTIST, MediaKind.ALBUM, MediaKind.TRACK}
    try:
        path = ensure_cached(
            image,
            connector,
            Path(current_app.instance_path) / "images",
            width=400 if square else 300,
            height=400 if square else 450,
        )
        db.session.commit()
    except (ConnectorError, OSError):
        db.session.rollback()
        return _placeholder_image(item.kind)
    finally:
        connector.close()
    response = send_file(path, mimetype=image.mime_type, conditional=True, max_age=86400)
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


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
    child_states = {
        child_state.media_item_id: child_state
        for child_state in db.session.scalars(
            select(WatchState).where(
                WatchState.media_item_id.in_([child.id for child in children_for_states])
            )
        ).all()
    }
    aggregate = None
    if item.kind in {MediaKind.SHOW, MediaKind.ARTIST} and children_for_states:
        playable = [
            child
            for child in children_for_states
            if child.kind in {MediaKind.EPISODE, MediaKind.TRACK}
        ]
        played_states = [child_states[child.id] for child in playable if child.id in child_states]
        last_played = max(
            (value.last_watched_at for value in played_states if value.last_watched_at is not None),
            default=None,
        )
        aggregate = {
            "total": len(playable),
            "played": sum(1 for value in played_states if value.view_count > 0),
            "play_count": sum(value.view_count for value in played_states),
            "last_played": last_played,
        }
    item_genres = db.session.scalars(
        select(Genre)
        .join(MediaGenre, MediaGenre.genre_id == Genre.id)
        .where(MediaGenre.media_item_id == media_id)
        .order_by(Genre.name)
    ).all()
    refs = db.session.scalars(
        select(SourceMediaRef).where(SourceMediaRef.media_item_id == media_id)
    ).all()
    return render_template(
        "media_detail.html",
        item=item,
        state=state,
        events=events,
        children=children,
        grouped_children=grouped_children,
        aggregate=aggregate,
        item_genres=item_genres,
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


def _placeholder_image(kind: MediaKind) -> Response:
    square = kind in {MediaKind.ARTIST, MediaKind.ALBUM, MediaKind.TRACK}
    width, height = ((400, 400) if square else (300, 450))
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
