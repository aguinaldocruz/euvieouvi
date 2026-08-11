# Contributing to euvieouvi

[Português do Brasil](CONTRIBUTING.pt-BR.md) · English

Thank you for improving euvieouvi. Keep changes focused, tested, documented, and safe for existing
self-hosted installations.

## Before starting

- Search existing issues and pull requests to avoid duplicate work.
- Use an issue for substantial behavior or schema proposals before implementation.
- Never include tokens, webhook URLs, databases, backups, private media metadata, or logs with
  personal data.
- Keep unrelated local changes out of the commit.

## Development workflow

1. Create a topic branch from the repository's default branch.
2. Install Python 3.12 and development dependencies with `pip install -e ".[dev]"`.
3. Implement a small, cohesive change with tests.
4. Add an Alembic migration for schema changes; never rewrite a migration already released.
5. Update both English and Brazilian Portuguese documentation when user behavior changes.
6. Run the quality gates:

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

Tests must not require real Plex/Jellyfin credentials or network access. Use sanitized fixtures and
mock transports. Real-server tests must remain explicitly opt-in.

## Coding expectations

- Target Python 3.12 and strict typing.
- Preserve connector boundaries: connectors do not access the database.
- Preserve pagination, idempotency, checkpoint-after-commit, and single-sync guarantees.
- Treat database migrations, restore behavior, and credential handling as high-risk changes.
- Keep UI writes CSRF-protected and API errors consistent with the OpenAPI contract.
- Do not weaken container privilege, read-only filesystem, or secret-redaction controls.

## Commits and pull requests

Use clear imperative commit subjects. A pull request should explain the problem, the chosen
solution, migrations/configuration impact, tests performed, and screenshots for visible UI changes.
Link the relevant issue when one exists. Keep the pull request reviewable and do not mix formatting
or refactoring with unrelated behavior.

By contributing, you confirm that you have the right to submit the work. This repository currently
has no license file; contribution acceptance does not itself grant redistribution rights.
