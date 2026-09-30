#!/bin/sh
set -eu

service="euvieouvi"

docker compose ps "$service"

uid="$(docker compose exec -T "$service" id -u)"
if [ "$uid" = "0" ]; then
    echo "validation failed: application runs as root" >&2
    exit 1
fi

docker compose exec -T "$service" test -w /data
docker compose exec -T "$service" python - <<'PY'
import json
import sqlite3
from urllib.request import urlopen

for path, expected in (
    ("/health/live", "alive"),
    ("/health/ready", "ready"),
):
    with urlopen(f"http://127.0.0.1:8000{path}", timeout=5) as response:
        payload = json.load(response)
    if payload.get("status") != expected:
        raise SystemExit(f"unexpected {path} response: {payload}")

with urlopen("http://127.0.0.1:8000/health/metrics", timeout=5) as response:
    metrics = json.load(response)
if not metrics.get("database", {}).get("sqlite_runtime_safe"):
    raise SystemExit(f"unsafe SQLite runtime in metrics: {metrics.get('database')}")
if sqlite3.sqlite_version != metrics["database"]["sqlite_runtime"]:
    raise SystemExit("SQLite runtime differs between process and metrics")
for key in ("jobs", "async_tasks", "alerts", "history"):
    if key not in metrics:
        raise SystemExit(f"missing operational metric: {key}")
PY

echo "deployment validation passed (uid=$uid)"
