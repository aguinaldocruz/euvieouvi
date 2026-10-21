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
- Fila durável para atualizações instantâneas, com idempotência, tentativas automáticas,
  recuperação após reinício, drenagem periódica/por atividade e job manual.
- Jobs manuais/diários independentes usam fila no banco, lease, heartbeat, tentativas limitadas,
  cancelamento persistente, recuperação por checkpoints após reinício, progresso ao vivo,
  resultado e logs rotativos.
- Capas existentes no Plex/Jellyfin são servidas por proxy sob demanda sem expor tokens. Episódios e
  faixas herdam capas disponíveis da série/temporada ou álbum/artista sem duplicar arquivos. Quando a
  imagem não existe ou retorna 404, o job usa IDs exatos TMDB/IMDb/TheTVDB/MusicBrainz para buscar e
  armazenar um fallback seguro do TMDB ou Cover Art Archive; nunca tenta adivinhar pelo título.
- Enriquecimento opcional via TMDB, MusicBrainz e Cover Art Archive por IDs exatos.
- Cache local de imagens e preservação histórica de mídias indisponíveis.
- Idioma inglês/português brasileiro persistente (inglês por padrão), temas claro/escuro,
  interface responsiva renderizada no servidor e API REST.
- Backup SQLite manual/agendado, restauração, download e retenção.
- O Docker inclui SQLite 3.53.4 com a correção upstream do reset de WAL; o startup em produção
  rejeita runtimes antigos afetados. Amostras e alertas operacionais são mantidos por 30 dias.
- Importação offline do histórico completo exportado pelo Trakt.

## Arquitetura

O euvieouvi é um monólito modular Flask com Jinja, HTMX, Bootstrap, SQLAlchemy 2, Alembic,
SQLite e Gunicorn. Os conectores convertem respostas externas em DTOs neutros; serviços de
sincronização reconciliam e persistem os dados por repositories e unidade de trabalho. Apenas
uma sincronização pode permanecer ativa.

```text
data/
├── euvieouvi.db
├── backups/
├── images/
└── job-logs/
```

Consulte a [documentação do projeto](docs/README.pt-BR.md) para componentes, comportamento dos
dados, API, limites de segurança e índice completo.

## Requisitos

- Docker Engine e Docker Compose v2 para implantação recomendada; ou
- Python 3.12+, Git e ambiente virtual para desenvolvimento.

O Plex exige URL e autorização pela página hospedada de login do Plex; o euvieouvi nunca recebe a
senha Plex. O Jellyfin usa o fluxo de dispositivo Quick Connect: aprove o código exibido em um
cliente Jellyfin já autenticado. O euvieouvi recebe um token do usuário, nunca a senha da conta.
Configurações antigas com API key administrativa continuam funcionando até serem reconectadas.

## Início rápido com Docker Compose

Copie o exemplo Compose e inicie; nenhum `.env` ou segredo da aplicação é necessário:

```bash
cp compose.yaml.sample compose.yaml
docker compose up -d --build
docker compose ps
docker compose logs -f euvieouvi
```

Abra <http://localhost:8000/setup>, salve os parâmetros operacionais, configure Plex e/ou
Jellyfin, descubra as bibliotecas, habilite as desejadas e inicie uma sincronização.

O volume nomeado `euvieouvi_data` sobrevive à recriação do contêiner. O entrypoint valida a
configuração, aplica migrações e reconcilia sincronizações interrompidas antes do Gunicorn.

Endpoints operacionais:

- `GET /health/live` — vida do processo, sem detalhes de dependências.
- `GET /health/ready` — conectividade do banco e estado das migrações.
- `GET /health/metrics` — telemetria sem segredos de filas, worker, conectores, armazenamento,
  alertas e histórico de 24 horas.
- `/api/v1` — API REST descrita por [openapi.yaml](openapi.yaml).

Para atualização, backup, restauração, proxy reverso e diagnóstico, consulte o
[guia operacional](docs/operations.pt-BR.md).

## Configuração

Nível de log, fuso horário, quantidade de threads HTTP e timeout do SQLite ficam no banco e são
editados em `/setup`. Conectores, agendamentos, retenções, metadados e aparência também são
configurados pela interface. A porta interna Docker (`8000`) e o volume `/data` permanecem no
Compose porque precisam existir antes da abertura do SQLite.

Credenciais dos conectores e tokens de metadados/webhook são criptografados em repouso. A
aplicação cria `/data/.euvieouvi-master.key` com modo `0600`; preserve esse arquivo com os backups
do banco. Segredos continuam somente escrita na API e removidos defensivamente dos logs.

## Desenvolvimento

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
export EUVIEOUVI_ENV=development
flask --app euvieouvi:create_app run
```

Windows PowerShell:

```powershell
$env:EUVIEOUVI_ENV = "development"
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
próxima sincronização. Progresso recebido enquanto uma atualização anterior está sendo processada
é preservado como próximo item da fila durável, sem sobrescrita. A página mantém a quantidade
configurada de conclusões recentes.

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
