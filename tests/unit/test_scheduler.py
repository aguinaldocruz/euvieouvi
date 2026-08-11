"""Persistent daily scheduler behavior."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from flask import Flask

from euvieouvi.database.enums import ConnectorType, SyncTrigger
from euvieouvi.database.models import Setting, Source
from euvieouvi.extensions import db
from euvieouvi.sync import scheduler


def _seed_schedule(*, enabled: bool = True, time: str = "03:00") -> Source:
    source = Source(
        connector_type=ConnectorType.PLEX,
        name="Plex",
        base_url="http://plex:32400",
        secret="secret",
        enabled=True,
    )
    db.session.add_all(
        [
            source,
            Setting(key="sync.schedule.enabled", value="true" if enabled else "false"),
            Setting(key="sync.schedule.time", value=time),
        ]
    )
    db.session.commit()
    return source


def test_due_schedule_queues_once_per_local_date(app: Flask, monkeypatch: object) -> None:
    calls: list[tuple[int, SyncTrigger]] = []

    class Executor:
        def submit(self, source_id: int, *, trigger: SyncTrigger) -> int:
            calls.append((source_id, trigger))
            return 42

    with app.app_context():
        source = _seed_schedule()
        monkeypatch.setattr(scheduler, "get_executor", lambda application: Executor())  # type: ignore[attr-defined]
        now = datetime(2026, 8, 5, 3, 15, tzinfo=ZoneInfo("America/Sao_Paulo"))
        assert scheduler._run_if_due(app, now=now) is True
        assert scheduler._run_if_due(app, now=now) is False
        assert calls == [(source.id, SyncTrigger.SCHEDULED)]
        assert db.session.get(Setting, "sync.schedule.last_date").value == "2026-08-05"  # type: ignore[union-attr]


def test_pending_watch_propagation_delegates_to_executor(app: Flask, monkeypatch: object) -> None:
    calls: list[bool] = []

    class Executor:
        def submit_pending_watch_sync(self) -> bool:
            calls.append(True)
            return True

    with app.app_context():
        monkeypatch.setattr(scheduler, "get_executor", lambda application: Executor())  # type: ignore[attr-defined]
        assert scheduler._run_watch_sync_if_pending(app) is True
        assert calls == [True]


def test_schedule_disabled_early_or_invalid_is_not_started(app: Flask, monkeypatch: object) -> None:
    with app.app_context():
        _seed_schedule(enabled=False)
        monkeypatch.setattr(  # type: ignore[attr-defined]
            scheduler, "get_executor", lambda application: SimpleNamespace(submit=lambda *a, **k: 1)
        )
        due = datetime(2026, 8, 5, 4, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))
        assert scheduler._run_if_due(app, now=due) is False
        db.session.get(Setting, "sync.schedule.enabled").value = "true"  # type: ignore[union-attr]
        db.session.commit()
        early = datetime(2026, 8, 5, 2, 59, tzinfo=ZoneInfo("America/Sao_Paulo"))
        assert scheduler._run_if_due(app, now=early) is False
        db.session.get(Setting, "sync.schedule.time").value = "invalid"  # type: ignore[union-attr]
        db.session.commit()
        assert scheduler._run_if_due(app, now=due) is False
