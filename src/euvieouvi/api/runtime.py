"""Connector construction and bounded in-process sync execution."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
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
    return PlexConnector(client)


class LocalSyncExecutor:
    """Run one synchronization in a daemon thread and expose cooperative cancellation."""

    def __init__(self, app: Flask, factory: ConnectorFactory | None = None) -> None:
        self._app = app
        self._factory = factory or connector_for
        self._tokens: dict[int, CancellationToken] = {}
        self._lock = threading.Lock()
        self._watch_sync_running = False

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
                    orchestrator = SyncOrchestrator(lambda: db.session(), self._factory(source))
                    result = orchestrator.run_queued(
                        run_id,
                        cancellation=token,
                        finalize_on_success=not enrich_after_sync,
                    )
                    watch_sync = db.session.get(Setting, "watch_sync.enabled")
                    watch_sync_enabled = watch_sync is not None and watch_sync.value == "true"
                    if result.status is SyncStatus.SUCCEEDED and enrich_after_sync:
                        from euvieouvi.enrichment.service import enrich_catalog

                        orchestrator.update_progress(
                            run_id, "Enriquecendo metadados externos · preparando lote."
                        )

                        def report(counters: dict[str, int]) -> None:
                            token.raise_if_cancelled()
                            if counters["processed"] % 5 != 0:
                                return
                            orchestrator.update_progress(
                                run_id,
                                "Enriquecendo metadados externos · "
                                f"{counters['processed']} processados, "
                                f"{counters['updated']} atualizados e "
                                f"{counters['failed']} falhas.",
                            )

                        counters = enrich_catalog(self._app, progress=report)
                        orchestrator.finish_success(
                            run_id,
                            "Sincronização e enriquecimento concluídos · "
                            f"{counters['processed']} metadados processados, "
                            f"{counters['updated']} atualizados e "
                            f"{counters['failed']} falhas seguras.",
                        )
                    if result.status is SyncStatus.SUCCEEDED and watch_sync_enabled:
                        self._run_watch_sync(run_id)
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

    def submit_watch_sync(self, run_id: int) -> bool:
        run = db.session.get(SyncRun, run_id)
        if run is None or run.status is not SyncStatus.SUCCEEDED:
            return False
        if run.watch_sync_status in {SyncStatus.QUEUED, SyncStatus.RUNNING}:
            return False
        run.watch_sync_status = SyncStatus.QUEUED
        run.watch_sync_started_at = None
        run.watch_sync_finished_at = None
        run.watch_sync_scanned = 0
        run.watch_sync_updated = 0
        run.watch_sync_skipped = 0
        run.watch_sync_failed = 0
        run.watch_sync_summary = "Propagação aguardando início."
        db.session.commit()

        def execute_watch_sync() -> None:
            with self._app.app_context():
                self._run_watch_sync(run_id)

        threading.Thread(
            target=execute_watch_sync, name="euvieouvi-watch-sync", daemon=True
        ).start()
        return True

    def _run_watch_sync(self, run_id: int) -> None:
        try:
            WatchSyncService(lambda: db.session(), self._factory).run(run_id)
        except Exception:
            self._app.logger.exception("watched-state propagation failed")
            db.session.rollback()
            run = db.session.get(SyncRun, run_id)
            if run is not None:
                run.watch_sync_status = SyncStatus.FAILED
                run.watch_sync_finished_at = datetime.now(UTC)
                run.watch_sync_summary = "Propagação de conclusões falhou com segurança."
                db.session.commit()

    def submit_all(
        self, source_ids: tuple[int, ...], *, trigger: SyncTrigger = SyncTrigger.MANUAL
    ) -> int:
        if not source_ids:
            raise LookupError("No source available")
        return self.submit(
            source_ids[0],
            trigger=trigger,
            remaining_source_ids=source_ids[1:],
        )

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
