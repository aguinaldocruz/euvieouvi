# Offline Trakt history import

[`Português do Brasil`](trakt-offline-import.pt-BR.md) · English

The `scripts/import_trakt_export.py` utility imports `watched-history-N.json` files from a full
Trakt export ZIP. It runs outside Docker, uses the Python standard library, and writes directly to
the stopped application's SQLite database.

## Preconditions

- Python 3.12 on the host.
- A Plex source already stored in the database.
- The original full Trakt export ZIP and the exact `euvieouvi.db` path.
- The application container completely stopped.

The importer accepts Alembic revisions `20260805_0009` and `20260811_0010`. Upgrade the
application and inspect `flask db current` first. Unsupported schemas are rejected without writes.

Identity uses Plex GUID, IMDb, TMDB, TVDB, and Trakt IDs—not fuzzy titles. Missing historical media
is created without a false Plex availability reference. Trakt does not include artwork; later sync
or optional exact-ID metadata enrichment can supply it.

## Dry run

```bash
docker compose stop euvieouvi
docker compose ps
```

Run as container UID/GID 10001 to preserve file ownership:

```bash
sudo setpriv --reuid=10001 --regid=10001 --clear-groups \
  python3 scripts/import_trakt_export.py
```

```bash
python3 scripts/import_trakt_export.py --help
```

Interactive mode asks for the archive, database, confirmation that Docker is stopped, and—when
needed—the Plex source. Without `--apply`, the transaction is always rolled back.

```bash
sudo setpriv --reuid=10001 --regid=10001 --clear-groups \
  python3 scripts/import_trakt_export.py \
  --archive /home/docker/import/trakt-export.zip \
  --database /home/docker/euvieouvi/euvieouvi.db \
  --confirm-docker-down \
  --progress-every 1000 \
  --report /home/docker/euvieouvi/trakt-dry-run.json
```

Review `invalid_events` and `ambiguous_events`; both should normally be zero. Progress frequency is
controlled by `--progress-every N` or disabled with `--no-progress`.

## Apply

```bash
sudo setpriv --reuid=10001 --regid=10001 --clear-groups \
  python3 scripts/import_trakt_export.py \
  --archive /home/docker/import/trakt-export.zip \
  --database /home/docker/euvieouvi/euvieouvi.db \
  --apply \
  --report /home/docker/euvieouvi/trakt-import.json
```

The utility requires the literal confirmation `IMPORTAR` and creates a sibling backup first:

```text
euvieouvi.db.pre-trakt-20260804T200000000000Z.bak
```

The import is one transaction and rolls back on error. Event IDs use `trakt:<id>`, making repeated
imports idempotent.

## Return to service

```bash
sudo ls -lah /home/docker/euvieouvi
docker compose up -d
./scripts/validate-deployment.sh
```

Keep the automatic pre-import backup until the history has been verified in the UI.
