"""Container startup validation."""

from euvieouvi.config import load_settings
from euvieouvi.instance import prepare_instance_path


def main() -> None:
    """Validate startup configuration and persistent storage."""
    settings = load_settings()
    prepare_instance_path(settings.instance_path)
    print("euvieouvi startup validation succeeded", flush=True)


if __name__ == "__main__":
    main()
