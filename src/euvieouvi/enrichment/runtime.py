"""Single local background executor for optional enrichment."""

from __future__ import annotations

import threading

from flask import Flask, current_app
from werkzeug.local import LocalProxy

from euvieouvi.enrichment.service import enrich_catalog


class LocalEnrichmentExecutor:
    def __init__(self, app: Flask) -> None:
        self._app = app
        self._lock = threading.Lock()
        self._active = False

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def submit(self) -> bool:
        with self._lock:
            if self._active:
                return False
            self._active = True

        def execute() -> None:
            try:
                with self._app.app_context():
                    enrich_catalog(self._app)
            finally:
                with self._lock:
                    self._active = False

        threading.Thread(target=execute, name="euvieouvi-enrichment", daemon=True).start()
        return True


def get_enrichment_executor(app: Flask | None = None) -> LocalEnrichmentExecutor:
    value = app or current_app
    concrete = value._get_current_object() if isinstance(value, LocalProxy) else value
    executor = concrete.extensions.get("euvieouvi.enrichment_executor")
    if not isinstance(executor, LocalEnrichmentExecutor):
        executor = LocalEnrichmentExecutor(concrete)
        concrete.extensions["euvieouvi.enrichment_executor"] = executor
    return executor
