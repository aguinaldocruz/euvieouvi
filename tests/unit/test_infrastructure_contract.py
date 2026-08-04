"""Static checks for infrastructure invariants when Docker is unavailable."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_uses_pinned_python_and_non_root_user() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.12.13-slim-bookworm")
    assert "USER euvieouvi" in dockerfile
    assert 'CMD ["gunicorn"' in dockerfile


def test_compose_preserves_data_and_drops_privileges() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "euvieouvi_data:/app/instance" in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose and "- ALL" in compose


def test_production_lock_contains_runtime_dependencies() -> None:
    lock = (PROJECT_ROOT / "requirements.lock").read_text(encoding="utf-8")

    assert "Flask==3.1.3" in lock
    assert "gunicorn==26.0.0" in lock
