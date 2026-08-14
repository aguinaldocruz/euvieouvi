"""Persistent daily scheduler behavior."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask
from pytest import MonkeyPatch

from euvieouvi.database.enums import SyncTrigger
from euvieouvi.database.models import Setting
from euvieouvi.extensions import db
from euvieouvi.sync import scheduler


def test_independent_job_schedule_runs_once_per_day(app: Flask, monkeypatch: MonkeyPatch) -> None:
    calls: list[tuple[str, SyncTrigger]] = []

    def submit(_app: Flask, job_id: str, *, trigger: SyncTrigger) -> bool:
        calls.append((job_id, trigger))
        return True

    monkeypatch.setattr("euvieouvi.sync.jobs.submit_job", submit)
    with app.app_context():
        db.session.add_all(
            [
                Setting(key="jobs.metadata.enabled", value="true"),
                Setting(key="jobs.metadata.time", value="04:00"),
            ]
        )
        db.session.commit()
        now = datetime(2026, 8, 5, 4, 1, tzinfo=ZoneInfo("America/Sao_Paulo"))

        assert scheduler._run_jobs_if_due(app, now=now) is True
        assert scheduler._run_jobs_if_due(app, now=now) is False
        assert calls == [("metadata", SyncTrigger.SCHEDULED)]
        assert db.session.get(Setting, "jobs.metadata.last_date").value == "2026-08-05"  # type: ignore[union-attr]
