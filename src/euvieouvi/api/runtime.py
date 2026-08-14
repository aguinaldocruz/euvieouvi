"""Connector construction and bounded in-process sync execution."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from importlib.metadata import version

from flask import Flask
from sqlalchemy import select
from werkzeug.local import LocalProxy

from euvieouvi.connectors.base import MediaConnector
from euvieouvi.connectors.jellyfin.client import JellyfinHttpClient
from euvieouvi.connectors.jellyfin.connector import JellyfinConnector
from euvieouvi.connectors.plex.client import PlexHttpClient
from euvieouvi.connectors.plex.connector import PlexConnector
from euvieouvi.database.enums import ConnectorType, SyncStatus, SyncTrigger
from euvieouvi.database.models import Setting, Source, SyncRun
from euvieouvi.extensions import db
from euvieouvi.sync.cancellation import CancellationToken
from euvieouvi.sync.errors import SyncAlreadyRunningError, SyncSourceUnavailableError
from euvieouvi.sync.orchestrator import SyncOrchestrator
from euvieouvi.sync.watch_sync import WatchSyncService

ConnectorFactory = Callable[[Source], MediaConnector]


def connector_for(source: Source) -> MediaConnector:
    if source.connector_type is ConnectorType.JELLYFIN:
        try:
            secret = json.loads(source.secret)
            api_key = str(secret["api_key"])
            user_id = str(secret["user_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid persisted Jellyfin credentials") from error
        jf_client = JellyfinHttpClient(source.base_url, api_key)
        try:
            resolved = jf_client.resolve_user_id(user_id)
        except Exception:
            resolved = user_id
        return JellyfinConnector(jf_client, resolved)
    client = PlexHttpClient(
        source.base_url,
        source.secret,
        application_version=version("euvieouvi"),
        client_identifier=f"euvieouvi-{uuid.getnode():x}",
    )
    configured_user = db.session.get(Setting, "plex.user_id")
    if configured_user is None:
        configured_user = db.session.get(Setting, "webhook.plex.user_filter")
    return PlexConnector(client, configured_user.value if configured_user is not None else None)


class LocalSyncExecutor:
    """Run one synchronization in a daemon thread and expose cooperative cancellation."""

    def __init__(self, app: Flask, factory: ConnectorFactory | None = None) -> None:
        self._app = app
        self._factory = factory or connector_for
        self._tokens: dict[int, CancellationToken] = {}
        self._lock = threading.Lock()
        self._watch_sync_running = False
        self._watch_snapshot: dict[str, int | bool | str] = {
            "active": False,
            "processed": 0,
            "updated": 0,
            "failed": 0,
            "summary": "ainda não executado",
        }

    def submit(
        self,
        source_id: int,
        *,
        trigger: SyncTrigger = SyncTrigger.MANUAL,
        remaining_source_ids: tuple[int, ...] = (),
    ) -> int:
        source = db.session.get(Source, source_id)
        if source is None:
            raise LookupError("Source not found")
        run_id = SyncOrchestrator(lambda: db.session(), self._factory(source)).enqueue(
            source_id, trigger=trigger
        )
        token = CancellationToken()
        with self._lock:
            self._tokens[run_id] = token

        def execute() -> None:
            with self._app.app_context():
                orchestrator: SyncOrchestrator | None = None
                source = db.session.get(Source, source_id)
                if source is None:
                    self._app.logger.error("queued synchronization source disappeared")
                    return
                try:
                    auto_enrich = db.session.get(Setting, "metadata.auto_after_sync")
                    enrich_after_sync = auto_enrich is not None and auto_enrich.value == "true"
                    orchestrator = SyncOrchestrator(
                        lambda: db.session(), self._factory(source), page_size=1000
                    )
                    result = orchestrator.run_queued(
                        run_id,
                        cancellation=token,
                        finalize_on_success=not enrich_after_sync,
                    )
                    if result.status is SyncStatus.SUCCEEDED and enrich_after_sync:
                        from euvieouvi.enrichment.service import enrich_catalog

                        orchestrator.update_progress(
                            run_id, "Enriquecendo metadados externos · preparando lote."
                        )

                        def report(counters: dict[str, int]) -> None:
                            token.raise_if_cancelled()
                            percent = counters.get("percent", 0)
                            if counters["processed"] % 5 != 0 and percent != 100:
                                return
                            total = counters.get("total", counters["processed"])
                            orchestrator.update_progress(
                                run_id,
                                "Enriquecendo metadados externos · "
                                f"{percent}% · {counters['processed']} de {total} processados, "
                                f"{counters['updated']} atualizados e "
                                f"{counters['failed']} falhas.",
                            )

                        counters = enrich_catalog(
                            self._app, progress=report, cancelled=lambda: token.is_cancelled
                        )
                        orchestrator.finish_success(
                            run_id,
                            "Sincronização e enriquecimento concluídos · "
                            f"{counters['processed']} metadados processados, "
                            f"{counters['updated']} atualizados e "
                            f"{counters['failed']} falhas seguras.",
                        )
                except Exception as error:
                    self._app.logger.exception("background synchronization failed")
                    db.session.rollback()
                    run = db.session.get(SyncRun, run_id)
                    if (
                        orchestrator is not None
                        and run is not None
                        and run.status
                        in {
                            SyncStatus.QUEUED,
                            SyncStatus.RUNNING,
                        }
                    ):
                        orchestrator.finish_failure(
                            run_id,
                            error,
                            "Sincronização ou enriquecimento falhou com segurança.",
                        )
                finally:
                    with self._lock:
                        self._tokens.pop(run_id, None)
                if remaining_source_ids:
                    try:
                        self.submit(
                            remaining_source_ids[0],
                            trigger=trigger,
                            remaining_source_ids=remaining_source_ids[1:],
                        )
                    except (LookupError, SyncAlreadyRunningError, SyncSourceUnavailableError):
                        self._app.logger.exception("next source synchronization was not queued")

        threading.Thread(target=execute, name="euvieouvi-sync", daemon=True).start()
        return run_id

    def submit_pending_watch_sync(self) -> bool:
        enabled = db.session.get(Setting, "watch_sync.enabled")
        pending = db.session.get(Setting, "watch_sync.pending")
        if enabled is None or enabled.value != "true" or pending is None or pending.value != "true":
            return False
        connector_types = set(
            db.session.scalars(
                select(Source.connector_type).where(
                    Source.enabled.is_(True),
                    Source.connector_type.in_([ConnectorType.PLEX, ConnectorType.JELLYFIN]),
                )
            )
        )
        if not {ConnectorType.PLEX, ConnectorType.JELLYFIN} <= connector_types:
            return False
        with self._lock:
            if self._watch_sync_running:
                return False
            self._watch_sync_running = True

        def execute_pending() -> None:
            try:
                with self._app.app_context():
                    WatchSyncService(lambda: db.session(), self._factory).run(None)
            except Exception:
                self._app.logger.exception("pending watched-state propagation failed")
            finally:
                with self._lock:
                    self._watch_sync_running = False

        threading.Thread(target=execute_pending, name="euvieouvi-watch-events", daemon=True).start()
        return True

    @property
    def watch_sync_active(self) -> bool:
        with self._lock:
            return self._watch_sync_running

    @property
    def watch_sync_snapshot(self) -> dict[str, int | bool | str]:
        with self._lock:
            return dict(self._watch_snapshot)

    def submit_directional_watch_sync(self, source_type: ConnectorType) -> bool:
        """Run watched-state propagation as an independent directional job."""
        with self._lock:
            if self._watch_sync_running:
                return False
            self._watch_sync_running = True
            self._watch_snapshot = {
                "active": True,
                "processed": 0,
                "updated": 0,
                "failed": 0,
                "percent": 1,
                "summary": "Analisando o catálogo e calculando apenas as diferenças.",
            }

        def execute() -> None:
            try:
                with self._app.app_context():
                    def report(scanned: int, updated: int, skipped: int, failed: int) -> None:
                        processed = min(scanned, updated + skipped + failed)
                        percent = 100 if scanned == 0 else processed * 100 // scanned
                        with self._lock:
                            self._watch_snapshot.update(
                                processed=processed,
                                updated=updated,
                                failed=failed,
                                percent=percent,
                                summary=(
                                    f"{processed} de {scanned} comparações processadas; "
                                    f"{skipped} já alinhadas."
                                ),
                            )

                    result = WatchSyncService(
                        lambda: db.session(), self._factory, progress=report
                    ).run(
                        None, source_type=source_type
                    )
                    with self._lock:
                        self._watch_snapshot.update(
                            processed=result.scanned,
                            updated=result.updated,
                            failed=result.failed,
                            percent=100,
                            summary=(
                                f"{result.updated} atualizados, {result.skipped} ignorados e "
                                f"{result.failed} falhas."
                            ),
                        )
            except Exception:
                self._app.logger.exception("directional watched-state propagation failed")
                with self._lock:
                    self._watch_snapshot.update(summary="Propagação falhou.", failed=1)
            finally:
                with self._lock:
                    self._watch_sync_running = False
                    self._watch_snapshot["active"] = False

        threading.Thread(target=execute, name="euvieouvi-watch-direction", daemon=True).start()
        return True

    def cancel(self, run_id: int) -> bool:
        with self._lock:
            token = self._tokens.get(run_id)
        if token is not None:
            token.cancel()
            return True
        run = db.session.get(SyncRun, run_id)
        return run is not None and run.status in {SyncStatus.QUEUED, SyncStatus.RUNNING}


def get_executor(app: Flask) -> LocalSyncExecutor:
    executor = app.extensions.get("euvieouvi.sync_executor")
    if not isinstance(executor, LocalSyncExecutor):
        concrete = app._get_current_object() if isinstance(app, LocalProxy) else app
        executor = LocalSyncExecutor(concrete)
        app.extensions["euvieouvi.sync_executor"] = executor
    return executor
