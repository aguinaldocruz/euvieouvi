"""Keep server-scoped state isolated when a configured server is replaced."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from euvieouvi.database.models import (
    AsyncTask,
    Library,
    MediaImage,
    Source,
    SourceMediaRef,
    SyncCheckpoint,
    WatchState,
    WebhookEvent,
)


def apply_server_identity(
    session: Session,
    source: Source,
    server_identifier: str | None,
    *,
    now: datetime,
) -> bool:
    """Persist identity and retire stale state after a confirmed server change.

    A missing stored identity is an upgrade/bootstrap case, so existing state is
    retained. Cleanup only happens when two known, different identities are seen.
    """
    identifier = (server_identifier or "").strip()
    if not identifier or source.server_identifier == identifier:
        return False
    if source.server_identifier is None:
        source.server_identifier = identifier
        return False

    libraries = session.scalars(select(Library).where(Library.source_id == source.id)).all()
    library_ids = [library.id for library in libraries]
    refs = session.scalars(
        select(SourceMediaRef).where(SourceMediaRef.source_id == source.id)
    ).all()

    for ref in refs:
        ref.external_id = _retired_identity("media", ref.id, ref.external_id)
        ref.external_key = None
        ref.available = False
        ref.unavailable_since = now
    for library in libraries:
        library.external_id = _retired_identity("library", library.id, library.external_id)
        library.available = False
        library.enabled = False

    if library_ids:
        session.execute(delete(SyncCheckpoint).where(SyncCheckpoint.library_id.in_(library_ids)))
    session.execute(delete(WatchState).where(WatchState.source_id == source.id))
    session.execute(delete(WebhookEvent).where(WebhookEvent.source_id == source.id))
    session.execute(delete(MediaImage).where(MediaImage.source_id == source.id))

    # Queued instant updates contain source IDs in JSON and must not be sent to
    # the replacement server using identifiers from the previous one.
    for task in session.scalars(select(AsyncTask).where(AsyncTask.task_type == "watch_update")):
        try:
            payload_source_id = int(json.loads(task.payload).get("source_id"))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if payload_source_id == source.id:
            session.delete(task)

    source.server_identifier = identifier
    return True


def _retired_identity(kind: str, row_id: int, external_id: str) -> str:
    digest = hashlib.sha256(external_id.encode()).hexdigest()[:16]
    return f"retired:{kind}:{row_id}:{digest}"
