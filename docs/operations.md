# Operations guide

[Português do Brasil](operations.pt-BR.md) · English

## Deployment

Use Docker Engine and Docker Compose v2. Copy `.env.example` to `.env` and
`compose.yaml.sample` to `compose.yaml`. Set a long random `EUVIEOUVI_SECRET_KEY`.

```bash
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 euvieouvi
./scripts/validate-deployment.sh
```

The sample publishes port 8000, uses the named `euvieouvi_data` volume at `/data`, runs as
UID/GID 10001, drops Linux capabilities, prevents privilege escalation, and mounts the root
filesystem read-only except for the persistent volume and `/tmp`.

For a bind mount, create `compose.override.yaml` and make the host directory writable by 10001:

```yaml
services:
  euvieouvi:
    volumes:
      - ./data:/data
```

## Initial setup

1. Open the web UI and configure Plex and/or Jellyfin.
2. Test each connection.
3. Discover libraries and enable the desired ones.
4. Run the first synchronization.
5. Optionally configure schedules, metadata enrichment, backups, and webhooks.

Jellyfin needs an administrative API key and the tracked user's ID or name. Webhook user events
are ignored when they do not match the configured user.

## Reverse proxy and network security

The service has no authentication. Restrict port 8000 to a trusted network or place it behind an
authenticated HTTPS reverse proxy. Preserve the original host/scheme when generating webhook URLs.
Apply request-size and rate limits at the proxy, especially for webhook and restore-upload routes.
Do not cache authenticated pages or API responses containing private catalog data.

## Upgrade

1. Create a backup and copy it outside the Docker volume.
2. Fetch the new code/image.
3. Rebuild and recreate only the application container.
4. Validate readiness, migrations, logs, catalog, history, and sync.

```bash
docker compose exec -T euvieouvi python -m euvieouvi.database.backup \
  backup /data/euvieouvi.db /data/backups/pre-upgrade.db
docker compose cp euvieouvi:/data/backups/pre-upgrade.db ./pre-upgrade.db
docker compose build --pull
docker compose up -d --force-recreate
./scripts/validate-deployment.sh
docker compose logs --tail=100 euvieouvi
```

Startup applies Alembic migrations. An active sync lost during restart is marked `interrupted`;
committed pages and checkpoints remain available.

## Backup and restore

The web UI can create, download, delete, schedule, retain, and restore SQLite backups. Backups can
be created while the service runs. Avoid restoring during active requests; for the safest manual
restore, stop the main service:

```bash
docker compose stop euvieouvi
docker compose run --rm --no-deps euvieouvi python -m euvieouvi.database.backup \
  restore /data/backups/pre-upgrade.db /data/euvieouvi.db
docker compose up -d
./scripts/validate-deployment.sh
```

Keep an external copy until catalog, history, credentials, images, and synchronization are checked.
Backups contain secrets and require the same access controls as the live database.

## Monitoring and logs

```bash
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:8000/health/ready
docker compose logs -f --tail=200 euvieouvi
```

Logs use UTC timestamps and request IDs. Token-like values are redacted defensively, but review logs
before sharing. Readiness fails when SQLite is unavailable or the migration revision is not current;
connector downtime does not make the local catalog unavailable.

Each job also writes logs under `/data/job-logs`; retention per job is configured on the **Jobs**
page. To migrate an older installation, stop the container, copy all old `/app/instance` contents
into `/data`, preserve UID/GID 10001, and only then recreate the service. Keep `backups/` and
`images/` together with the database.

The **Optimize data** job is safe online: it rotates operational records, removes orphaned image
files, and runs `PRAGMA optimize`. Full `VACUUM` remains offline/manual because it locks writes and
can require temporary free space comparable to the database size. Back up and stop the service
before running `sqlite3 /data/euvieouvi.db 'VACUUM;'`.

## Troubleshooting

- **Readiness fails after upgrade:** inspect startup logs and run
  `docker compose exec euvieouvi flask --app euvieouvi.wsgi db current`.
- **Sync dependency failure:** test the source, rediscover libraries, verify the tracked Jellyfin
  user, and inspect the run's per-library errors. A malformed Jellyfin item is skipped safely.
- **Missing both catalog badges:** synchronize both enabled libraries. Exact provider IDs merge
  records; movies without IDs use a unique exact title/year fallback.
- **Webhook does not mark completion:** verify the secret URL, event type, configured Jellyfin user,
  and item availability. Pre-catalog completions reconcile on the next sync.
- **No current-playing item:** enable Plex play/resume or Jellyfin playback-start notifications.
- **SQLite busy errors:** avoid network filesystems, keep one application container, and increase
  `EUVIEOUVI_SQLITE_BUSY_TIMEOUT_MS` within its documented limit.
- **Artwork unavailable:** verify source connectivity and instance-directory permissions; external
  artwork requires the relevant enrichment provider.

## Validation and shutdown

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
uvx pip-audit --requirement requirements.lock
```

Real Plex validation is opt-in; see `tests/integration/test_real_plex.py`. Stop without deleting
data using `docker compose down`. Do not run `docker compose down -v` unless permanent volume
deletion is intended and a verified external backup exists.
