"""Static checks for infrastructure invariants when Docker is unavailable."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_uses_pinned_python_and_non_root_user() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.12.13-slim-bookworm AS sqlite-builder")
    assert "SQLITE_AUTOCONF_VERSION=3530400" in dockerfile
    assert "sqlite3.sqlite_version == '3.53.4'" in dockerfile
    assert "USER euvieouvi" in dockerfile
    assert 'CMD ["gunicorn"' in dockerfile


def test_compose_preserves_data_and_drops_privileges() -> None:
    compose = (PROJECT_ROOT / "compose.yaml.sample").read_text(encoding="utf-8")

    assert "euvieouvi_data:/data" in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    assert "environment:" not in compose
    assert '"8000:8000"' in compose


def test_production_lock_contains_runtime_dependencies() -> None:
    lock = (PROJECT_ROOT / "requirements.lock").read_text(encoding="utf-8")

    assert "Flask==3.1.3" in lock
    assert "gunicorn==26.0.0" in lock
    assert "cryptography==50.0.1" in lock


def test_gunicorn_control_socket_is_disabled_for_read_only_container() -> None:
    config = (PROJECT_ROOT / "gunicorn.conf.py").read_text(encoding="utf-8")
    assert "control_socket_disable = True" in config


def test_build_context_excludes_sensitive_and_runtime_data() -> None:
    ignored = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    for required in (".git", ".env", "*.db", "instance", "data", "tests"):
        assert required in ignored

    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY ." not in dockerfile
    assert "EUVIEOUVI_SECRET_KEY" not in dockerfile
    assert "pip install --no-deps ." in dockerfile


def test_operational_validation_script_is_non_destructive() -> None:
    script = (PROJECT_ROOT / "scripts" / "validate-deployment.sh").read_text(encoding="utf-8")
    assert "id -u" in script
    assert all(path in script for path in ("/health/live", "/health/ready", "/health/metrics"))
    assert "sqlite_runtime_safe" in script
    assert "test -w /data" in script
    assert "down -v" not in script and "rm " not in script
