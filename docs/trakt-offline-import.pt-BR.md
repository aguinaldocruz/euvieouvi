# Importação histórica offline do Trakt

Português do Brasil · [English](trakt-offline-import.md)

O utilitário `scripts/import_trakt_export.py` importa `watched-history-N.json` do ZIP completo do
Trakt. Executa fora do Docker, usa a biblioteca padrão e escreve diretamente no SQLite com o
serviço parado.

## Pré-condições

- Python 3.12 no host.
- Uma fonte Plex já armazenada no banco.
- ZIP original completo e caminho exato do `euvieouvi.db`.
- Contêiner da aplicação completamente parado.

O importador aceita as revisões Alembic `20260805_0009` e `20260811_0010`. Atualize a aplicação e
confira `flask db current`; schemas incompatíveis são recusados sem escrita.

A identidade usa Plex GUID, IMDb, TMDB, TVDB e Trakt, nunca título aproximado. Mídia histórica
ausente é criada sem falsa disponibilidade Plex. O Trakt não inclui capas; sync posterior ou
enriquecimento opcional por ID exato pode fornecê-las.

## Dry-run

```bash
docker compose stop euvieouvi
docker compose ps
sudo setpriv --reuid=10001 --regid=10001 --clear-groups \
  python3 scripts/import_trakt_export.py
python3 scripts/import_trakt_export.py --help
```

O modo interativo pede arquivo, banco, confirmação de Docker parado e, se necessário, fonte Plex.
Sem `--apply`, toda a transação é revertida.

```bash
sudo setpriv --reuid=10001 --regid=10001 --clear-groups \
  python3 scripts/import_trakt_export.py \
  --archive /home/docker/import/trakt-export.zip \
  --database /home/docker/euvieouvi/euvieouvi.db \
  --confirm-docker-down \
  --progress-every 1000 \
  --report /home/docker/euvieouvi/trakt-dry-run.json
```

Revise `invalid_events` e `ambiguous_events`; normalmente ambos devem ser zero. Ajuste progresso
com `--progress-every N` ou `--no-progress`.

## Aplicação definitiva

```bash
sudo setpriv --reuid=10001 --regid=10001 --clear-groups \
  python3 scripts/import_trakt_export.py \
  --archive /home/docker/import/trakt-export.zip \
  --database /home/docker/euvieouvi/euvieouvi.db \
  --apply \
  --report /home/docker/euvieouvi/trakt-import.json
```

O utilitário exige a confirmação literal `IMPORTAR` e cria antes um backup semelhante a
`euvieouvi.db.pre-trakt-20260804T200000000000Z.bak`. A importação é uma transação única. IDs usam
`trakt:<id>`, então repetir o mesmo export não duplica eventos.

## Retorno ao serviço

```bash
sudo ls -lah /home/docker/euvieouvi
docker compose up -d
./scripts/validate-deployment.sh
```

Mantenha o backup pré-importação até conferir o histórico na interface.
