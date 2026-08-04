"""Neutral connector DTO validation tests."""

from datetime import UTC, datetime

import pytest

from euvieouvi.connectors.dtos import (
    ExternalMediaItem,
    ExternalMediaKind,
    ExternalWatchEvent,
    Page,
    PageRequest,
)


def test_page_request_and_page_are_defensive() -> None:
    with pytest.raises(ValueError, match="start"):
        PageRequest(start=-1)
    with pytest.raises(ValueError, match="size"):
        PageRequest(size=1001)
    with pytest.raises(ValueError, match="advance"):
        Page(items=("item",), start=10, size=1, next_start=10)

    assert Page(items=(), start=0, size=0, next_start=None).has_more is False
    assert Page(items=("item",), start=0, size=1, next_start=1).has_more is True


def test_episode_requires_its_hierarchy() -> None:
    with pytest.raises(ValueError, match="show_external_id"):
        ExternalMediaItem(
            external_id="1",
            library_external_id="2",
            kind=ExternalMediaKind.EPISODE,
            title="Episode",
            season_number=1,
            episode_number=1,
        )


def test_dtos_reject_naive_time_and_negative_progress() -> None:
    with pytest.raises(ValueError, match="timezone"):
        ExternalWatchEvent(
            media_external_id="1",
            library_external_id="2",
            watched_at=datetime(2026, 8, 4),
            completed=True,
        )
    with pytest.raises(ValueError, match="nonnegative"):
        ExternalWatchEvent(
            media_external_id="1",
            library_external_id="2",
            watched_at=datetime.now(UTC),
            completed=True,
            progress_ms=-1,
        )
