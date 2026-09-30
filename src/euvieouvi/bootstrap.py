"""Container startup validation."""

from euvieouvi.config import load_settings
from euvieouvi.database.sqlite_runtime import require_safe_sqlite_runtime
from euvieouvi.instance import prepare_instance_path


def main() -> None:
    """Validate startup configuration and persistent storage."""
    settings = load_settings()
    if settings.environment == "production":
        require_safe_sqlite_runtime()
    prepare_instance_path(settings.instance_path)
    print("euvieouvi startup validation succeeded", flush=True)


if __name__ == "__main__":
    main()
