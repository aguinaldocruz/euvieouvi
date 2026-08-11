"""Mark orphaned running executions interrupted during controlled startup."""

from datetime import UTC, datetime

from sqlalchemy import select

from euvieouvi import create_app
from euvieouvi.database.enums import SyncStatus
from euvieouvi.database.models import SyncRun
from euvieouvi.database.unit_of_work import UnitOfWork
from euvieouvi.extensions import db


def reconcile_orphaned_runs() -> int:
    """Interrupt active rows when no previous process can still own them."""
    now = datetime.now(UTC)
    work = UnitOfWork(db.session())
    runs = tuple(work.sync_runs.running())
    for run in runs:
        run.status = SyncStatus.INTERRUPTED
        run.finished_at = now
        run.heartbeat_at = now
        run.summary = "Execution interrupted during a previous process lifetime."
        for detail in work.sync_run_libraries.for_run(run.id):
            if detail.status in {SyncStatus.QUEUED, SyncStatus.RUNNING}:
                detail.status = SyncStatus.INTERRUPTED
                detail.finished_at = now
                detail.message = run.summary
    watch_runs = tuple(
        db.session.scalars(
            select(SyncRun).where(
                SyncRun.watch_sync_status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING])
            )
        )
    )
    for run in watch_runs:
        run.watch_sync_status = SyncStatus.INTERRUPTED
        run.watch_sync_finished_at = now
        run.watch_sync_summary = (
            "Propagação interrompida durante uma execução anterior do aplicativo."
        )
    work.commit()
    return len(runs) + len(watch_runs)


def main() -> None:
    app = create_app()
    with app.app_context():
        count = reconcile_orphaned_runs()
    print(f"euvieouvi reconciled orphaned sync runs: {count}", flush=True)


if __name__ == "__main__":
    main()
