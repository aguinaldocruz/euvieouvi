# Importação histórica offline do Trakt

O utilitário `scripts/import_trakt_export.py` importa uma única vez os arquivos
`watched-history-N.json` do ZIP completo do Trakt. Ele roda fora do Docker, usa apenas a
biblioteca padrão do Python e não altera a interface web.

## Pré-condições

- Python 3.12 disponível no host;
- uma sincronização Plex completa e bem-sucedida já realizada;
- o ZIP original do export do Trakt acessível no host;
- o caminho real do `euvieouvi.db` conhecido;
- o serviço Docker completamente parado.

O importador acompanha o esquema atual do projeto e exige a revisão Alembic
`20260805_0006`. Execute a atualização da aplicação e confirme `flask db current` antes da
importação; revisões anteriores são recusadas sem modificar o banco.

O importador associa mídias por Plex GUID, IMDb, TMDB, TVDB e Trakt. Título não é usado
como identidade. Filmes e episódios históricos que não existem no catálogo atual são
criados com os dados do Trakt, sem uma referência falsa de disponibilidade no Plex.

## Execução recomendada

Pare o serviço e confirme que não existe contêiner ativo:

```bash
docker compose stop euvieouvi
docker compose ps
```

Execute como o mesmo UID usado pela imagem (`10001`) para preservar as permissões do
banco e dos arquivos auxiliares:

```bash
sudo setpriv --reuid=10001 --regid=10001 --clear-groups \
  python3 scripts/import_trakt_export.py
```

Consulte a sintaxe completa e os exemplos incorporados:

```bash
python3 scripts/import_trakt_export.py --help
```

O programa perguntará:

1. caminho absoluto do ZIP do Trakt;
2. caminho absoluto do `euvieouvi.db`;
3. confirmação de que o contêiner está parado;
4. fonte Plex, somente se houver mais de uma.

Sem `--apply`, a execução é sempre um dry-run e termina com rollback integral.

Exemplo não interativo de dry-run:

```bash
sudo setpriv --reuid=10001 --regid=10001 --clear-groups \
  python3 scripts/import_trakt_export.py \
  --archive /home/docker/import/trakt-export.zip \
  --database /home/docker/euvieouvi/euvieouvi.db \
  --confirm-docker-down \
  --progress-every 1000 \
  --report /home/docker/euvieouvi/trakt-dry-run.json
```

Revise os contadores `invalid_events` e `ambiguous_events`. O esperado para o export
validado é zero em ambos.

Durante a execução, o programa informa cada `watched-history-N.json` lido e as quatro
fases da carga. Associação, eventos e estados exibem contagem, total e percentual a cada
1.000 registros. Ajuste com `--progress-every N` ou oculte com `--no-progress`.

## Aplicação definitiva

Execute novamente com `--apply`:

```bash
sudo setpriv --reuid=10001 --regid=10001 --clear-groups \
  python3 scripts/import_trakt_export.py \
  --archive /home/docker/import/trakt-export.zip \
  --database /home/docker/euvieouvi/euvieouvi.db \
  --apply \
  --report /home/docker/euvieouvi/trakt-import.json
```

O programa pede a confirmação literal `IMPORTAR`. Antes de abrir a transação de escrita,
ele cria no mesmo diretório um backup com nome semelhante a:

```text
euvieouvi.db.pre-trakt-20260804T200000000000Z.bak
```

Toda a carga é uma única transação. Uma exceção causa rollback. IDs de evento são salvos
como `trakt:<id>`, portanto uma nova execução do mesmo export não duplica o histórico.

## Retorno ao serviço

Confira as permissões e reinicie:

```bash
sudo ls -lah /home/docker/euvieouvi
docker compose up -d
./scripts/validate-deployment.sh
```

Mantenha o backup automático até conferir o histórico na interface.
