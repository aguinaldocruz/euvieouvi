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
- Daily schedules shared by both sources or configured per source.
- Optional TMDB, MusicBrainz, and Cover Art Archive enrichment using exact identifiers.
- Local artwork cache and historical retention for unavailable media.
- Light and dark themes, responsive server-rendered UI, and REST API.
- Scheduled/manual SQLite backups, restore, download, and retention controls.
- Offline import of a complete Trakt history export.

## Architecture

euvieouvi is a modular Flask monolith using Jinja, HTMX, Bootstrap, SQLAlchemy 2, Alembic,
SQLite, and Gunicorn. Connectors map external payloads to neutral DTOs; synchronization services
perform reconciliation and persistence through repositories and a unit of work. Only one sync
run is active at a time.

Persistent data is stored under the Flask instance directory:

```text
instance/
├── euvieouvi.db
├── backups/
└── images/
```

See [Project documentation](docs/README.md) for the component model, data behavior, API,
security boundaries, and documentation map.

## Requirements

- Docker Engine and Docker Compose v2 for the recommended deployment; or
- Python 3.12+, Git, and a virtual environment for development.

Plex requires a server URL and token. Jellyfin requires a server URL, API key, and the ID or
name of the user whose playback state is synchronized.

## Quick start with Docker Compose

```bash
cp .env.example .env
cp compose.yaml.sample compose.yaml
```

Set a long random `EUVIEOUVI_SECRET_KEY` in `.env`, then start the service:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f euvieouvi
```

Open <http://localhost:8000>, configure Plex and/or Jellyfin, discover libraries, enable the
desired libraries, and start a synchronization.

The named `euvieouvi_data` volume survives container recreation. The entrypoint validates the
configuration, applies database migrations, and reconciles interrupted sync runs before
Gunicorn starts.

Operational endpoints:

- `GET /health/live` — process liveness, without dependency details.
- `GET /health/ready` — database connectivity and migration readiness.
- `/api/v1` — REST API described by [openapi.yaml](openapi.yaml).

For upgrades, backups, restore, reverse-proxy guidance, and troubleshooting, read the
[operations guide](docs/operations.md).

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `EUVIEOUVI_ENV` | `production` | `development`, `production`, or `testing`. |
| `EUVIEOUVI_SECRET_KEY` | none | Required Flask/session secret. Use a long random value. |
| `EUVIEOUVI_HOST` | `0.0.0.0` | Bind address. |
| `EUVIEOUVI_PORT` | `8000` | Application port, 1–65535. |
| `EUVIEOUVI_INSTANCE_PATH` | `./instance` | Database, image cache, and backup directory. |
| `EUVIEOUVI_DATABASE_URI` | SQLite in the instance path | SQLite URI; other databases are unsupported. |
| `EUVIEOUVI_LOG_LEVEL` | `INFO` | `CRITICAL`, `ERROR`, `WARNING`, `INFO`, or `DEBUG`. |
| `EUVIEOUVI_TIMEZONE` | `America/Sao_Paulo` | Valid IANA timezone used by schedules and display. |
| `EUVIEOUVI_GUNICORN_THREADS` | `4` | Gunicorn threads, 1–32. |
| `EUVIEOUVI_SQLITE_BUSY_TIMEOUT_MS` | `5000` | SQLite busy timeout, 1–60000 ms. |

Connector credentials, schedules, webhook tokens, retention rules, and metadata settings are
stored through the web interface. Secrets are write-only in the API and defensively redacted
from logs.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
export EUVIEOUVI_ENV=development
export EUVIEOUVI_SECRET_KEY=local-development-only
flask --app euvieouvi:create_app run
```

Windows PowerShell:

```powershell
$env:EUVIEOUVI_ENV = "development"
$env:EUVIEOUVI_SECRET_KEY = "local-development-only"
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
sync. The page keeps the configured number of recent completed webhook events.

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
