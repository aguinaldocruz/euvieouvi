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
        self._progress: dict[str, int] = {
            "processed": 0,
            "updated": 0,
            "failed": 0,
            "total": 0,
            "percent": 0,
        }

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def snapshot(self) -> dict[str, int | bool]:
        with self._lock:
            return {"active": self._active, **self._progress}

    def submit(self) -> bool:
        with self._lock:
            if self._active:
                return False
            self._active = True
            self._progress = {
                "processed": 0,
                "updated": 0,
                "failed": 0,
                "total": 0,
                "percent": 0,
            }

        def execute() -> None:
            def report(counters: dict[str, int]) -> None:
                with self._lock:
                    self._progress.update(counters)

            try:
                with self._app.app_context():
                    enrich_catalog(self._app, progress=report)
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
