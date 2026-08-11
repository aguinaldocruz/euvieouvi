# euvieouvi

Português do Brasil · [English](README.md)

Catálogo de mídia e histórico de reprodução self-hosted para Plex e Jellyfin. Sincroniza filmes,
séries e músicas em um banco SQLite local, preservando o histórico de conclusões e registros de
mídias que posteriormente deixam de existir no servidor.

> [!IMPORTANT]
> O euvieouvi não possui autenticação interna. Execute-o em rede confiável ou atrás de proxy
> reverso autenticado. Nunca publique URLs de webhook, banco de dados, backups ou tokens.

## Funcionalidades

- Fontes Plex e Jellyfin, teste de conexão e descoberta de bibliotecas.
- Filmes, séries, temporadas, episódios, artistas, álbuns e faixas.
- Sincronização idempotente e paginada, com checkpoints e cancelamento seguro.
- Disponibilidade combinada no Plex/Jellyfin nas entradas do catálogo.
- Histórico concluído com servidor de origem e forma de aquisição (`webhook` ou
  `sincronização`).
- Webhooks Plex/Jellyfin, retenção de eventos recentes e mídia em reprodução.
- Agendamento diário compartilhado ou separado por fonte.
- Enriquecimento opcional via TMDB, MusicBrainz e Cover Art Archive por IDs exatos.
- Cache local de imagens e preservação histórica de mídias indisponíveis.
- Temas claro/escuro, interface responsiva renderizada no servidor e API REST.
- Backup SQLite manual/agendado, restauração, download e retenção.
- Importação offline do histórico completo exportado pelo Trakt.

## Arquitetura

O euvieouvi é um monólito modular Flask com Jinja, HTMX, Bootstrap, SQLAlchemy 2, Alembic,
SQLite e Gunicorn. Os conectores convertem respostas externas em DTOs neutros; serviços de
sincronização reconciliam e persistem os dados por repositories e unidade de trabalho. Apenas
uma sincronização pode permanecer ativa.

```text
instance/
├── euvieouvi.db
├── backups/
└── images/
```

Consulte a [documentação do projeto](docs/README.pt-BR.md) para componentes, comportamento dos
dados, API, limites de segurança e índice completo.

## Requisitos

- Docker Engine e Docker Compose v2 para implantação recomendada; ou
- Python 3.12+, Git e ambiente virtual para desenvolvimento.

O Plex exige URL e token. O Jellyfin exige URL, API key e ID ou nome do usuário cujo estado de
reprodução será sincronizado.

## Início rápido com Docker Compose

```bash
cp .env.example .env
cp compose.yaml.sample compose.yaml
```

Defina um `EUVIEOUVI_SECRET_KEY` longo e aleatório em `.env` e inicie:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f euvieouvi
```

Abra <http://localhost:8000>, configure Plex e/ou Jellyfin, descubra as bibliotecas, habilite as
desejadas e inicie uma sincronização.

O volume nomeado `euvieouvi_data` sobrevive à recriação do contêiner. O entrypoint valida a
configuração, aplica migrações e reconcilia sincronizações interrompidas antes do Gunicorn.

Endpoints operacionais:

- `GET /health/live` — vida do processo, sem detalhes de dependências.
- `GET /health/ready` — conectividade do banco e estado das migrações.
- `/api/v1` — API REST descrita por [openapi.yaml](openapi.yaml).

Para atualização, backup, restauração, proxy reverso e diagnóstico, consulte o
[guia operacional](docs/operations.pt-BR.md).

## Configuração

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `EUVIEOUVI_ENV` | `production` | `development`, `production` ou `testing`. |
| `EUVIEOUVI_SECRET_KEY` | nenhum | Segredo obrigatório de sessão/Flask; use valor longo e aleatório. |
| `EUVIEOUVI_HOST` | `0.0.0.0` | Endereço de escuta. |
| `EUVIEOUVI_PORT` | `8000` | Porta, 1–65535. |
| `EUVIEOUVI_INSTANCE_PATH` | `./instance` | Diretório do banco, imagens e backups. |
| `EUVIEOUVI_DATABASE_URI` | SQLite no diretório instance | URI SQLite; outros bancos não são suportados. |
| `EUVIEOUVI_LOG_LEVEL` | `INFO` | `CRITICAL`, `ERROR`, `WARNING`, `INFO` ou `DEBUG`. |
| `EUVIEOUVI_TIMEZONE` | `America/Sao_Paulo` | Fuso IANA usado na exibição e agendamentos. |
| `EUVIEOUVI_GUNICORN_THREADS` | `4` | Threads do Gunicorn, 1–32. |
| `EUVIEOUVI_SQLITE_BUSY_TIMEOUT_MS` | `5000` | Timeout SQLite, 1–60000 ms. |

Credenciais, agendamentos, tokens de webhook, retenções e metadados são configurados pela
interface. Segredos são somente escrita na API e removidos defensivamente dos logs.

## Desenvolvimento

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
export EUVIEOUVI_ENV=development
export EUVIEOUVI_SECRET_KEY=apenas-desenvolvimento-local
flask --app euvieouvi:create_app run
```

Windows PowerShell:

```powershell
$env:EUVIEOUVI_ENV = "development"
$env:EUVIEOUVI_SECRET_KEY = "apenas-desenvolvimento-local"
flask --app euvieouvi:create_app run
```

Portões de qualidade:

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

Crie migrações com Flask-Migrate, revise as operações geradas e teste upgrade/downgrade em banco
descartável. A produção executa `flask --app euvieouvi.wsgi db upgrade` automaticamente.

## Webhooks

A página de Webhooks gera URLs secretas para cada conector.

- Plex: configure em **Settings → Webhooks**. Início/retomada alimenta a visualização atual;
  `media.scrobble` cria uma conclusão.
- Jellyfin: configure o plugin oficial Webhook com a URL gerada e eventos de início/parada. Uma
  parada com `PlayedToCompletion=true` cria uma conclusão.

Conclusões recebidas antes de a mídia existir no catálogo são preservadas e reconciliadas na
próxima sincronização. A página mantém a quantidade configurada de conclusões recentes.

## API

A API cobre fontes, bibliotecas, sincronizações, mídia, eventos, estados e resumo do dashboard.
Requisições e respostas JSON ficam em `/api/v1`; cursores e erros estão em
[openapi.yaml](openapi.yaml). CORS permanece desativado e a API não adiciona autenticação.

## Dados e privacidade

O banco contém metadados, histórico, credenciais, tokens de webhook e configurações. Backups
contêm os mesmos dados sensíveis. Capas são armazenadas no diretório instance. Nenhuma nuvem é
obrigatória, mas o enriquecimento opcional acessa TMDB, MusicBrainz e Cover Art Archive.

## Contribuição e segurança

Leia [CONTRIBUTING.pt-BR.md](CONTRIBUTING.pt-BR.md) antes de enviar alterações. Vulnerabilidades
devem seguir [SECURITY.pt-BR.md](SECURITY.pt-BR.md), nunca uma issue pública.

O repositório não contém licença. Sem declaração do proprietário, não presuma permissão para
redistribuir cópias modificadas ou não modificadas.
