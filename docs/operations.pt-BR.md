# Guia operacional

Português do Brasil · [English](operations.md)

## Implantação

Use Docker Engine e Docker Compose v2. Copie `.env.example` para `.env` e
`compose.yaml.sample` para `compose.yaml`. Defina `EUVIEOUVI_SECRET_KEY` longo e aleatório.

```bash
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 euvieouvi
./scripts/validate-deployment.sh
```

O exemplo publica a porta 8000, usa `euvieouvi_data` em `/app/instance`, executa com UID/GID
10001, remove capabilities, impede elevação e deixa a raiz somente leitura, exceto volume e `/tmp`.

Para bind mount, crie `compose.override.yaml` e permita escrita ao UID 10001:

```yaml
services:
  euvieouvi:
    volumes:
      - ./instance:/app/instance
```

## Configuração inicial

1. Configure Plex e/ou Jellyfin na interface.
2. Teste cada conexão.
3. Descubra e habilite bibliotecas.
4. Execute a primeira sincronização.
5. Opcionalmente configure agendamentos, metadados, backups e webhooks.

O Jellyfin exige API key administrativa e ID ou nome do usuário acompanhado. Webhooks de outro
usuário são ignorados.

## Proxy reverso e rede

O serviço não possui autenticação. Restrinja a porta 8000 ou use proxy HTTPS autenticado. Preserve
host/esquema originais ao gerar URLs de webhook. Aplique limites de tamanho e frequência,
especialmente em webhook e upload de restauração. Não armazene páginas/API privadas em cache.

## Atualização

1. Crie backup e copie-o para fora do volume.
2. Obtenha o código/imagem novo.
3. Reconstrua e recrie apenas o contêiner da aplicação.
4. Valide readiness, migrações, logs, catálogo, histórico e sync.

```bash
docker compose exec -T euvieouvi python -m euvieouvi.database.backup \
  backup /app/instance/euvieouvi.db /app/instance/backups/pre-upgrade.db
docker compose cp euvieouvi:/app/instance/backups/pre-upgrade.db ./pre-upgrade.db
docker compose build --pull
docker compose up -d --force-recreate
./scripts/validate-deployment.sh
docker compose logs --tail=100 euvieouvi
```

O startup aplica Alembic. Uma sync perdida vira `interrupted`; páginas confirmadas e checkpoints
são preservados.

## Backup e restauração

A interface cria, baixa, apaga, agenda, retém e restaura backups SQLite. Backup funciona com o
serviço ativo. Para restauração manual mais segura, pare o serviço principal:

```bash
docker compose stop euvieouvi
docker compose run --rm --no-deps euvieouvi python -m euvieouvi.database.backup \
  restore /app/instance/backups/pre-upgrade.db /app/instance/euvieouvi.db
docker compose up -d
./scripts/validate-deployment.sh
```

Mantenha cópia externa até conferir catálogo, histórico, credenciais, imagens e sync. Backups
contêm segredos e exigem a mesma proteção do banco ativo.

## Monitoramento e logs

```bash
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:8000/health/ready
docker compose logs -f --tail=200 euvieouvi
```

Logs usam UTC e request ID. Segredos são removidos defensivamente, mas revise antes de compartilhar.
Readiness falha se SQLite ou migração estiver incorreto; queda de conector não derruba o catálogo.

## Diagnóstico

- **Readiness falha após atualização:** veja logs e execute
  `docker compose exec euvieouvi flask --app euvieouvi.wsgi db current`.
- **Falha de dependência no sync:** teste a fonte, redescubra bibliotecas, confira usuário Jellyfin
  e erros por biblioteca. Um item Jellyfin malformado é ignorado com segurança.
- **Catálogo sem os dois ícones:** sincronize ambas as bibliotecas. IDs exatos unem registros;
  filmes sem IDs usam correspondência única e exata por título/ano.
- **Webhook não conclui:** confira URL secreta, evento, usuário Jellyfin e disponibilidade. Eventos
  anteriores ao catálogo são reconciliados na próxima sync.
- **Nada em reprodução:** habilite play/resume do Plex ou playback-start do Jellyfin.
- **SQLite busy:** evite filesystem de rede, mantenha um contêiner e ajuste o timeout dentro do limite.
- **Capa ausente:** confira fonte/permissões; capas externas exigem o provedor de metadados.

## Validação e encerramento

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
uvx pip-audit --requirement requirements.lock
```

O teste Plex real é opt-in em `tests/integration/test_real_plex.py`. Pare preservando dados com
`docker compose down`. Não use `docker compose down -v` sem intenção de apagar e backup validado.
