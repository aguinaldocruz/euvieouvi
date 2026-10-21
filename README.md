# euvieouvi

[Português do Brasil](README.pt-BR.md) · English

Self-hosted media catalog and playback-history service for Plex and Jellyfin. It synchronizes
movies, television, and music into a local SQLite database while retaining completed playback
history and records for media that later disappears from a server.

> [!IMPORTANT]
> euvieouvi has no built-in authentication. Run it on a trusted network or behind an
> authenticated reverse proxy. Never publish webhook URLs, database files, backups, or server
> tokens.

## Features

- Plex and Jellyfin sources, connection tests, and library discovery.
- Movies, shows, seasons, episodes, artists, albums, and tracks.
- Idempotent, paginated synchronization with checkpoints and safe cancellation.
- Combined Plex/Jellyfin availability on catalog entries.
- Completed playback history with source and acquisition origin (`webhook` or
  `synchronization`).
- Plex and Jellyfin webhooks, recent-event retention, and currently playing media.
- Durable asynchronous webhook updates with idempotency, automatic retries, restart recovery,
  periodic/page-activity draining, and a manual queue job.
- Independent manual/daily jobs use a database-backed queue with leases, heartbeats, bounded
  retries, persistent cancellation, restart recovery through synchronization checkpoints, live
  progress, results, and rotated logs. Available
  Plex/Jellyfin artwork is proxied on demand without exposing tokens. Episodes and tracks inherit
  available series/season or album/artist artwork without duplicating files. If server artwork is absent
  or returns 404, the image job uses exact TMDB/IMDb/TheTVDB/MusicBrainz identifiers to recover and
  cache a safe TMDB or Cover Art Archive fallback; it never guesses by title.
- Optional TMDB, MusicBrainz, and Cover Art Archive enrichment using exact identifiers.
- Local artwork cache and historical retention for unavailable media.
- Persistent English/Brazilian Portuguese selection (English by default), light/dark themes,
  responsive server-rendered UI, and REST API.
- Scheduled/manual SQLite backups, restore, download, and retention controls.
- Docker ships SQLite 3.53.4 with the upstream WAL-reset correction; production startup rejects
  older affected runtimes. Operational samples and alerts are retained for 30 days.
- Offline import of a complete Trakt history export.

## Architecture

euvieouvi is a modular Flask monolith using Jinja, HTMX, Bootstrap, SQLAlchemy 2, Alembic,
SQLite, and Gunicorn. Connectors map external payloads to neutral DTOs; synchronization services
perform reconciliation and persistence through repositories and a unit of work. Only one sync
run is active at a time.

Persistent data is stored under the Flask instance directory:

```text
data/
├── euvieouvi.db
├── backups/
├── images/
└── job-logs/
```

See [Project documentation](docs/README.md) for the component model, data behavior, API,
security boundaries, and documentation map.

## Requirements

- Docker Engine and Docker Compose v2 for the recommended deployment; or
- Python 3.12+, Git, and a virtual environment for development.

Plex requires a server URL and authorization through Plex's hosted sign-in page; euvieouvi never
receives the Plex password. Jellyfin uses its Quick Connect device flow; approve the displayed code
from an already signed-in Jellyfin client. euvieouvi receives a user access token, never the account
password. Existing administrative API-key configurations remain usable until reconnected.

## Quick start with Docker Compose

Copy the Compose sample and start the service; no `.env` file or application secret is required:

```bash
cp compose.yaml.sample compose.yaml
docker compose up -d --build
docker compose ps
docker compose logs -f euvieouvi
```

Open <http://localhost:8000/setup>, save the operational settings, configure Plex and/or Jellyfin,
discover libraries, enable the desired libraries, and start a synchronization.

The named `euvieouvi_data` volume survives container recreation. The entrypoint validates the
configuration, applies database migrations, and reconciles interrupted sync runs before
Gunicorn starts.

Operational endpoints:

- `GET /health/live` — process liveness, without dependency details.
- `GET /health/ready` — database connectivity and migration readiness.
- `GET /health/metrics` — non-secret queue, worker, connector, storage, alert, and 24-hour
  historical telemetry.
- `/api/v1` — REST API described by [openapi.yaml](openapi.yaml).

For upgrades, backups, restore, reverse-proxy guidance, and troubleshooting, read the
[operations guide](docs/operations.md).

## Configuration

Log level, time zone, HTTP thread count, and SQLite busy timeout are stored in SQLite and edited at
`/setup`. Connector settings, schedules, retention, metadata, and appearance are also stored
through the web interface. The Docker-internal port (`8000`) and `/data` mount remain in Compose
because both must exist before SQLite can be opened.

Connector credentials, metadata tokens, and webhook tokens are encrypted at rest. The application
creates `/data/.euvieouvi-master.key` with mode `0600`; preserve this file securely with database
backups. Secrets remain write-only in the API and are defensively redacted from logs.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
export EUVIEOUVI_ENV=development
flask --app euvieouvi:create_app run
```

Windows PowerShell:

```powershell
$env:EUVIEOUVI_ENV = "development"
flask --app euvieouvi:create_app run
```

Quality gates:

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

Create database migrations with Flask-Migrate, review generated operations, and always test
upgrade and downgrade behavior against a disposable database. Production startup automatically
runs `flask --app euvieouvi.wsgi db upgrade`.

## Webhooks

The Webhooks settings page generates secret URLs for each configured connector.

- Plex: configure the URL under **Settings → Webhooks**. Playback starts/resumes drive the
  current-playing view; `media.scrobble` creates a completion.
- Jellyfin: configure the official Webhook plugin with the generated URL and playback start/stop
  notifications. A stop with `PlayedToCompletion=true` creates a completion.

Completions received before the item is cataloged are retained and reconciled after the next
sync. Progress updates arriving while an older update is being processed are retained as the
next durable queue item rather than overwritten. The page keeps the configured number of recent
completed webhook events.

## API

The REST API covers sources, libraries, sync runs, media, watch events, watch states, and the
dashboard summary. Requests and responses use JSON under `/api/v1`; cursor pagination and error
formats are documented in [openapi.yaml](openapi.yaml). CORS is disabled by default and the API
does not add authentication.

## Data and privacy

The database contains media metadata, playback history, connector credentials, webhook tokens,
and application settings. Backups contain the same sensitive data. Artwork is cached in the
instance directory. No cloud service is required, but optional enrichment contacts TMDB,
MusicBrainz, and Cover Art Archive when enabled.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a change. Report vulnerabilities using
the private process in [SECURITY.md](SECURITY.md), not a public issue.

No license file is currently included. Unless the repository owner states otherwise, do not
assume permission to redistribute modified or unmodified copies.
