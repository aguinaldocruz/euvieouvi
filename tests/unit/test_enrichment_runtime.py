"""Bounded single-thread enrichment execution."""

import time
from threading import Event

from flask import Flask

from euvieouvi.enrichment import runtime


def test_enrichment_executor_allows_only_one_active_run(app: Flask, monkeypatch: object) -> None:
    started = Event()
    release = Event()

    def run(application: Flask) -> dict[str, int]:
        assert application is app
        started.set()
        assert release.wait(2)
        return {"processed": 0, "updated": 0, "failed": 0}

    monkeypatch.setattr(runtime, "enrich_catalog", run)  # type: ignore[attr-defined]
    executor = runtime.get_enrichment_executor(app)
    assert executor.submit() is True
    assert started.wait(2)
    assert executor.active is True
    assert executor.submit() is False
    assert runtime.get_enrichment_executor(app) is executor
    release.set()

    def is_active() -> bool:
        return executor.active

    for _ in range(100):
        if not is_active():
            break
        time.sleep(0.01)
    assert executor.active is False
