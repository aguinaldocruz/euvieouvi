"""Persistent instance path tests."""

from pathlib import Path

import pytest

from euvieouvi.domain.errors import ConfigurationError
from euvieouvi.instance import instance_path_is_ready, prepare_instance_path


def test_prepare_instance_path_creates_directory(tmp_path: Path) -> None:
    instance_path = tmp_path / "nested" / "instance"

    prepare_instance_path(instance_path)

    assert instance_path_is_ready(instance_path)


def test_prepare_instance_path_rejects_regular_file(tmp_path: Path) -> None:
    instance_path = tmp_path / "not-a-directory"
    instance_path.write_text("data", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="Unable to create instance directory"):
        prepare_instance_path(instance_path)
