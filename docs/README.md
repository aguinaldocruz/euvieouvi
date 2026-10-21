# Project documentation

[Português do Brasil](README.pt-BR.md) · English

This directory contains the maintained documentation for euvieouvi. The root
[README](../README.md) is the product overview and quick start.

## Maintained guides

| Guide | Purpose |
| --- | --- |
| [Operations](operations.md) | Deployment, upgrades, backup/restore, monitoring, and troubleshooting. |
| [Trakt offline import](trakt-offline-import.md) | Safe one-time import from a complete Trakt export. |
| [OpenAPI contract](../openapi.yaml) | REST API paths, parameters, payloads, and error envelopes. |
| [Contributing](../CONTRIBUTING.md) | Development workflow, tests, migrations, and pull requests. |
| [Security](../SECURITY.md) | Supported version and private vulnerability reporting. |

## System behavior

The application has five major runtime areas:

1. **Web/API:** Flask routes render Jinja pages and expose JSON under `/api/v1`.
2. **Connectors:** Plex XML and Jellyfin JSON are mapped into connector-neutral DTOs.
3. **Synchronization:** library snapshots are paged, persisted transactionally, checkpointed,
   and reconciled. A global lock permits one active synchronization.
4. **Persistence:** SQLAlchemy models and repositories store canonical media, per-source
   references, images, completed events, aggregate watch state, sync audit data, and settings.
5. **Background services:** a database-backed worker claims independent jobs with a renewable
   lease and delegates their bounded work to local executors. The durable instant-update queue is
   separate; the scheduler enqueues enabled daily jobs at most once per local day and persists
   bounded operational health samples.

Media identity prefers exact provider identifiers. Exact title/year fallback is restricted to
movies when one unique match exists across sources. Source references remain separate, allowing
one catalog item to show both Plex and Jellyfin availability.

Watch events represent known completed occurrences. Watch states represent the latest aggregate
reported by a server. Webhook events record recent delivery/current playback and are reconciled
into watch history when a matching source reference becomes available.

Jobs keep persistent progress, results, rotated execution logs, and one globally active execution.
Expired leases are requeued with bounded backoff; cancellation intent is committed before being
forwarded to the executor. The preferred media pipeline order is Plex catalog, Plex → Jellyfin
watched state, Jellyfin → Plex watched state, then Jellyfin catalog.
The watched-state reconciler uses incremental checkpoints plus periodic full baselines; source
identity/configuration changes invalidate those checkpoints.

The Docker runtime pins SQLite 3.53.4 and production refuses older runtimes affected by the
WAL-reset race. Operational health history is retained for 30 days and exposed as a 24-hour
summary without credentials or task payloads.

Operational settings are stored in SQLite and edited at `/setup`. Persisted connector and provider
credentials use authenticated encryption. Docker creates the master key in `/data`; the fixed
internal port and data mount remain bootstrap infrastructure because they are needed before the
database can be opened. Plex account authorization uses Plex's hosted PIN flow and never stores a
Plex password.

## Historical architecture records

Files under [`architecture/`](architecture/) document earlier design approvals and implementation
phases. They are retained for decision history, but statements about current scope or feature
availability may be obsolete. Current runtime behavior, the maintained guides above, migrations,
tests, and OpenAPI contract take precedence.

[`validation-phase8.md`](validation-phase8.md) is a dated validation record, not a claim about the
current release. [`architecture/evolution-media-catalog.md`](architecture/evolution-media-catalog.md)
is an implementation history.

## Documentation rules

- User-facing maintained documentation is published in English and Brazilian Portuguese.
- English is the default filename; Portuguese counterparts use `.pt-BR.md`.
- Keep heading order and factual content aligned across translations.
- Use relative links, fenced commands, explicit security warnings, and no real credentials.
- Update documentation in the same pull request as behavior, configuration, API, or schema changes.
