"""Independent operational jobs exposed by the scheduler and web UI."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from flask import Flask
from sqlalchemy import func, select, text
from werkzeug.local import LocalProxy

from euvieouvi.api.runtime import connector_for, get_executor
from euvieouvi.database.enums import ConnectorType, SyncStatus, SyncTrigger
from euvieouvi.database.models import (
    JobRun,
    Library,
    MediaImage,
    Setting,
    Source,
    SourceMediaRef,
    SyncRun,
    SyncRunLibrary,
)
from euvieouvi.enrichment.runtime import get_enrichment_executor
from euvieouvi.extensions import db
from euvieouvi.media_images import ensure_cached, ensure_external_cached
from euvieouvi.sync.async_tasks import get_async_task_executor


@dataclass(frozen=True, slots=True)
class JobDefinition:
    id: str
    name: str
    description: str
    default_time: str


JOBS = (
    JobDefinition(
        "sync_plex", "Sincronizar Plex", "Atualiza catálogo e histórico do Plex.", "03:00"
    ),
    JobDefinition(
        "sync_jellyfin",
        "Sincronizar Jellyfin",
        "Atualiza catálogo e histórico do Jellyfin.",
        "03:15",
    ),
    JobDefinition(
        "watched_plex_to_jellyfin",
        "Assistidos: Plex → Jellyfin",
        "Propaga conclusões do Plex para o Jellyfin.",
        "03:30",
    ),
    JobDefinition(
        "watched_jellyfin_to_plex",
        "Assistidos: Jellyfin → Plex",
        "Propaga conclusões do Jellyfin para o Plex.",
        "03:45",
    ),
    JobDefinition(
        "metadata", "Atualizar metadados", "Enriquece campos ausentes do catálogo.", "04:00"
    ),
    JobDefinition(
        "catalog_images",
        "Baixar imagens do catálogo",
        "Baixa e armazena localmente as imagens pendentes.",
        "04:30",
    ),
    JobDefinition(
        "catalog_reconcile",
        "Reconciliar catálogo",
        "Une duplicatas confirmadas por identificadores externos estáveis.",
        "04:45",
    ),
    JobDefinition(
        "async_tasks",
        "Processar fila de atualizações",
        "Repete atualizações instantâneas pendentes entre serviços.",
        "00:30",
    ),
    JobDefinition(
        "maintenance",
        "Otimizar dados",
        "Remove logs excedentes e imagens órfãs e otimiza o SQLite.",
        "05:00",
    ),
)
JOB_BY_ID = {job.id: job for job in JOBS}


def setting_key(job_id: str, field: str) -> str:
    return f"jobs.{job_id}.{field}"


def submit_job(app: Flask, job_id: str, *, trigger: SyncTrigger = SyncTrigger.MANUAL) -> bool:
    """Create a persistent execution, dispatch it, and monitor progress."""
    app = app._get_current_object() if isinstance(app, LocalProxy) else app
    if job_id not in JOB_BY_ID:
        raise KeyError(job_id)
    if db.session.scalar(
        select(JobRun.id).where(
            JobRun.job_id == job_id,
            JobRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]),
        )
    ):
        return False
    run = JobRun(
        job_id=job_id, trigger=trigger, status=SyncStatus.QUEUED, summary="Aguardando início."
    )
    db.session.add(run)
    db.session.commit()
    job_run_id = run.id
    log_name = f"{job_id}-{job_run_id}.log"
    run.log_filename = log_name
    db.session.commit()
    _write_log(app, log_name, f"Job {job_id} criado com gatilho {trigger.value}.")
    try:
        reference = _dispatch_job(app, job_id, trigger=trigger)
    except Exception as error:
        failed_run = db.session.get(JobRun, job_run_id)
        if failed_run is not None:
            failed_run.status = SyncStatus.FAILED
            failed_run.finished_at = datetime.now(UTC)
            failed_run.failed = 1
            failed_run.summary = "Não foi possível iniciar o job."
        db.session.commit()
        _write_log(app, log_name, f"Falha ao iniciar: {type(error).__name__}: {error}")
        raise
    if reference is False:
        persisted = db.session.get(JobRun, job_run_id)
        if persisted is not None:
            db.session.delete(persisted)
        db.session.commit()
        _log_path(app, log_name).unlink(missing_ok=True)
        return False
    run = db.session.get(JobRun, job_run_id)
    if run is None:
        raise RuntimeError("A execução do job desapareceu após iniciar a tarefa.")
    run.status = SyncStatus.RUNNING
    run.started_at = datetime.now(UTC)
    run.summary = "Execução iniciada."
    db.session.commit()
    threading.Thread(
        target=_monitor_job,
        args=(app, job_run_id, job_id, reference),
        name=f"euvieouvi-job-{job_id}",
        daemon=True,
    ).start()
    return True


def _dispatch_job(app: Flask, job_id: str, *, trigger: SyncTrigger) -> int | str | bool:
    if job_id in {"sync_plex", "sync_jellyfin"}:
        connector_type = ConnectorType.PLEX if job_id == "sync_plex" else ConnectorType.JELLYFIN
        source_id = db.session.scalar(
            select(Source.id)
            .where(
                Source.connector_type == connector_type,
                Source.enabled.is_(True),
                select(Library.id)
                .where(
                    Library.source_id == Source.id,
                    Library.enabled.is_(True),
                    Library.available.is_(True),
                )
                .exists(),
            )
            .order_by(Source.id)
        )
        if source_id is None:
            return False
        return get_executor(app).submit(source_id, trigger=trigger)
    if job_id == "metadata":
        return "metadata" if get_enrichment_executor(app).submit() else False
    if job_id == "catalog_images":
        return "images" if get_image_executor(app).submit() else False
    if job_id == "async_tasks":
        return "async_tasks" if get_async_task_executor(app).retry_all() else False
    if job_id == "maintenance":
        return "maintenance"
    if job_id == "catalog_reconcile":
        return "reconcile"
    source_type = (
        ConnectorType.PLEX if job_id == "watched_plex_to_jellyfin" else ConnectorType.JELLYFIN
    )
    return "watch" if get_executor(app).submit_directional_watch_sync(source_type) else False


def _monitor_job(app: Flask, job_run_id: int, job_id: str, reference: int | str) -> None:
    """Persist coarse progress and a readable execution log."""
    last_line = ""
    with app.app_context():
        if reference == "maintenance":
            _run_maintenance(app, job_run_id)
            return
        if reference == "reconcile":
            _run_reconcile(app, job_run_id)
            return
        while True:
            run = db.session.get(JobRun, job_run_id)
            if run is None:
                return
            active = False
            status = SyncStatus.RUNNING
            if isinstance(reference, int):
                sync = db.session.get(SyncRun, reference)
                if sync is None:
                    status, summary = SyncStatus.FAILED, "Sincronização associada desapareceu."
                    processed = updated = 0
                    failed = 1
                    percent = 100
                else:
                    active = sync.status in {SyncStatus.QUEUED, SyncStatus.RUNNING}
                    status, summary = sync.status, sync.summary or "Sincronização em andamento."
                    processed = sync.items_read
                    updated = sync.items_inserted + sync.items_updated
                    failed = sync.items_failed
                    details = db.session.scalars(
                        select(SyncRunLibrary).where(SyncRunLibrary.sync_run_id == sync.id)
                    ).all()
                    library_ids = [detail.library_id for detail in details]
                    existing_counts = {
                        library_id: count
                        for library_id, count in db.session.execute(
                            select(SourceMediaRef.library_id, func.count(SourceMediaRef.id))
                            .where(
                                SourceMediaRef.library_id.in_(library_ids),
                                SourceMediaRef.available.is_(True),
                            )
                            .group_by(SourceMediaRef.library_id)
                        )
                    }
                    scanned = sum(
                        min(
                            detail.items_scanned,
                            detail.items_total or existing_counts.get(detail.library_id, 0),
                        )
                        for detail in details
                    )
                    total = sum(
                        detail.items_total or existing_counts.get(detail.library_id, 0)
                        for detail in details
                    )
                    percent = (
                        100
                        if not active
                        else max(1, min(99, scanned * 100 // total))
                        if total
                        else 1
                    )
            else:
                snapshot = (
                    get_enrichment_executor(app).snapshot
                    if reference == "metadata"
                    else get_image_executor(app).snapshot
                    if reference == "images"
                    else get_async_task_executor(app).snapshot
                    if reference == "async_tasks"
                    else get_executor(app).watch_sync_snapshot
                )
                active = bool(snapshot.get("active"))
                processed = int(snapshot.get("processed", 0))
                updated = int(snapshot.get("updated", 0))
                failed = int(snapshot.get("failed", 0))
                percent = int(snapshot.get("percent", 0 if active else 100))
                summary = str(snapshot.get("summary", "Execução em andamento."))
                status = (
                    SyncStatus.RUNNING
                    if active
                    else (SyncStatus.FAILED if failed else SyncStatus.SUCCEEDED)
                )
            run.status = status
            run.progress_percent = percent
            run.processed = processed
            run.updated = updated
            run.failed = failed
            run.summary = summary
            if not active:
                run.finished_at = datetime.now(UTC)
            db.session.commit()
            line = (
                f"{percent}% · {summary} · processados={processed}, "
                f"atualizados={updated}, falhas={failed}"
            )
            if line != last_line:
                _write_log(app, run.log_filename or "", line)
                last_line = line
            if not active:
                _rotate_job_runs(app, job_id)
                return
            time.sleep(1)


def _run_maintenance(app: Flask, job_run_id: int) -> None:
    run = db.session.get(JobRun, job_run_id)
    if run is None:
        return
    try:
        _rotate_all_job_runs(app)
        active_cached = db.session.scalars(
            select(MediaImage).where(
                MediaImage.provider.in_(["plex", "jellyfin"]),
                MediaImage.local_filename.is_not(None),
                select(SourceMediaRef.id)
                .where(
                    SourceMediaRef.media_item_id == MediaImage.media_item_id,
                    SourceMediaRef.available.is_(True),
                )
                .exists(),
            )
        ).all()
        detached = 0
        for image in active_cached:
            image.local_filename = None
            image.mime_type = None
            image.cache_status = "pending"
            image.cached_at = None
            detached += 1
        db.session.flush()
        referenced = {
            name for name in db.session.scalars(select(MediaImage.local_filename)) if name
        }
        removed = 0
        reclaimed_bytes = 0
        image_dir = Path(app.instance_path) / "images"
        if image_dir.is_dir():
            for path in image_dir.iterdir():
                if path.is_file() and path.name not in referenced:
                    reclaimed_bytes += path.stat().st_size
                    path.unlink()
                    removed += 1
        db.session.execute(text("PRAGMA optimize"))
        run.status = SyncStatus.SUCCEEDED
        run.progress_percent = 100
        run.processed = detached
        run.updated = removed
        run.summary = (
            f"Otimização concluída; {detached} caches ativos desvinculados e "
            f"{removed} arquivos removidos ({reclaimed_bytes / 1024 / 1024:.1f} MiB)."
        )
    except Exception as error:
        db.session.rollback()
        run = db.session.get(JobRun, job_run_id)
        if run is None:
            return
        run.status = SyncStatus.FAILED
        run.failed = 1
        run.summary = f"Otimização falhou: {type(error).__name__}."
    run.finished_at = datetime.now(UTC)
    db.session.commit()
    _write_log(app, run.log_filename or "", run.summary or "Otimização encerrada.")


def _run_reconcile(app: Flask, job_run_id: int) -> None:
    from euvieouvi.database.backup import backup_database
    from euvieouvi.sync.catalog_reconcile import reconcile_catalog

    run = db.session.get(JobRun, job_run_id)
    if run is None:
        return
    apply_setting = db.session.get(Setting, "jobs.catalog_reconcile.apply")
    apply_changes = apply_setting is not None and apply_setting.value == "true"
    try:
        if apply_changes:
            database = Path(app.instance_path) / "euvieouvi.db"
            backup = (
                Path(app.instance_path)
                / "backups"
                / f"pre-reconcile-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.db"
            )
            backup_database(database, backup)
            _write_log(app, run.log_filename or "", f"Backup criado: {backup.name}")
        result = reconcile_catalog(db.session(), dry_run=not apply_changes)
        run = db.session.get(JobRun, job_run_id)
        if run is None:
            return
        run.status = SyncStatus.SUCCEEDED
        run.progress_percent = 100
        run.processed = result.groups_found
        run.updated = result.items_merged
        run.summary = (
            f"{'Aplicação' if apply_changes else 'Simulação'} concluída: "
            f"{result.groups_found} grupos, {result.items_merged} itens e "
            f"{result.hierarchy_merged} itens hierárquicos reconciliados; "
            f"{result.skipped} grupos ambíguos ignorados; "
            f"{result.identifierless} itens sem identificadores exigem revisão."
        )
    except Exception as error:
        db.session.rollback()
        run = db.session.get(JobRun, job_run_id)
        if run is None:
            return
        run.status = SyncStatus.FAILED
        run.failed = 1
        run.summary = f"Reconciliação falhou: {type(error).__name__}: {error}"
    run.finished_at = datetime.now(UTC)
    db.session.commit()
    _write_log(app, run.log_filename or "", run.summary or "Reconciliação encerrada.")


def _log_path(app: Flask, filename: str) -> Path:
    directory = Path(app.instance_path) / "job-logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename


def _write_log(app: Flask, filename: str, message: str) -> None:
    if not filename:
        return
    with _log_path(app, filename).open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now(UTC).isoformat()} {message}\n")


def _retention() -> int:
    value = db.session.get(Setting, "jobs.retention.keep_last")
    try:
        return max(1, min(500, int(value.value if value else "20")))
    except ValueError:
        return 20


def _rotate_job_runs(app: Flask, job_id: str) -> None:
    keep = _retention()
    old = db.session.scalars(
        select(JobRun)
        .where(JobRun.job_id == job_id)
        .order_by(JobRun.created_at.desc(), JobRun.id.desc())
        .offset(keep)
    ).all()
    for run in old:
        if run.log_filename:
            _log_path(app, run.log_filename).unlink(missing_ok=True)
        db.session.delete(run)
    db.session.commit()


def _rotate_all_job_runs(app: Flask) -> None:
    for job in JOBS:
        _rotate_job_runs(app, job.id)


class LocalImageExecutor:
    """Bounded single-thread catalog image downloader."""

    def __init__(self, app: Flask) -> None:
        self._app = app._get_current_object() if isinstance(app, LocalProxy) else app
        self._lock = threading.Lock()
        self._active = False
        self._snapshot: dict[str, int | bool | str] = {
            "active": False,
            "processed": 0,
            "failed": 0,
            "updated": 0,
            "percent": 0,
            "summary": "ainda não executado",
        }

    @property
    def snapshot(self) -> dict[str, int | bool | str]:
        with self._lock:
            return dict(self._snapshot)

    def submit(self) -> bool:
        with self._lock:
            if self._active:
                return False
            self._active = True
            self._snapshot = {
                "active": True,
                "processed": 0,
                "updated": 0,
                "failed": 0,
                "percent": 0,
                "summary": "Contando imagens pendentes.",
            }

        def execute() -> None:
            processed = updated = failed = 0
            with self._app.app_context():
                image_ids = list(
                    db.session.scalars(
                    select(MediaImage.id)
                    .where(
                        MediaImage.cache_status != "cached",
                        ~select(SourceMediaRef.id)
                        .where(
                            SourceMediaRef.media_item_id == MediaImage.media_item_id,
                            SourceMediaRef.available.is_(True),
                        )
                        .exists(),
                    )
                    .order_by(MediaImage.id)
                    )
                )
                total = len(image_ids)
                cache = Path(self._app.instance_path) / "images"
                setting = db.session.get(Setting, "jobs.catalog_images.workers")
                try:
                    workers = min(12, max(1, int(setting.value if setting else "6")))
                except ValueError:
                    workers = 6
                with self._lock:
                    self._snapshot.update(
                        total=total,
                        summary=f"Baixando {total} imagens com {workers} trabalhadores.",
                        percent=100 if total == 0 else 0,
                    )

                def process_chunk(ids: list[int]) -> None:
                    nonlocal processed, updated, failed
                    connectors: dict[int, object] = {}
                    with self._app.app_context():
                        try:
                            for image_id in ids:
                                succeeded = False
                                try:
                                    media_image = db.session.get(MediaImage, image_id)
                                    if media_image is None or media_image.cache_status == "cached":
                                        succeeded = True
                                    elif media_image.provider in {"tmdb", "coverartarchive"}:
                                        ensure_external_cached(media_image, cache)
                                        db.session.commit()
                                        succeeded = True
                                    else:
                                        source_id = media_image.source_id
                                        if source_id is None:
                                            raise LookupError("Image source is unavailable")
                                        connector = connectors.get(source_id)
                                        if connector is None:
                                            source = db.session.get(Source, source_id)
                                            if source is None:
                                                raise LookupError("Image source is unavailable")
                                            connector = connector_for(source)
                                            connectors[source_id] = connector
                                        ensure_cached(
                                            media_image, connector, cache, width=500, height=750  # type: ignore[arg-type]
                                        )
                                        db.session.commit()
                                        succeeded = True
                                except Exception:
                                    db.session.rollback()
                                with self._lock:
                                    processed += 1
                                    if succeeded:
                                        updated += 1
                                    else:
                                        failed += 1
                                    percent = 100 if total == 0 else processed * 100 // total
                                    self._snapshot.update(
                                        processed=processed,
                                        updated=updated,
                                        failed=failed,
                                        percent=percent,
                                        summary=f"{processed} de {total} imagens processadas.",
                                    )
                        finally:
                            for connector in connectors.values():
                                close = getattr(connector, "close", None)
                                if callable(close):
                                    close()

                try:
                    chunks = [image_ids[index::workers] for index in range(workers)]
                    with ThreadPoolExecutor(
                        max_workers=workers, thread_name_prefix="image"
                    ) as pool:
                        list(pool.map(process_chunk, chunks))
                finally:
                    summary = (
                        f"{processed} imagens processadas; {updated} baixadas; {failed} falhas."
                    )
                    with self._lock:
                        self._active = False
                        self._snapshot.update(active=False, percent=100, summary=summary)

        threading.Thread(target=execute, name="euvieouvi-images", daemon=True).start()
        return True



def get_image_executor(app: Flask) -> LocalImageExecutor:
    app = app._get_current_object() if isinstance(app, LocalProxy) else app
    executor = app.extensions.get("euvieouvi.image_executor")
    if not isinstance(executor, LocalImageExecutor):
        executor = LocalImageExecutor(app)
        app.extensions["euvieouvi.image_executor"] = executor
    return executor


def reconcile_completed_sync_job(run: JobRun) -> bool:
    """Close a sync job whose underlying sync already reached a terminal state."""
    if run.status not in {SyncStatus.QUEUED, SyncStatus.RUNNING}:
        return False
    connector_type = (
        ConnectorType.PLEX
        if run.job_id == "sync_plex"
        else ConnectorType.JELLYFIN
        if run.job_id == "sync_jellyfin"
        else None
    )
    if connector_type is None:
        return False
    completed_sync = db.session.scalar(
        select(SyncRun)
        .join(Source, Source.id == SyncRun.source_id)
        .where(
            Source.connector_type == connector_type,
            SyncRun.created_at >= run.created_at,
            SyncRun.status.not_in([SyncStatus.QUEUED, SyncStatus.RUNNING]),
        )
        .order_by(SyncRun.created_at.desc(), SyncRun.id.desc())
    )
    if completed_sync is None:
        return False
    run.status = completed_sync.status
    run.finished_at = completed_sync.finished_at or datetime.now(UTC)
    run.progress_percent = 100
    run.processed = completed_sync.items_read
    run.updated = completed_sync.items_inserted + completed_sync.items_updated
    run.failed = completed_sync.items_failed
    run.summary = completed_sync.summary
    db.session.commit()
    return True


def reconcile_interrupted_job_runs() -> int:
    """Close persisted active jobs left behind by a worker crash or restart."""
    runs = db.session.scalars(
        select(JobRun).where(JobRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
    ).all()
    now = datetime.now(UTC)
    for run in runs:
        if reconcile_completed_sync_job(run):
            continue
        run.status = SyncStatus.INTERRUPTED
        run.finished_at = now
        run.summary = "Execução interrompida por reinicialização ou falha do worker."
    if runs:
        db.session.commit()
    return len(runs)
