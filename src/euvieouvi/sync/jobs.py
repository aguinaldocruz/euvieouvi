"""Independent operational jobs exposed by the scheduler and web UI."""

from __future__ import annotations

import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flask import Flask
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.local import LocalProxy

from euvieouvi.api.runtime import connector_for, get_executor
from euvieouvi.connectors.base import MediaConnector
from euvieouvi.database.enums import ConnectorType, SyncStatus, SyncTrigger
from euvieouvi.database.models import (
    JobRun,
    Library,
    MediaImage,
    MediaItem,
    Setting,
    Source,
    SourceMediaRef,
    SyncError,
    SyncRun,
    SyncRunLibrary,
)
from euvieouvi.database.sqlite_runtime import is_sqlite_lock_error
from euvieouvi.enrichment.runtime import get_enrichment_executor
from euvieouvi.enrichment.service import recover_missing_artwork
from euvieouvi.extensions import db
from euvieouvi.logging import concise_error
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
        "watched_plex_to_jellyfin",
        "Assistidos: Plex → Jellyfin",
        "Propaga conclusões e progresso parcial do Plex para o Jellyfin.",
        "03:15",
    ),
    JobDefinition(
        "watched_jellyfin_to_plex",
        "Assistidos: Jellyfin → Plex",
        "Propaga conclusões e progresso parcial do Jellyfin para o Plex, "
        "respeitando conflitos do Plex.",
        "03:30",
    ),
    JobDefinition(
        "sync_jellyfin",
        "Sincronizar Jellyfin",
        "Atualiza catálogo e histórico do Jellyfin.",
        "03:45",
    ),
    JobDefinition(
        "metadata", "Atualizar metadados", "Enriquece campos ausentes do catálogo.", "04:00"
    ),
    JobDefinition(
        "catalog_images",
        "Baixar imagens do catálogo",
        "Recupera imagens ausentes por IDs exatos e baixa as imagens externas pendentes.",
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
    """Persist an execution and wake the database-backed worker."""
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
        job_id=job_id,
        trigger=trigger,
        status=SyncStatus.QUEUED,
        available_at=datetime.now(UTC),
        max_attempts=_max_attempts(),
        summary="Aguardando o worker persistente.",
    )
    db.session.add(run)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raced_run = db.session.scalar(
            select(JobRun.id).where(
                JobRun.job_id == job_id,
                JobRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]),
            )
        )
        if raced_run is None:
            raise
        return False
    job_run_id = run.id
    log_name = f"{job_id}-{job_run_id}.log"
    run.log_filename = log_name
    db.session.commit()
    _write_log(app, log_name, f"Job {job_id} criado com gatilho {trigger.value}.")
    from euvieouvi.sync.job_worker import wake_job_worker

    wake_job_worker(app)
    return True


def _max_attempts() -> int:
    setting = db.session.get(Setting, "jobs.retry.max_attempts")
    try:
        return max(1, min(10, int(setting.value if setting else "3")))
    except ValueError:
        return 3


def execute_claimed_job(app: Flask, job_run_id: int, worker_id: str) -> None:
    """Dispatch and monitor a row already leased by ``worker_id``."""
    with app.app_context():
        run = db.session.get(JobRun, job_run_id)
        if run is None or run.status is not SyncStatus.RUNNING or run.lease_owner != worker_id:
            return
        if run.cancel_requested_at is not None:
            run.status = SyncStatus.INTERRUPTED
            run.finished_at = datetime.now(UTC)
            run.progress_percent = 100
            run.summary = "Execução cancelada antes do início."
            _release_lease(run)
            db.session.commit()
            return
        reference = _dispatch_job(app, run.job_id, trigger=run.trigger)
        if reference is False:
            raise RuntimeError("O executor específico já está ocupado ou indisponível.")
        # Service-owned sessions may commit/close while dispatching. Reload the
        # durable row instead of retaining an ORM instance across that boundary.
        run = db.session.get(JobRun, job_run_id)
        if run is None or run.status is not SyncStatus.RUNNING or run.lease_owner != worker_id:
            raise RuntimeError("O lease do job foi perdido durante o despacho.")
        run.execution_reference = _serialize_reference(reference)
        run.summary = "Execução iniciada pelo worker persistente."
        job_id = run.job_id
        db.session.commit()
        _write_log(
            app,
            run.log_filename or "",
            f"Tentativa {run.attempt_count} iniciada · referência={run.execution_reference}.",
        )
    _monitor_job(app, job_run_id, job_id, reference, worker_id=worker_id)


def _serialize_reference(reference: int | str) -> str:
    return f"sync:{reference}" if isinstance(reference, int) else f"executor:{reference}"


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


def _monitor_job(
    app: Flask,
    job_run_id: int,
    job_id: str,
    reference: int | str,
    *,
    worker_id: str,
) -> None:
    """Persist coarse progress and a readable execution log."""
    last_line = ""
    cancellation_forwarded = False
    with app.app_context():
        if reference == "maintenance":
            _run_maintenance(app, job_run_id, worker_id=worker_id)
            return
        if reference == "reconcile":
            _run_reconcile(app, job_run_id, worker_id=worker_id)
            return
        while True:
            run = db.session.get(JobRun, job_run_id)
            if run is None or run.lease_owner != worker_id:
                return
            if run.cancel_requested_at is not None and not cancellation_forwarded:
                _cancel_reference(app, reference)
                cancellation_forwarded = True
                _write_log(app, run.log_filename or "", "Cancelamento encaminhado ao executor.")
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
                    status = SyncStatus.RUNNING if active else sync.status
                    summary = sync.summary or "Sincronização em andamento."
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
                    else (
                        SyncStatus.INTERRUPTED
                        if bool(snapshot.get("cancelled"))
                        else (SyncStatus.FAILED if failed else SyncStatus.SUCCEEDED)
                    )
                )
            if not active and run.cancel_requested_at is not None:
                status = SyncStatus.INTERRUPTED
                summary = "Execução cancelada; dados já confirmados foram preservados."
            run.status = status
            run.progress_percent = percent
            run.processed = processed
            run.updated = updated
            run.failed = failed
            run.summary = summary
            _renew_lease(run, worker_id)
            if not active:
                run.finished_at = datetime.now(UTC)
                run.progress_percent = 100
                _release_lease(run)
            try:
                db.session.commit()
            except OperationalError as lock_error:
                db.session.rollback()
                if not is_sqlite_lock_error(lock_error):
                    raise
                # A catalog page owns SQLite's single writer slot. Its
                # transaction remains authoritative; refresh progress after it
                # commits instead of retrying the entire external sync.
                time.sleep(1)
                continue
            line = (
                f"{percent}% · {summary} · processados={processed}, "
                f"atualizados={updated}, falhas={failed}"
            )
            if line != last_line:
                _write_log(app, run.log_filename or "", line)
                last_line = line
            if not active:
                if not isinstance(reference, int):
                    failure_details = str(snapshot.get("failure_details", ""))
                    for detail in failure_details.splitlines():
                        if detail.strip():
                            prefix = "INFO" if detail.startswith("AUTO-REPARO") else "ERRO"
                            _write_log(app, run.log_filename or "", f"{prefix} · {detail}")
                if isinstance(reference, int):
                    errors = db.session.scalars(
                        select(SyncError)
                        .where(SyncError.sync_run_id == reference)
                        .order_by(SyncError.id)
                    ).all()
                    for error in errors:
                        library = db.session.get(Library, error.library_id)
                        source = (
                            db.session.get(Source, library.source_id)
                            if library is not None
                            else None
                        )
                        ref = (
                            db.session.scalar(
                                select(SourceMediaRef).where(
                                    SourceMediaRef.source_id == source.id,
                                    SourceMediaRef.external_id == error.media_external_id,
                                )
                            )
                            if source is not None and error.media_external_id
                            else None
                        )
                        item = (
                            db.session.get(MediaItem, ref.media_item_id)
                            if ref is not None
                            else None
                        )
                        parts = [f"categoria={error.category}"]
                        if source is not None:
                            parts.append(f"fonte={source.name}")
                        if library is not None:
                            parts.append(f"biblioteca={library.name}")
                        if item is not None:
                            parts.extend(
                                (
                                    f"tipo={item.kind.value}",
                                    f"título={item.title}",
                                    f"item={item.id}",
                                )
                            )
                        if error.media_external_id:
                            parts.append(f"externo={error.media_external_id}")
                        parts.append(f"erro={error.message}")
                        prefix = "INFO" if error.category == "catalog_auto_reconciled" else "ERRO"
                        _write_log(
                            app,
                            run.log_filename or "",
                            prefix + " · " + " · ".join(parts),
                        )
                    _rotate_sync_runs()
                _rotate_job_runs(app, job_id)
                return
            time.sleep(1)


def _renew_lease(run: JobRun, worker_id: str) -> None:
    now = datetime.now(UTC)
    run.heartbeat_at = now
    run.lease_owner = worker_id
    run.lease_expires_at = now + timedelta(seconds=45)


def _release_lease(run: JobRun) -> None:
    run.lease_owner = None
    run.lease_expires_at = None


def _cancel_reference(app: Flask, reference: int | str) -> None:
    if isinstance(reference, int):
        get_executor(app).cancel(reference)
    elif reference == "metadata":
        get_enrichment_executor(app).cancel()
    elif reference == "images":
        get_image_executor(app).cancel()
    elif reference == "watch":
        get_executor(app).cancel_watch_sync()


def _run_maintenance(app: Flask, job_run_id: int, *, worker_id: str) -> None:
    run = db.session.get(JobRun, job_run_id)
    if run is None or run.lease_owner != worker_id:
        return
    if run.cancel_requested_at is not None:
        run.status = SyncStatus.INTERRUPTED
        run.finished_at = datetime.now(UTC)
        run.progress_percent = 100
        run.summary = "Otimização cancelada antes do início."
        _release_lease(run)
        db.session.commit()
        return
    try:
        _rotate_all_job_runs(app)
        pruned_sync_runs = _rotate_sync_runs()
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
        db.session.commit()
        _write_log(
            app,
            run.log_filename or "",
            "Compactando o SQLite com arquivos temporários persistentes em /data/sqlite-tmp.",
        )
        _vacuum_database(app)
        run.status = SyncStatus.SUCCEEDED
        run.progress_percent = 100
        run.processed = detached
        run.updated = removed
        run.summary = (
            f"Otimização concluída; {detached} caches ativos desvinculados, "
            f"{removed} arquivos removidos ({reclaimed_bytes / 1024 / 1024:.1f} MiB), "
            f"{pruned_sync_runs} sincronizações antigas removidas e SQLite compactado."
        )
    except Exception as error:
        db.session.rollback()
        run = db.session.get(JobRun, job_run_id)
        if run is None:
            return
        run.status = SyncStatus.FAILED
        run.failed = 1
        run.summary = f"Otimização falhou: {concise_error(error)}"
    run.finished_at = datetime.now(UTC)
    run.heartbeat_at = run.finished_at
    _release_lease(run)
    db.session.commit()
    _write_log(app, run.log_filename or "", run.summary or "Otimização encerrada.")


def _vacuum_database(app: Flask) -> None:
    """Run VACUUM with temporary storage on /data instead of the bounded /tmp tmpfs."""
    database_path = Path(db.engine.url.database or "")
    temporary_directory = Path(app.instance_path) / "sqlite-tmp"
    temporary_directory.mkdir(parents=True, exist_ok=True)
    temporary_directory.chmod(0o700)
    database_size = database_path.stat().st_size if database_path.is_file() else 0
    required_bytes = max(database_size * 2, 64 * 1024 * 1024)
    available_bytes = shutil.disk_usage(temporary_directory).free
    if available_bytes < required_bytes:
        raise OSError(
            "insufficient persistent space for SQLite VACUUM: "
            f"required={required_bytes} available={available_bytes}"
        )
    os.environ["SQLITE_TMPDIR"] = str(temporary_directory)
    escaped_directory = str(temporary_directory).replace("'", "''")
    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        # SQLite may cache its temporary directory before SQLITE_TMPDIR is set in a
        # long-running worker. The PRAGMA updates sqlite3_temp_directory directly.
        connection.exec_driver_sql(
            f"PRAGMA temp_store_directory='{escaped_directory}'"
        )
        connection.exec_driver_sql("PRAGMA temp_store=FILE")
        connection.exec_driver_sql("VACUUM")


def _run_reconcile(app: Flask, job_run_id: int, *, worker_id: str) -> None:
    from euvieouvi.database.backup import backup_database
    from euvieouvi.sync.catalog_reconcile import reconcile_catalog

    run = db.session.get(JobRun, job_run_id)
    if run is None or run.lease_owner != worker_id:
        return
    if run.cancel_requested_at is not None:
        run.status = SyncStatus.INTERRUPTED
        run.finished_at = datetime.now(UTC)
        run.progress_percent = 100
        run.summary = "Reconciliação cancelada antes do início."
        _release_lease(run)
        db.session.commit()
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
        log_filename = run.log_filename or ""
        result = reconcile_catalog(
            db.session(),
            dry_run=not apply_changes,
            report=lambda message: _write_log(app, log_filename, message),
        )
        run = db.session.get(JobRun, job_run_id)
        if run is None:
            return
        run.status = SyncStatus.SUCCEEDED
        run.progress_percent = 100
        run.processed = result.groups_found
        run.updated = result.items_merged + result.identifiers_recovered
        run.summary = (
            f"{'Aplicação' if apply_changes else 'Simulação'} concluída: "
            f"{result.groups_found} grupos, {result.items_merged} itens e "
            f"{result.hierarchy_merged} itens hierárquicos reconciliados; "
            f"{result.identifiers_recovered} identificadores recuperados de "
            f"{result.embedded_items} títulos e "
            f"{result.titles_normalized} títulos normalizados; "
            f"{result.identifier_conflicts} conflitos de identificador ignorados; "
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
        run.summary = f"Reconciliação falhou: {concise_error(error)}"
    run.finished_at = datetime.now(UTC)
    run.heartbeat_at = run.finished_at
    _release_lease(run)
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


def _rotate_sync_runs() -> int:
    value = db.session.get(Setting, "sync.retention.keep_last")
    try:
        keep = max(1, min(500, int(value.value if value else "15")))
    except ValueError:
        keep = 15
    removed = 0
    source_ids = db.session.scalars(select(SyncRun.source_id).distinct()).all()
    for source_id in source_ids:
        keep_ids = set(
            db.session.scalars(
                select(SyncRun.id)
                .where(
                    SyncRun.source_id == source_id,
                    SyncRun.status.notin_([SyncStatus.QUEUED, SyncStatus.RUNNING]),
                )
                .order_by(SyncRun.created_at.desc(), SyncRun.id.desc())
                .limit(keep)
            )
        )
        if not keep_ids:
            continue
        result = db.session.execute(
            delete(SyncRun).where(
                SyncRun.source_id == source_id,
                SyncRun.id.notin_(keep_ids),
                SyncRun.status.notin_([SyncStatus.QUEUED, SyncStatus.RUNNING]),
            )
        )
        removed += int(getattr(result, "rowcount", 0) or 0)
    db.session.commit()
    return removed


class LocalImageExecutor:
    """Bounded single-thread catalog image downloader."""

    def __init__(self, app: Flask) -> None:
        self._app = app._get_current_object() if isinstance(app, LocalProxy) else app
        self._lock = threading.Lock()
        self._active = False
        self._cancel = threading.Event()
        self._snapshot: dict[str, int | bool | str] = {
            "active": False,
            "processed": 0,
            "failed": 0,
            "updated": 0,
            "percent": 0,
            "summary": "ainda não executado",
            "failure_details": "",
            "cancelled": False,
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
            self._cancel.clear()
            self._snapshot = {
                "active": True,
                "processed": 0,
                "updated": 0,
                "failed": 0,
                "percent": 0,
                "summary": "Contando imagens pendentes.",
                "failure_details": "",
                "cancelled": False,
            }

        def execute() -> None:
            processed = updated = failed = download_processed = 0
            failure_details: list[str] = []
            with self._app.app_context():
                orphan_condition = (
                    ~select(SourceMediaRef.id)
                    .where(
                        SourceMediaRef.media_item_id == MediaImage.media_item_id,
                        SourceMediaRef.source_id == MediaImage.source_id,
                        SourceMediaRef.available.is_(True),
                    )
                    .exists()
                )
                cleanup = db.session.execute(
                    delete(MediaImage).where(
                        MediaImage.provider.in_(("plex", "jellyfin")),
                        orphan_condition,
                    )
                )
                retired = int(getattr(cleanup, "rowcount", 0) or 0)
                db.session.commit()

                def recovery_progress(values: dict[str, int]) -> None:
                    total_candidates = values.get("total", 0)
                    recovered = values.get("processed", 0)
                    with self._lock:
                        self._snapshot.update(
                            processed=recovered,
                            failed=values.get("failed", 0),
                            total=total_candidates,
                            percent=values.get("percent", 0) * 2 // 5,
                            summary=(
                                f"Buscando imagens externas: {recovered} de "
                                f"{total_candidates} itens verificados."
                            ),
                            failure_details="\n".join(failure_details),
                        )

                recovery = recover_missing_artwork(
                    self._app,
                    progress=recovery_progress,
                    failure=failure_details.append,
                    cancelled=self._cancel.is_set,
                )
                lookup_processed = recovery["processed"]
                not_found = recovery["not_found"]
                processed = lookup_processed
                failed = recovery["failed"]
                image_ids = list(
                    db.session.scalars(
                        select(MediaImage.id)
                        .where(
                            MediaImage.cache_status != "cached",
                            MediaImage.provider.in_(("tmdb", "coverartarchive")),
                        )
                        .order_by(MediaImage.id)
                    )
                )
                total = lookup_processed + len(image_ids)
                download_total = len(image_ids)
                cache = Path(self._app.instance_path) / "images"
                setting = db.session.get(Setting, "jobs.catalog_images.workers")
                try:
                    workers = min(12, max(1, int(setting.value if setting else "6")))
                except ValueError:
                    workers = 6
                with self._lock:
                    self._snapshot.update(
                        total=total,
                        summary=(
                            f"{retired} imagens órfãs removidas; "
                            f"{recovery['updated']} fallbacks encontrados; "
                            f"baixando {len(image_ids)} imagens externas com "
                            f"{workers} trabalhadores."
                        ),
                        percent=100 if download_total == 0 else 40,
                    )

                def process_chunk(ids: list[int]) -> None:
                    nonlocal download_processed, processed, updated, failed
                    connectors: dict[int, MediaConnector] = {}
                    with self._app.app_context():
                        try:
                            for image_id in ids:
                                if self._cancel.is_set():
                                    break
                                succeeded = False
                                detail_context = f"imagem={image_id}"
                                try:
                                    media_image = db.session.get(MediaImage, image_id)
                                    if media_image is not None:
                                        item = db.session.get(MediaItem, media_image.media_item_id)
                                        source = (
                                            db.session.get(Source, media_image.source_id)
                                            if media_image.source_id is not None
                                            else None
                                        )
                                        kind = (
                                            item.kind.value if item is not None else "desconhecido"
                                        )
                                        title = item.title if item is not None else "item ausente"
                                        source_name = (
                                            source.name if source is not None else "indisponível"
                                        )
                                        origin = (
                                            media_image.source_path
                                            or media_image.source_url
                                            or "ausente"
                                        )
                                        detail_context = (
                                            f"imagem={image_id} · tipo={kind} · título={title} · "
                                            f"item={media_image.media_item_id} · "
                                            f"imagem_tipo={media_image.image_type} · "
                                            f"provedor={media_image.provider} · "
                                            f"fonte={source_name} · "
                                            f"origem={origin}"
                                        )
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
                                            media_image,
                                            connector,
                                            cache,
                                            width=500,
                                            height=750,
                                        )
                                        db.session.commit()
                                        succeeded = True
                                except Exception as error:
                                    db.session.rollback()
                                    detail = (
                                        f"operação=baixar imagem · {detail_context} · "
                                        f"erro={type(error).__name__}: {error}"
                                    )
                                    self._app.logger.warning(
                                        "Catalog image item failed: %s", detail, exc_info=True
                                    )
                                    with self._lock:
                                        failure_details.append(detail)
                                        self._snapshot["failure_details"] = "\n".join(
                                            failure_details
                                        )
                                with self._lock:
                                    download_processed += 1
                                    processed += 1
                                    if succeeded:
                                        updated += 1
                                    else:
                                        failed += 1
                                    percent = 40 + download_processed * 60 // download_total
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
                    summary = ("Download cancelado; " if self._cancel.is_set() else "") + (
                        f"{lookup_processed} itens verificados para fallback; "
                        f"{recovery['updated']} imagens externas encontradas; "
                        f"{not_found} sem imagem externa; "
                        f"{download_processed} downloads processados; "
                        f"{updated} baixados; {failed} falhas; "
                        f"{retired} referências órfãs removidas."
                    )
                    with self._lock:
                        self._active = False
                        self._snapshot.update(
                            active=False,
                            cancelled=self._cancel.is_set(),
                            percent=100,
                            summary=summary,
                        )

        def guarded_execute() -> None:
            try:
                execute()
            except Exception as error:
                self._app.logger.exception("catalog image worker failed")
                formatted_error = concise_error(error)
                detail = f"operação=baixar imagens · erro={formatted_error}"
                with self._lock:
                    previous = str(self._snapshot.get("failure_details", "")).strip()
                    self._snapshot.update(
                        active=False,
                        failed=max(1, int(self._snapshot.get("failed", 0))),
                        percent=100,
                        summary=f"Download de imagens falhou: {formatted_error}",
                        failure_details="\n".join(value for value in (previous, detail) if value),
                    )
            finally:
                with self._lock:
                    self._active = False
                    self._snapshot["active"] = False

        threading.Thread(target=guarded_execute, name="euvieouvi-images", daemon=True).start()
        return True

    def cancel(self) -> bool:
        with self._lock:
            if not self._active:
                return False
            self._cancel.set()
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
    if run.execution_reference and run.execution_reference.startswith("sync:"):
        try:
            sync_run_id = int(run.execution_reference.removeprefix("sync:"))
        except ValueError:
            return False
        completed_sync = db.session.get(SyncRun, sync_run_id)
        if completed_sync is None or completed_sync.status in {
            SyncStatus.QUEUED,
            SyncStatus.RUNNING,
        }:
            return False
        return _copy_sync_result(run, completed_sync)
    # Rows created before the durable queue migration have no explicit reference.
    # Never apply this heuristic to a new or recovered queued attempt.
    if run.status is not SyncStatus.RUNNING or run.attempt_count != 0:
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
    return _copy_sync_result(run, completed_sync)


def _copy_sync_result(run: JobRun, completed_sync: SyncRun) -> bool:
    run.status = completed_sync.status
    run.finished_at = completed_sync.finished_at or datetime.now(UTC)
    run.progress_percent = 100
    run.processed = completed_sync.items_read
    run.updated = completed_sync.items_inserted + completed_sync.items_updated
    run.failed = completed_sync.items_failed
    run.summary = completed_sync.summary
    run.heartbeat_at = completed_sync.heartbeat_at or run.finished_at
    _release_lease(run)
    db.session.commit()
    return True


def reconcile_interrupted_job_runs() -> int:
    """Compatibility wrapper: recover active jobs instead of discarding them."""
    active = list(
        db.session.scalars(
            select(JobRun).where(JobRun.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]))
        )
    )
    for run in active:
        reconcile_completed_sync_job(run)
    from euvieouvi.sync.reconcile import reconcile_orphaned_runs

    reconcile_orphaned_runs()
    from euvieouvi.sync.job_worker import recover_job_runs

    return recover_job_runs(force=True)


def request_job_cancellation(app: Flask, run_id: int) -> bool:
    """Persist cancellation before forwarding it to an in-memory executor."""
    run = db.session.get(JobRun, run_id)
    if run is None or run.status not in {SyncStatus.QUEUED, SyncStatus.RUNNING}:
        return False
    now = datetime.now(UTC)
    run.cancel_requested_at = now
    if run.status is SyncStatus.QUEUED:
        run.status = SyncStatus.INTERRUPTED
        run.finished_at = now
        run.progress_percent = 100
        run.summary = "Execução cancelada enquanto aguardava o worker."
        _release_lease(run)
    else:
        run.summary = "Cancelamento solicitado; aguardando ponto seguro."
    db.session.commit()
    _write_log(app, run.log_filename or "", "Cancelamento persistente solicitado pelo usuário.")
    if run.execution_reference:
        kind, _, value = run.execution_reference.partition(":")
        reference: int | str = int(value) if kind == "sync" else value
        _cancel_reference(app, reference)
    return True
