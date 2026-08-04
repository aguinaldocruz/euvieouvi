"""Mark orphaned running executions interrupted during controlled startup."""

from datetime import UTC, datetime

from euvieouvi import create_app
from euvieouvi.database.enums import SyncStatus
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
    work.commit()
    return len(runs)


def main() -> None:
    app = create_app()
    with app.app_context():
        count = reconcile_orphaned_runs()
    print(f"euvieouvi reconciled orphaned sync runs: {count}", flush=True)


if __name__ == "__main__":
    main()
