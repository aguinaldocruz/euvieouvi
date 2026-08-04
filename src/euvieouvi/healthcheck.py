"""Dependency-free container healthcheck client."""

from __future__ import annotations

import os
from urllib.request import urlopen


def main() -> int:
    """Return a process status suitable for Docker HEALTHCHECK."""
    port = os.getenv("EUVIEOUVI_PORT", "8000")
    url = f"http://127.0.0.1:{port}/health/ready"
    try:
        with urlopen(url, timeout=3) as response:
            return 0 if response.status == 200 else 1
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
