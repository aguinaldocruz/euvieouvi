"""Connector construction and bounded in-process sync execution."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from importlib.metadata import version

from flask import Flask
from werkzeug.local import LocalProxy

from euvieouvi.connectors.base import MediaConnector
from euvieouvi.connectors.plex.client import PlexHttpClient
from euvieouvi.connectors.plex.connector import PlexConnector
from euvieouvi.database.enums import SyncStatus, SyncTrigger
from euvieouvi.database.models import Setting, Source, SyncRun
from euvieouvi.extensions import db
from euvieouvi.sync.cancellation import CancellationToken
from euvieouvi.sync.orchestrator import SyncOrchestrator

ConnectorFactory = Callable[[Source], MediaConnector]


def connector_for(source: Source) -> MediaConnector:
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

    def submit(self, source_id: int, *, trigger: SyncTrigger = SyncTrigger.MANUAL) -> int:
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
                source = db.session.get(Source, source_id)
                if source is None:
                    self._app.logger.error("queued synchronization source disappeared")
                    return
                try:
                    result = SyncOrchestrator(
                        lambda: db.session(), self._factory(source)
                    ).run_queued(
                        run_id, cancellation=token
                    )
                    auto_enrich = db.session.get(Setting, "metadata.auto_after_sync")
                    if (
                        result.status is SyncStatus.SUCCEEDED
                        and auto_enrich is not None
                        and auto_enrich.value == "true"
                    ):
                        from euvieouvi.enrichment.runtime import get_enrichment_executor

                        get_enrichment_executor(self._app).submit()
                except BaseException:
                    self._app.logger.exception("background synchronization failed")
                finally:
                    with self._lock:
                        self._tokens.pop(run_id, None)

        threading.Thread(target=execute, name="euvieouvi-sync", daemon=True).start()
        return run_id

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
