# Operação do euvieouvi v2

Este guia cobre instalação, validação, atualização, backup, restauração e os portões
externos da primeira versão. A aplicação não possui autenticação interna: publique-a
somente em rede confiável ou atrás de proxy reverso com controle de acesso.

## 1. Instalação com Docker Compose

Requisitos: Docker Engine e Docker Compose v2.

```bash
cp .env.example .env
```

Edite `.env` e substitua `EUVIEOUVI_SECRET_KEY` por um valor longo e aleatório. Não
configure o token Plex no arquivo: ele será salvo pela interface.

```bash
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 euvieouvi
```

Abra `http://IP_DO_SERVIDOR:8000`. O primeiro acesso orientará a configuração do Plex e
das bibliotecas.

Validação automatizada da implantação:

```bash
./scripts/validate-deployment.sh
```

O script confirma processo não root, volume gravável, liveness, readiness e ausência de
falha no healthcheck. A indisponibilidade do Plex não deve derrubar consultas locais.

## 2. Volume persistente

O Compose padrão usa o volume nomeado `euvieouvi_data` em `/app/instance`. Recriar o
contêiner não remove esse volume.

Para usar bind mount, crie `compose.override.yaml`:

```yaml
services:
  euvieouvi:
    volumes:
      - ./instance:/app/instance
```

Garanta que o diretório seja gravável pelo UID/GID `10001`. Não versione seu conteúdo.

## 3. Backup consistente

O backup usa a API oficial do SQLite e pode ser criado com a aplicação em execução:

```bash
docker compose exec -T euvieouvi python -m euvieouvi.database.backup \
  backup /app/instance/euvieouvi.db /app/instance/backups/euvieouvi-antes-update.db
```

Copie o resultado para fora do volume:

```bash
docker compose cp \
  euvieouvi:/app/instance/backups/euvieouvi-antes-update.db \
  ./euvieouvi-antes-update.db
```

O backup contém histórico e token Plex e deve receber a mesma proteção do banco.

## 4. Restauração

A restauração exige o serviço parado:

```bash
docker compose stop euvieouvi
docker compose run --rm --no-deps euvieouvi python -m euvieouvi.database.backup \
  restore /app/instance/backups/euvieouvi-antes-update.db \
  /app/instance/euvieouvi.db
docker compose up -d
./scripts/validate-deployment.sh
```

Mantenha o backup anterior até confirmar catálogo, histórico e sincronização.

## 5. Atualização preservando dados

1. Crie e retire do volume um backup consistente.
2. Obtenha o novo código ou imagem.
3. Construa a imagem atualizada.
4. Recrie apenas o contêiner, preservando o volume.
5. Verifique healthchecks e logs.

```bash
docker compose build --pull
docker compose up -d --force-recreate
./scripts/validate-deployment.sh
docker compose logs --tail=100 euvieouvi
```

O entrypoint valida configuração, aplica migrações e reconcilia execuções órfãs antes do
Gunicorn. Uma execução perdida durante reinício fica `interrupted`; dados já confirmados e
checkpoints permanecem preservados.

## 6. Teste controlado com Plex real

Use uma biblioteca pequena e explícita. As variáveis existem apenas no processo do teste;
não as salve no repositório nem no relatório.

```bash
export EUVIEOUVI_RUN_REAL_PLEX=1
export EUVIEOUVI_TEST_PLEX_URL=http://IP_DO_PLEX:32400
export EUVIEOUVI_TEST_PLEX_TOKEN=seu-token
export EUVIEOUVI_TEST_PLEX_LIBRARY_ID=id-da-biblioteca-pequena
pytest -m real_plex tests/integration/test_real_plex.py
unset EUVIEOUVI_TEST_PLEX_TOKEN
```

O teste valida autenticação, descoberta, sincronização inicial e repetição idempotente. A
validação operacional final deve ainda interromper um contêiner durante uma sincronização,
reiniciá-lo e confirmar `interrupted` e o último checkpoint confirmado.

## 7. Teste de volume e dependências

O teste automatizado cria 3.000 filmes locais e confirma paginação e consultas:

```bash
pytest -m volume tests/integration/test_hardening.py
```

Auditoria reproduzível das versões fixadas:

```bash
uvx pip-audit --requirement requirements.lock
```

Na validação de 4 de agosto de 2026, nenhuma vulnerabilidade conhecida foi encontrada.
Repita a auditoria antes de cada release e atualização de dependências.

## 8. Logs e diagnóstico

```bash
docker compose logs -f --tail=200 euvieouvi
```

Os logs vão para stdout/stderr e incluem horário UTC, nível, componente e request ID. O
filtro defensivo remove token, segredo e Authorization de mensagens. Não envie banco,
backup ou logs completos a terceiros sem revisão.

## 9. Encerramento e remoção

Parar preservando dados:

```bash
docker compose down
```

Não use `docker compose down -v` a menos que queira remover definitivamente o volume após
confirmar um backup externo válido.

