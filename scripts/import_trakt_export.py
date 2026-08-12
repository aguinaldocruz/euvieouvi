#!/usr/bin/env python3
"""One-time, offline importer for a complete Trakt export into euvieouvi SQLite."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

HISTORY_NAME = re.compile(r"watched-history-(\d+)\.json\Z")
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
REQUIRED_TABLES = {
    "alembic_version",
    "media_identifiers",
    "media_items",
    "sources",
    "watch_events",
    "watch_states",
}
SUPPORTED_DATABASE_REVISIONS = {"4ac542335f9b", "20260811_0011"}
Progress = Callable[[str], None]


class ImportFailure(RuntimeError):
    """Safe operator-facing import failure."""


@dataclass(slots=True)
class ImportReport:
    mode: str
    archive: str
    database: str
    source_id: int
    history_files: int = 0
    events_read: int = 0
    events_valid: int = 0
    events_inserted: int = 0
    events_already_imported: int = 0
    media_matched: int = 0
    media_created: int = 0
    shows_created: int = 0
    seasons_created: int = 0
    identifiers_added: int = 0
    invalid_events: int = 0
    ambiguous_events: int = 0
    committed: bool = False
    backup: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    event_id: str
    watched_at: str
    kind: str
    title: str
    year: int | None
    season_number: int | None
    episode_number: int | None
    identifiers: tuple[tuple[str, str], ...]
    show_title: str | None = None
    show_year: int | None = None
    show_identifiers: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True)
class MediaIndex:
    by_identifier: defaultdict[tuple[str, str, str], set[int]]
    preferred_media_ids: set[int]

    @classmethod
    def load(cls, connection: sqlite3.Connection, *, source_id: int) -> MediaIndex:
        values: defaultdict[tuple[str, str, str], set[int]] = defaultdict(set)
        rows = connection.execute(
            """
            SELECT m.kind, mi.provider, mi.external_id, mi.media_item_id
            FROM media_identifiers AS mi
            JOIN media_items AS m ON m.id = mi.media_item_id
            """
        )
        for row in rows:
            values[(str(row[0]), str(row[1]), str(row[2]))].add(int(row[3]))
        preferred = {
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT media_item_id FROM source_media_refs WHERE source_id = ?",
                (source_id,),
            )
        }
        return cls(values, preferred)

    def add(
        self,
        kind: str,
        media_id: int,
        identifiers: tuple[tuple[str, str], ...],
    ) -> None:
        for provider, external_id in identifiers:
            self.by_identifier[(kind, provider, external_id)].add(media_id)

    def match(self, kind: str, identifiers: tuple[tuple[str, str], ...]) -> int | None:
        scores: defaultdict[int, int] = defaultdict(int)
        for provider, external_id in identifiers:
            for media_id in self.by_identifier.get((kind, provider, external_id), ()):
                scores[media_id] += 1
        if not scores:
            return None
        best_score = max(scores.values())
        best = [media_id for media_id, score in scores.items() if score == best_score]
        preferred = [media_id for media_id in best if media_id in self.preferred_media_ids]
        if len(preferred) == 1:
            return preferred[0]
        return best[0] if len(best) == 1 else -1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Importa uma única vez o histórico do export completo do Trakt.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  Dry-run interativo (não grava):
    python3 scripts/import_trakt_export.py

  Dry-run com caminhos informados:
    python3 scripts/import_trakt_export.py \\
      --archive /dados/trakt-export.zip \\
      --database /home/docker/euvieouvi/euvieouvi.db \\
      --confirm-docker-down

  Importação definitiva, ainda pedindo a palavra IMPORTAR:
    python3 scripts/import_trakt_export.py \\
      --archive /dados/trakt-export.zip \\
      --database /home/docker/euvieouvi/euvieouvi.db \\
      --apply

Sem --apply, a transação sempre termina em rollback (dry-run).
""",
    )
    parser.add_argument(
        "--archive",
        metavar="ARQUIVO.zip",
        type=Path,
        help="ZIP completo do Trakt; se omitido, pergunta no terminal",
    )
    parser.add_argument(
        "--database",
        metavar="euvieouvi.db",
        type=Path,
        help="banco SQLite do euvieouvi; se omitido, pergunta no terminal",
    )
    parser.add_argument(
        "--source-id",
        metavar="ID",
        type=int,
        help="fonte Plex; seleção automática quando existe somente uma",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="grava após backup; sem esta opção executa dry-run",
    )
    parser.add_argument(
        "--confirm-docker-down",
        action="store_true",
        help="confirma sem pergunta que o contêiner está completamente parado",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="com --apply, dispensa digitar IMPORTAR; somente para automação controlada",
    )
    parser.add_argument(
        "--report", metavar="RELATORIO.json", type=Path, help="grava o relatório em JSON"
    )
    parser.add_argument(
        "--progress-every",
        metavar="N",
        type=_positive_int,
        default=1000,
        help="mostra progresso a cada N eventos (padrão: 1000)",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="oculta progresso; mantém o relatório final",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    progress: Progress | None = None if args.no_progress else _print_progress
    try:
        archive = _resolved_input(args.archive, "Caminho do ZIP do Trakt: ")
        database = _resolved_input(args.database, "Caminho do euvieouvi.db: ")
        _confirm_docker_is_down(args.confirm_docker_down)
        _validate_paths(archive, database)

        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            _validate_database(connection)
            source_id = _select_source(connection, args.source_id)
            if args.apply and not args.yes:
                confirmation = input(
                    "Digite IMPORTAR para criar o backup e gravar definitivamente: "
                ).strip()
                if confirmation != "IMPORTAR":
                    raise ImportFailure("Importação cancelada; confirmação não recebida.")
            report = import_archive(
                connection,
                archive,
                database,
                source_id=source_id,
                apply=args.apply,
                progress=progress,
                progress_every=args.progress_every,
            )
        finally:
            connection.close()
        _print_report(report)
        if args.report is not None:
            _write_report(args.report.expanduser().resolve(), report)
        return 0
    except (ImportFailure, BadZipFile, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        print(f"ERRO: {error}", file=sys.stderr)
        return 1


def import_archive(
    connection: sqlite3.Connection,
    archive: Path,
    database: Path,
    *,
    source_id: int,
    apply: bool,
    progress: Progress | None = None,
    progress_every: int = 1000,
) -> ImportReport:
    _emit(progress, "Fase 1/4 — lendo e validando o export do Trakt")
    events, history_files, invalid = _load_history(archive, progress=progress)
    report = ImportReport(
        mode="apply" if apply else "dry-run",
        archive=str(archive),
        database=str(database),
        source_id=source_id,
        history_files=history_files,
        events_read=len(events) + invalid,
        events_valid=len(events),
        invalid_events=invalid,
    )
    if not events:
        raise ImportFailure("Nenhum evento válido foi encontrado no export do Trakt.")

    if apply:
        _emit(progress, "Criando backup consistente antes da transação de escrita")
        backup_path = _backup_database(connection, database)
        report.backup = str(backup_path)
        _emit(progress, f"Backup criado: {backup_path}")

    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _emit(progress, "Fase 2/4 — indexando e associando mídias existentes")
        media_index = MediaIndex.load(connection, source_id=source_id)
        matched: dict[int, int] = {}
        ambiguous: set[int] = set()

        # First pass enriches existing catalog IDs and show ancestry before creating anything.
        for processed, event in enumerate(events, start=1):
            index = processed - 1
            _batch_progress(progress, "associação", processed, len(events), progress_every)
            media_id = media_index.match(event.kind, event.identifiers)
            if media_id == -1:
                ambiguous.add(index)
                report.ambiguous_events += 1
                continue
            if media_id is None:
                continue
            matched[index] = media_id
            report.media_matched += 1
            report.identifiers_added += _add_identifiers(
                connection,
                media_id,
                event.identifiers,
                media_index=media_index,
                kind=event.kind,
            )
            if event.kind == "episode":
                show_id = _show_for_episode(connection, media_id)
                if show_id is not None:
                    report.identifiers_added += _add_identifiers(
                        connection,
                        show_id,
                        event.show_identifiers,
                        media_index=media_index,
                        kind="show",
                    )

        _emit(progress, "Fase 3/4 — criando mídias ausentes e importando eventos")
        touched_media: set[int] = set()
        for processed, event in enumerate(events, start=1):
            index = processed - 1
            _batch_progress(progress, "eventos", processed, len(events), progress_every)
            if index in ambiguous:
                continue
            media_id = matched.get(index)
            if media_id is None:
                rematched = media_index.match(event.kind, event.identifiers)
                if rematched == -1:
                    report.ambiguous_events += 1
                    continue
                media_id = rematched
            if media_id is None:
                media_id = _create_historical_media(connection, event, report, media_index)
            source_event_id = f"trakt:{event.event_id}"
            exists = connection.execute(
                "SELECT 1 FROM watch_events WHERE source_id = ? AND source_event_id = ?",
                (source_id, source_event_id),
            ).fetchone()
            if exists is not None:
                report.events_already_imported += 1
                continue
            now = _utc_now()
            dedup_key = hashlib.sha256(
                f"trakt|{event.event_id}|{media_id}|{event.watched_at}".encode()
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO watch_events (
                    media_item_id, source_id, source_event_id, dedup_key, watched_at,
                    completed, progress_ms, duration_ms, view_number, created_at, updated_at, origin
                ) VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, NULL, ?, ?, 'trakt_import')
                """,
                (
                    media_id,
                    source_id,
                    source_event_id,
                    dedup_key,
                    event.watched_at,
                    now,
                    now,
                ),
            )
            report.events_inserted += 1
            touched_media.add(media_id)

        _emit(progress, "Fase 4/4 — recalculando o estado assistido agregado")
        touched = sorted(touched_media)
        for processed, media_id in enumerate(touched, start=1):
            _batch_progress(progress, "estados", processed, len(touched), progress_every)
            _refresh_watch_state(connection, media_id, source_id)

        if touched:
            now = _utc_now()
            connection.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES ('watch_sync.pending', 'true', ?)
                ON CONFLICT(key) DO UPDATE SET value = 'true', updated_at = excluded.updated_at
                """,
                (now,),
            )

        if apply:
            connection.commit()
            report.committed = True
            _emit(progress, "Transação confirmada com sucesso")
        else:
            connection.rollback()
            _emit(progress, "Dry-run concluído; rollback integral executado")
    except BaseException:
        connection.rollback()
        raise
    return report


def _load_history(
    archive: Path, *, progress: Progress | None = None
) -> tuple[list[HistoryEvent], int, int]:
    events: list[HistoryEvent] = []
    invalid = 0
    with ZipFile(archive) as bundle:
        members = []
        total_size = 0
        for member in bundle.infolist():
            match = HISTORY_NAME.fullmatch(member.filename)
            if match is None:
                continue
            if member.file_size > MAX_MEMBER_BYTES:
                raise ImportFailure(f"Arquivo interno excede o limite: {member.filename}")
            total_size += member.file_size
            if total_size > MAX_ARCHIVE_BYTES:
                raise ImportFailure("Histórico descomprimido excede o limite de segurança.")
            members.append((int(match.group(1)), member))
        if not members:
            raise ImportFailure("O ZIP não contém watched-history-N.json.")
        ordered = sorted(members, key=lambda item: item[0])
        for position, (_, member) in enumerate(ordered, start=1):
            with bundle.open(member) as stream:
                document = json.load(stream)
            if not isinstance(document, list):
                raise ImportFailure(f"{member.filename} não contém uma lista JSON.")
            for raw in document:
                event = _parse_event(raw)
                if event is None:
                    invalid += 1
                else:
                    events.append(event)
            _emit(
                progress,
                f"  arquivo {position}/{len(ordered)}: {member.filename} — "
                f"válidos acumulados={len(events)}, inválidos={invalid}",
            )
    return events, len(members), invalid


def _parse_event(raw: Any) -> HistoryEvent | None:
    if not isinstance(raw, dict) or raw.get("type") not in {"movie", "episode"}:
        return None
    event_id = _text(raw.get("id"))
    watched_at = _timestamp(raw.get("watched_at"))
    kind = str(raw["type"])
    media = raw.get(kind)
    if event_id is None or watched_at is None or not isinstance(media, dict):
        return None
    title = _text(media.get("title"))
    identifiers = _identifiers(media.get("ids"))
    if title is None or not identifiers:
        return None
    if kind == "movie":
        return HistoryEvent(
            event_id,
            watched_at,
            kind,
            title,
            _nonnegative_int(media.get("year")),
            None,
            None,
            identifiers,
        )
    show = raw.get("show")
    if not isinstance(show, dict):
        return None
    show_title = _text(show.get("title"))
    season = _nonnegative_int(media.get("season"))
    number = _nonnegative_int(media.get("number"))
    show_ids = _identifiers(show.get("ids"))
    if show_title is None or season is None or number is None or not show_ids:
        return None
    return HistoryEvent(
        event_id,
        watched_at,
        kind,
        title,
        None,
        season,
        number,
        identifiers,
        show_title,
        _nonnegative_int(show.get("year")),
        show_ids,
    )


def _identifiers(raw: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, dict):
        return ()
    values: set[tuple[str, str]] = set()
    for provider in ("trakt", "imdb", "tmdb", "tvdb"):
        value = _text(raw.get(provider))
        if value is not None and len(value) <= 255:
            values.add((provider, value))
    plex = raw.get("plex")
    if isinstance(plex, dict):
        guid = _text(plex.get("guid"))
        if guid is not None:
            if guid.startswith("plex://"):
                guid = guid.split("://", 1)[1]
            if len(guid) <= 255:
                values.add(("plex", guid))
    return tuple(sorted(values))


def _create_historical_media(
    connection: sqlite3.Connection,
    event: HistoryEvent,
    report: ImportReport,
    media_index: MediaIndex,
) -> int:
    now = _utc_now()
    if event.kind == "movie":
        media_id = _insert_media(
            connection,
            kind="movie",
            title=event.title,
            year=event.year,
            parent_id=None,
            season_number=None,
            episode_number=None,
            now=now,
        )
        report.media_created += 1
        report.identifiers_added += _add_identifiers(
            connection,
            media_id,
            event.identifiers,
            media_index=media_index,
            kind="movie",
        )
        return media_id

    show_id = media_index.match("show", event.show_identifiers)
    if show_id == -1:
        raise ImportFailure("Identificadores ambíguos para um programa do histórico.")
    if show_id is None:
        assert event.show_title is not None
        show_id = _insert_media(
            connection,
            kind="show",
            title=event.show_title,
            year=event.show_year,
            parent_id=None,
            season_number=None,
            episode_number=None,
            now=now,
        )
        report.shows_created += 1
        report.identifiers_added += _add_identifiers(
            connection,
            show_id,
            event.show_identifiers,
            media_index=media_index,
            kind="show",
        )
    assert event.season_number is not None
    season_row = connection.execute(
        """
        SELECT id FROM media_items
        WHERE kind = 'season' AND parent_id = ? AND season_number = ?
        ORDER BY id LIMIT 1
        """,
        (show_id, event.season_number),
    ).fetchone()
    if season_row is None:
        season_id = _insert_media(
            connection,
            kind="season",
            title=f"Season {event.season_number}",
            year=None,
            parent_id=show_id,
            season_number=event.season_number,
            episode_number=None,
            now=now,
        )
        report.seasons_created += 1
    else:
        season_id = int(season_row[0])
    media_id = _insert_media(
        connection,
        kind="episode",
        title=event.title,
        year=None,
        parent_id=season_id,
        season_number=event.season_number,
        episode_number=event.episode_number,
        now=now,
    )
    report.media_created += 1
    report.identifiers_added += _add_identifiers(
        connection,
        media_id,
        event.identifiers,
        media_index=media_index,
        kind="episode",
    )
    return media_id


def _insert_media(
    connection: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    year: int | None,
    parent_id: int | None,
    season_number: int | None,
    episode_number: int | None,
    now: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO media_items (
            kind, parent_id, title, original_title, sort_title, year, season_number,
            episode_number, duration_ms, originally_available_on, summary, created_at, updated_at
        ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, NULL, NULL, NULL, ?, ?)
        """,
        (kind, parent_id, title[:500], year, season_number, episode_number, now, now),
    )
    if cursor.lastrowid is None:
        raise ImportFailure("SQLite não retornou o ID da mídia histórica criada.")
    return int(cursor.lastrowid)


def _add_identifiers(
    connection: sqlite3.Connection,
    media_id: int,
    identifiers: tuple[tuple[str, str], ...],
    *,
    media_index: MediaIndex,
    kind: str,
) -> int:
    now = _utc_now()
    added = 0
    for provider, external_id in identifiers:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO media_identifiers
                (media_item_id, provider, external_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (media_id, provider, external_id, now),
        )
        added += max(cursor.rowcount, 0)
    media_index.add(kind, media_id, identifiers)
    return added


def _show_for_episode(connection: sqlite3.Connection, episode_id: int) -> int | None:
    row = connection.execute(
        """
        SELECT show.id
        FROM media_items AS episode
        JOIN media_items AS season ON season.id = episode.parent_id AND season.kind = 'season'
        JOIN media_items AS show ON show.id = season.parent_id AND show.kind = 'show'
        WHERE episode.id = ? AND episode.kind = 'episode'
        """,
        (episode_id,),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _refresh_watch_state(connection: sqlite3.Connection, media_id: int, source_id: int) -> None:
    aggregate = connection.execute(
        """
        SELECT COUNT(*), MAX(watched_at)
        FROM watch_events
        WHERE media_item_id = ? AND source_id = ?
        """,
        (media_id, source_id),
    ).fetchone()
    count = int(aggregate[0])
    last_watched = str(aggregate[1])
    existing = connection.execute(
        """
        SELECT view_count, last_watched_at
        FROM watch_states
        WHERE media_item_id = ? AND source_id = ?
        """,
        (media_id, source_id),
    ).fetchone()
    now = _utc_now()
    if existing is None:
        connection.execute(
            """
            INSERT INTO watch_states (
                media_item_id, source_id, view_count, last_watched_at, completed,
                progress_ms, observed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, NULL, ?, ?, ?)
            """,
            (media_id, source_id, count, last_watched, now, now, now),
        )
        return
    current_count = int(existing[0])
    current_last = str(existing[1]) if existing[1] is not None else ""
    connection.execute(
        """
        UPDATE watch_states
        SET view_count = ?, last_watched_at = ?, completed = 1,
            observed_at = ?, updated_at = ?
        WHERE media_item_id = ? AND source_id = ?
        """,
        (
            max(current_count, count),
            max(current_last, last_watched),
            now,
            now,
            media_id,
            source_id,
        ),
    )


def _backup_database(connection: sqlite3.Connection, database: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = database.with_name(f"{database.name}.pre-trakt-{stamp}.bak")
    if backup.exists():
        raise ImportFailure(f"O backup já existe: {backup}")
    target = sqlite3.connect(backup)
    try:
        connection.backup(target)
    finally:
        target.close()
    return backup


def _validate_database(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise ImportFailure("O banco não passou no PRAGMA integrity_check.")
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = sorted(REQUIRED_TABLES - tables)
    if missing:
        raise ImportFailure(f"Banco incompatível; tabelas ausentes: {', '.join(missing)}")
    revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    if revision is None or str(revision[0]) not in SUPPORTED_DATABASE_REVISIONS:
        supported = ", ".join(sorted(SUPPORTED_DATABASE_REVISIONS))
        current = str(revision[0]) if revision is not None else "ausente"
        raise ImportFailure(f"Revisão do banco incompatível: {current}; esperada: {supported}.")


def _select_source(connection: sqlite3.Connection, requested: int | None) -> int:
    sources = connection.execute(
        "SELECT id, name, enabled FROM sources WHERE connector_type = 'plex' ORDER BY id"
    ).fetchall()
    if requested is not None:
        if not any(int(row[0]) == requested for row in sources):
            raise ImportFailure(f"Fonte Plex {requested} não encontrada.")
        return requested
    if len(sources) == 1:
        return int(sources[0][0])
    if not sources:
        raise ImportFailure("Nenhuma fonte Plex existe; sincronize o catálogo primeiro.")
    print("Fontes Plex disponíveis:")
    for row in sources:
        print(f"  {row[0]}: {row[1]} (enabled={bool(row[2])})")
    value = input("ID da fonte Plex para associar os eventos: ").strip()
    if not value.isdigit():
        raise ImportFailure("ID de fonte inválido.")
    selected = int(value)
    if not any(int(row[0]) == selected for row in sources):
        raise ImportFailure("A fonte selecionada não existe.")
    return selected


def _validate_paths(archive: Path, database: Path) -> None:
    if not archive.is_file() or archive.suffix.lower() != ".zip":
        raise ImportFailure(f"ZIP não encontrado ou inválido: {archive}")
    if not database.is_file():
        raise ImportFailure(f"Banco não encontrado: {database}")
    if not database.stat().st_size:
        raise ImportFailure("O banco está vazio.")


def _resolved_input(value: Path | None, prompt: str) -> Path:
    raw = str(value) if value is not None else input(prompt).strip()
    if not raw:
        raise ImportFailure("Caminho obrigatório não informado.")
    return Path(raw).expanduser().resolve()


def _confirm_docker_is_down(already_confirmed: bool) -> None:
    if already_confirmed:
        return
    answer = (
        input("O contêiner Docker do euvieouvi está completamente parado? [s/N]: ").strip().lower()
    )
    if answer not in {"s", "sim", "y", "yes"}:
        raise ImportFailure("Pare o contêiner antes de acessar diretamente o SQLite.")


def _timestamp(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    result = str(value).strip()
    return result or None


def _utc_now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("deve ser um número inteiro positivo") from error
    if result <= 0:
        raise argparse.ArgumentTypeError("deve ser maior que zero")
    return result


def _print_progress(message: str) -> None:
    print(message, flush=True)


def _emit(progress: Progress | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _batch_progress(
    progress: Progress | None,
    label: str,
    current: int,
    total: int,
    every: int,
) -> None:
    if progress is not None and (current == 1 or current == total or current % every == 0):
        percentage = (current / total * 100) if total else 100.0
        progress(f"  {label}: {current}/{total} ({percentage:.1f}%)")


def _write_report(path: Path, report: ImportReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(report), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _print_report(report: ImportReport) -> None:
    print("\nRelatório da importação Trakt")
    print(json.dumps(asdict(report), indent=2, ensure_ascii=False))
    if not report.committed:
        print("DRY-RUN: nenhuma alteração foi gravada no banco.")


if __name__ == "__main__":
    raise SystemExit(main())
