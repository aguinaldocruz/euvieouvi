"""Opt-in real Plex gate; never stores credentials in fixtures or reports."""

from __future__ import annotations

import os

import pytest
from flask import Flask
from sqlalchemy import func, select

from euvieouvi import __version__
from euvieouvi.connectors.plex.client import PlexHttpClient
from euvieouvi.connectors.plex.connector import PlexConnector
from euvieouvi.database.enums import ConnectorType, LibraryMediaType, SyncStatus, SyncTrigger
from euvieouvi.database.models import Library, MediaItem, Source, WatchState, utc_now
from euvieouvi.extensions import db
from euvieouvi.sync.orchestrator import SyncOrchestrator


@pytest.mark.real_plex
def test_small_real_plex_library_is_idempotent(app: Flask) -> None:
    if os.getenv("EUVIEOUVI_RUN_REAL_PLEX") != "1":
        pytest.skip("set EUVIEOUVI_RUN_REAL_PLEX=1 to enable the controlled real Plex gate")
    base_url = os.environ.get("EUVIEOUVI_TEST_PLEX_URL", "")
    token = os.environ.get("EUVIEOUVI_TEST_PLEX_TOKEN", "")
    library_external_id = os.environ.get("EUVIEOUVI_TEST_PLEX_LIBRARY_ID", "")
    if not base_url or not token or not library_external_id:
        pytest.fail(
            "real Plex gate requires URL, token and an explicitly selected small library ID"
        )

    client = PlexHttpClient(
        base_url,
        token,
        application_version=__version__,
        client_identifier="euvieouvi-controlled-real-test",
        retries=0,
    )
    connector = PlexConnector(client)
    try:
        connection = connector.test_connection()
        assert connection.authenticated
        discovered = {item.external_id: item for item in connector.list_libraries()}
        assert library_external_id in discovered
        selected = discovered[library_external_id]
        source = Source(
            connector_type=ConnectorType.PLEX,
            name="Controlled real Plex test",
            base_url=base_url,
            secret=token,
            enabled=True,
        )
        db.session.add(source)
        db.session.flush()
        library = Library(
            source_id=source.id,
            external_id=selected.external_id,
            name=selected.name,
            media_type=LibraryMediaType(selected.media_type.value),
            enabled=True,
            available=True,
            discovered_at=utc_now(),
            last_seen_at=utc_now(),
        )
        db.session.add(library)
        db.session.commit()

        orchestrator = SyncOrchestrator(lambda: db.session(), connector, page_size=50)
        first = orchestrator.run(source.id, trigger=SyncTrigger.MANUAL)
        assert first.status is SyncStatus.SUCCEEDED
        first_counts = (
            db.session.scalar(select(func.count()).select_from(MediaItem)),
            db.session.scalar(select(func.count()).select_from(WatchState)),
        )
        second = orchestrator.run(source.id, trigger=SyncTrigger.MANUAL)
        assert second.status is SyncStatus.SUCCEEDED
        second_counts = (
            db.session.scalar(select(func.count()).select_from(MediaItem)),
            db.session.scalar(select(func.count()).select_from(WatchState)),
        )
        assert first_counts == second_counts
        assert (first_counts[0] or 0) > 0
    finally:
        client.close()
