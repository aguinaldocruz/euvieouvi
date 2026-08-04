#!/bin/sh
set -eu

service="euvieouvi"

docker compose ps "$service"

uid="$(docker compose exec -T "$service" id -u)"
if [ "$uid" = "0" ]; then
    echo "validation failed: application runs as root" >&2
    exit 1
fi

docker compose exec -T "$service" test -w /app/instance
docker compose exec -T "$service" python - <<'PY'
import json
from urllib.request import urlopen

for path, expected in (("/health/live", "alive"), ("/health/ready", "ready")):
    with urlopen(f"http://127.0.0.1:8000{path}", timeout=5) as response:
        payload = json.load(response)
    if payload.get("status") != expected:
        raise SystemExit(f"unexpected {path} response: {payload}")
PY

echo "deployment validation passed (uid=$uid)"
