# euvieouvi v2

Aplicação self-hosted para registrar, sincronizar e consultar o histórico de filmes e
séries assistidos. O Plex será o primeiro conector.

## Estado atual

Fase 3 — Banco e repositories.

Esta fase contém somente:

- estrutura inicial do pacote;
- application factory do Flask;
- configuração por ambiente;
- ponto de inicialização de extensões;
- erros básicos;
- logging e request ID;
- testes e ferramentas de qualidade.

Também estão disponíveis:

- imagem baseada em Python 3.12 slim;
- execução sem root;
- Gunicorn com um worker `gthread`;
- Compose com volume nomeado;
- filesystem do contêiner somente leitura, exceto volume e `/tmp`;
- liveness e readiness iniciais;
- validação de configuração pelo entrypoint.

A camada persistente agora inclui:

- esquema SQLite aprovado e migração Alembic inicial;
- SQLAlchemy 2.x com repositories e unidade de trabalho explícita;
- foreign keys, WAL, timeout de concorrência e índices essenciais;
- distinção entre itens, eventos assistidos e estado agregado;
- deduplicação persistente de eventos;
- verificação da revisão do esquema no readiness;
- backup e restauração consistentes pela API oficial do SQLite.

Plex, motor de sincronização, API funcional e interface continuam reservados às fases
aprovadas correspondentes.

## Requisitos

- Python 3.12 ou superior compatível;
- Git;
- Docker Engine com Docker Compose v2 para a execução em contêiner.

## Ambiente de desenvolvimento

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Defina uma chave local e inicie o servidor de desenvolvimento:

```bash
export EUVIEOUVI_ENV=development
export EUVIEOUVI_SECRET_KEY=change-me-for-local-development
flask --app euvieouvi:create_app run
```

No Windows PowerShell:

```powershell
$env:EUVIEOUVI_ENV = "development"
$env:EUVIEOUVI_SECRET_KEY = "change-me-for-local-development"
flask --app euvieouvi:create_app run
```

## Qualidade

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

## Docker Compose

Crie a configuração local:

```bash
cp .env.example .env
```

Substitua `EUVIEOUVI_SECRET_KEY` por um valor longo e aleatório e execute:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f euvieouvi
```

A interface HTTP será publicada por padrão em `http://localhost:8000`. Nesta fase,
somente os endpoints operacionais estão disponíveis:

- `GET /health/live`
- `GET /health/ready`

O volume nomeado `euvieouvi_data` é mantido quando o contêiner é recriado.

## Migrações e backup

O entrypoint aplica automaticamente as migrações antes de iniciar o Gunicorn. Para
aplicá-las manualmente no ambiente de desenvolvimento:

```bash
flask --app euvieouvi.wsgi db upgrade
```

Crie um backup consistente com o serviço em execução:

```bash
python -m euvieouvi.database.backup backup instance/euvieouvi.db backups/euvieouvi.db
```

Restaure somente com o serviço parado:

```bash
python -m euvieouvi.database.backup restore backups/euvieouvi.db instance/euvieouvi.db
```

Para encerrar sem remover o volume:

```bash
docker compose down
```

## Documentação

A documentação aprovada está em [`docs/architecture`](docs/architecture). O índice é
[`euvieouvi-v2-indice-documentacao.md`](docs/architecture/euvieouvi-v2-indice-documentacao.md).
