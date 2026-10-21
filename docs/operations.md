# Operations guide

[Português do Brasil](operations.pt-BR.md) · English

## Deployment

Use Docker Engine and Docker Compose v2. Copy `compose.yaml.sample` to `compose.yaml`. Docker does
not require an `.env` file; operational application settings are persisted in SQLite.

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

The image builds SQLite 3.53.4 from the official source archive, verifies its SHA3-256 before
compilation, and checks the Python runtime during the image build. Production startup refuses a
SQLite version affected by the upstream WAL-reset race. Confirm the active runtime with:

```bash
docker compose exec -T euvieouvi python -c 'import sqlite3; print(sqlite3.sqlite_version)'
```

The application creates `/data/.euvieouvi-master.key` on first start with mode `0600`. Plex,
Jellyfin, TMDB, and webhook credentials are encrypted at rest with this key. Preserve it securely:
a copied database cannot recover its credentials without the matching key. An optional externally
managed `EUVIEOUVI_MASTER_KEY` remains supported for advanced secret-management deployments, but
must be available before the database is read.

For a bind mount, create `compose.override.yaml` and make the host directory writable by 10001:

```yaml
services:
  euvieouvi:
    volumes:
      - ./data:/data
```

## Initial setup

1. Open `/setup` and save the log level, time zone, HTTP threads, and SQLite timeout.
2. Save the Plex server URL and use **Sign in with Plex**. Authentication happens at Plex, so the
   application never receives or stores the account password. For Jellyfin, save the server URL,
   start **Quick Connect**, and approve the displayed code in an already signed-in client.
3. Test each connection.
4. Discover libraries and enable the desired ones.
5. Run the first synchronization and configure schedules, metadata, backups, and webhooks.

Jellyfin returns a user access token and identity through Quick Connect; the password is never sent
to euvieouvi. Quick Connect must be enabled on the server. Existing administrative API-key sources
remain supported until they are reconnected. Webhook events from other users are ignored.

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
docker compose cp euvieouvi:/data/.euvieouvi-master.key ./euvieouvi-master.key
docker compose build --pull
docker compose up -d --force-recreate
./scripts/validate-deployment.sh
docker compose logs --tail=100 euvieouvi
```

Store the copied master key with access controls equivalent to a password. Startup applies
Alembic migrations and encrypts any legacy plaintext credentials idempotently before serving
requests. An active low-level sync lost during restart is marked
`interrupted`, while its operational job is requeued and starts a new sync from the last committed
checkpoint. Queued jobs remain queued. The worker claims only one job globally, renews a 45-second
lease, records a heartbeat, and retries worker/dispatch failures with bounded backoff (three
attempts by default, configurable from 1 to 10 on the Jobs page). Persistent cancellation survives
a restart. The startup reconciliation also rebuilds the
persistent catalog projection when it is missing or dirty. This projection stores canonical
Plex/Jellyfin groups, availability, playback summaries, and series progress so catalog requests do
not rescan the full history. Source, identity, and playback writes mark it dirty transactionally;
the next catalog read also repairs it if startup or a job has not already done so. Playback writes
record the affected media IDs and refresh only their hierarchy; identity/source changes request a
full rebuild because they can alter canonical membership.

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
curl --fail http://localhost:8000/health/metrics
docker compose logs -f --tail=200 euvieouvi
```

Logs use UTC timestamps and request IDs. Token-like values are redacted defensively, including the
secret suffix in Plex/Jellyfin webhook access-log paths, but review logs before sharing. Readiness
fails when SQLite is unavailable or the migration revision is not current; connector downtime does
not make the local catalog unavailable.

Each job also writes logs under `/data/job-logs`; retention per job is configured on the **Jobs**
page. To migrate an older installation, stop the container, copy all old `/app/instance` contents
into `/data`, preserve UID/GID 10001, and only then recreate the service. Keep `backups/` and
`images/` together with the database.

The home and Jobs pages receive live state through server-sent events (SSE), with automatic
reconnection and a polling fallback. Reverse proxies must not buffer `/jobs/events`; the response
already sends `X-Accel-Buffering: no` and periodic keepalives. The Jobs page also shows queue age,
worker heartbeat, connector latency/failures, and recent operations taking at least one second.
After five consecutive transient connector failures, that process pauses new calls to the source
for 60 seconds; authentication, configuration, malformed-response, and not-found errors do not
open this circuit. This protection is intentionally process-local and resets on restart.

The scheduler stores one operational sample at most every five minutes and removes samples older
than 30 days. The UI and `/health/metrics` summarize the last 24 hours. Alerts are raised when a
job heartbeat is older than 45 seconds (critical after 90), a job or instant-update item has waited
over five minutes, a connector circuit is open, or repeated connector failures are detected.
Database and WAL sizes are also tracked. These samples are small rollups, not request traces.

Use **Download diagnostics** on the Jobs page to create a bounded ZIP containing operational
counts, up to 288 recent operational samples, recent job/task/error records, and the tail of at
most 20 job logs. Credentials and known
token forms are redacted; the database, settings values, task payloads, and complete log history
are not included. Still review the archive before sharing because titles, source names, internal
addresses, and media identifiers can be private.

The recommended daily order is **Sync Plex → Watched Plex to Jellyfin → Watched Jellyfin to Plex →
Sync Jellyfin** (defaults 03:00, 03:15, 03:30, and 03:45). The persistent dispatcher prioritizes
that order when multiple rows are ready and prevents concurrent operational jobs. Existing custom
times are preserved during upgrades.

The **Optimize data** job rotates job and synchronization history, removes orphaned image files,
runs `PRAGMA optimize`, and compacts SQLite with `VACUUM`. It is automated and safe to launch from
the UI, but `VACUUM` briefly takes an exclusive database lock and can need temporary free space.
Temporary SQLite files are kept under persistent `/data/sqlite-tmp`, not the container's bounded
`/tmp`; keep at least twice the database size free and schedule outside active playback/sync periods.

Instant webhook changes are committed to `async_tasks` before external delivery. The queue drains
on events, periodically, during page activity, or through **Process update queue**. Failures remain
with exponential backoff, in-flight newer progress becomes a successor item, and restart recovery
returns interrupted work to pending state. A queued completion supersedes an older partial-progress
event for the same item. Missing source mappings are recovered with a targeted item lookup; stale
remote mappings that return 404 are disabled and the next valid reference is tried. After 12
consecutive failures an item is isolated from automatic draining, while the manual queue job resets
and retries isolated items so one bad event cannot keep every queue run failing forever.

Directional watched jobs establish a full baseline, then compare only changed watch state,
history, source references, and provider identities. A full reconciliation is forced at least every
seven days. Changing/disabling a Plex or Jellyfin source resets these checkpoints. Remote APIs still
require one write per actual delta because neither service offers a compatible bulk update endpoint;
local state is persisted in batches.

Plex/Jellyfin artwork is proxied on demand without exposing credentials or keeping a duplicate local
copy. An episode or track without its own art inherits the first available series/season or
album/artist image; canonical equivalent entries are also considered. When an image is absent or
returns 404, the application queues the image job; that job resolves
only exact TMDB/IMDb/TheTVDB/MusicBrainz identifiers and caches TMDB or Cover Art Archive artwork.
It deliberately does not perform ambiguous title searches.

If Jellyfin returns no structured provider IDs, official bracketed tokens in movie or series
names (for example `[tmdb-15119]`, `[tvdbid-254049]`, or `[imdbid-tt0281433]`) provide an exact
identity during synchronization and targeted event lookups. Structured IDs take precedence.
The reconciliation job also recovers these IDs from existing catalog titles, cleans display
titles, logs each affected item, and merges confirmed cross-source duplicates with their history.
Conflicting tokens and ambiguous source ownership are preserved for review. Once the series is
confirmed, unique episode coordinates can recover identifierless children.

## Read-only load and soak validation

The repository includes a reproducible check for readiness, metrics, job status, and one-shot SSE.
It does not enqueue jobs or change application data:

```bash
python scripts/operational_load_test.py \
  --base-url http://localhost:8000 --duration 60 --concurrency 6
```

For a longer soak, use `--duration 1800`. The command returns nonzero if the error rate exceeds 1%
or p95 latency exceeds 1000 ms; adjust `--max-error-rate` and `--max-p95-ms` only when the deployed
hardware or reverse proxy has an explicitly different service objective.

## Troubleshooting

- **Readiness fails after upgrade:** inspect startup logs and run
  `docker compose exec euvieouvi flask --app euvieouvi.wsgi db current`.
- **Sync dependency failure:** test the source, rediscover libraries, verify the tracked user, and
  inspect the run log/per-library errors. Malformed Plex/Jellyfin items are identified by media ID
  and title, counted as safe failures, and skipped without discarding the rest of the page.
- **Missing both catalog badges:** synchronize both enabled libraries. Exact provider IDs merge
  records in the persistent catalog projection; top-level media without IDs use a normalized exact
  title/year fallback, while hierarchy children are scoped to their parent to avoid false matches.
- **Webhook does not mark completion:** verify the secret URL, event type, configured Jellyfin user,
  and item availability. Pre-catalog completions reconcile on the next sync.
- **No current-playing item:** enable Plex play/resume or Jellyfin playback-start notifications.
- **SQLite busy errors:** avoid network filesystems, keep one application container, and increase
  the SQLite busy timeout on the System settings page.
- **Credentials cannot be decrypted:** restore the matching `.euvieouvi-master.key` into `/data`
  with ownership UID/GID 10001 and mode `0600`; a different key cannot decrypt an existing DB.
- **Artwork unavailable:** verify source connectivity and instance-directory permissions; external
  artwork requires the relevant enrichment provider.

## Physical identity and cumulative completion

Under **Metadata → Media identity**, map each server's mount roots to the same logical root
(`/mnt/tv => /shared/tv`, `/media/series => /shared/tv`). Only map roots containing the same
physical media. Full paths are case sensitive; a filename alone is never sufficient evidence.
The next full sync collects paths from Plex/Jellyfin without mounting media in euvieouvi.
Show reconciliation uses shared episode files with compatible coordinates; ambiguous ownership,
simultaneous editions and conflicting manual matches are retained separately. Collected paths
are visible under **Physical media references** on media detail pages.
A show split into two entries on one server may be unified only after all their paths have
been collected and another server groups those same files under a single show. Matching IDs
alone do not authorize this merge; unconfirmed files keep the group separate.

**Fix match** on movie/show details searches TMDB or accepts an exact ID, including history-only
entries. It replaces catalog metadata/IDs, preserves playback history and dates, protects the
match during future syncs and queues available poster/backdrop images for download. It does
not modify server metadata. A configured TMDB credential is required; a match can be corrected again.
The correction also reconciles safe matching catalog records immediately, keeping the selected
record and its history. Unavailable old references do not conflict with current references;
simultaneous editions remain protected. Artwork URLs change when images change, and missing
covers are not browser-cached. Plex's existing batched parent lookup also populates show year,
summary and artwork, without a request per episode.

Select **Unidentified first** under catalog sorting to bring entries without TMDB, TVDB,
IMDb or MusicBrainz identifiers to the top. Identification checks include all grouped records;
a Plex/Jellyfin internal ID alone is not sufficient. This sort is remembered between visits,
and ascending/descending controls titles within each group. A missing identifier is a review
hint, not proof that the title is wrong.

Completion is cumulative: unplayed responses and resume updates never erase known completion.
Older timestamps do not replace the latest known playback date. Jobs and webhooks propagate
completion in both directions, never unwatched toggles.

**Last played** sorting uses actual playback timestamps from server history, server playback
dates or accepted live webhooks. Synchronization observation times never count as playback.
Imported resume positions with no playback date remain undated; progress is still retained
and propagated. A queued webhook retains its original event time even if processed later.
The playback-date migration rebuilds the catalog without changing watched history or dates.

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
