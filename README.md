# euvieouvi v2

Aplicação self-hosted para registrar, sincronizar e consultar o inventário e o histórico
de filmes, séries e músicas do Plex e do Jellyfin.

## Estado atual

Fase 8 — Integração e endurecimento.

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

Os conectores Plex e Jellyfin agora incluem:

- DTOs neutros e imutáveis, sem dependência do banco;
- interface `MediaConnector` tipada;
- autenticação por cabeçalho, sem token em URL ou log;
- timeouts, retries limitados e erros classificados;
- redirecionamentos limitados à mesma origem configurada;
- descoberta explícita de bibliotecas suportadas e rejeitadas;
- paginação defensiva de filmes, episódios, faixas e histórico concluído;
- leitura encapsulada de respostas XML e JSON;
- mapeamento de filmes, episódios e música com suas hierarquias e identificadores;
- fixtures sanitizadas e testes de contrato sem acesso à rede.

O motor de sincronização agora inclui:

- execução local exclusiva, com bloqueio transacional global;
- fotografia imutável das bibliotecas selecionadas em cada execução;
- sincronização paginada de filmes, episódios, faixas e somente conclusões do histórico;
- persistência idempotente de mídia, hierarquia, identificadores e estado assistido;
- savepoints por item e confirmação por página;
- checkpoints opacos para retomada a partir da última página confirmada;
- contadores, erros persistidos e estados de execução auditáveis;
- cancelamento cooperativo nos limites de página;
- reconciliação de execuções órfãs durante a inicialização;
- indisponibilidade de itens ausentes somente após varredura completa bem-sucedida;
- descoberta transacional que preserva a seleção das bibliotecas;
- regressão da biblioteca Futurama coberta por teste: 161 episódios e 144 assistidos.

A API REST agora inclui:

- contrato OpenAPI 3.1 versionado em `openapi.yaml`;
- endpoints `/api/v1` para fontes e bibliotecas;
- teste de conexão e descoberta segura de bibliotecas;
- início assíncrono, acompanhamento e cancelamento de sincronizações;
- consultas paginadas de catálogo, eventos e estado agregado;
- resumo operacional para o dashboard;
- cursores opacos, validação estrita e erros JSON uniformes;
- segredo Plex somente em escrita, nunca devolvido nas respostas;
- CORS desabilitado e limite conservador de corpo por padrão.

A interface web agora inclui:

- páginas Jinja responsivas em português do Brasil;
- Bootstrap e HTMX locais, com nomes de assets baseados em hash;
- configuração segura do Plex e Jellyfin;
- descoberta e seleção de bibliotecas com fallback tradicional;
- dashboard, atividade recente e sincronização em segundo plano;
- polling HTMX somente enquanto houver execução ativa;
- catálogo visual paginado com capas, disponibilidade e estado de reprodução;
- filtros, ordenação ascendente/descendente e detalhes hierárquicos de vídeo e música;
- histórico completo paginado apenas de reproduções concluídas, preservando repetições e datas;
- indicadores condicionais de disponibilidade no Plex e Jellyfin e total global de conclusões;
- proteção CSRF em todos os formulários de escrita;
- cabeçalhos de segurança, navegação por teclado e layout móvel.

A sincronização diária pode ser ativada e ter seu horário definido pela interface, sem
remover o acionamento manual. A aplicação não possui autenticação interna e deve permanecer
em rede confiável ou atrás de proxy reverso.

Os webhooks opcionais reduzem o intervalo entre uma conclusão e seu registro. Em
`Configurações → Webhooks`, copie a URL secreta para o Plex ou para o plugin oficial Webhook
do Jellyfin. O Plex registra apenas `media.scrobble`; o Jellyfin registra apenas
`PlaybackStop` com `PlayedToCompletion=true`. Eventos parciais e estado “reproduzindo agora”
não são armazenados nem exibidos.

No Jellyfin, crie uma API key administrativa e informe também o ID do usuário acompanhado.
A sincronização normal utiliza `PlayCount` e `LastPlayedDate`: mantém o contador informado,
mas cria evento histórico somente quando existe uma data real, sem inventar datas anteriores.

O enriquecimento opcional por TMDB e MusicBrainz fica desativado por padrão. Quando ativado,
usa somente identificadores exatos já fornecidos pelo Plex, preenche apenas campos ausentes e
pode ser executado manualmente ou após uma sincronização concluída. Quando o Plex não fornece
uma capa, a aplicação pode usar a imagem correspondente do TMDB ou Cover Art Archive; essas
imagens passam pela mesma camada de cache local e são substituídas se uma capa Plex surgir.

O endurecimento da primeira versão agora inclui:

- redaction defensiva de token, segredo e Authorization nos logs;
- testes integrados de interrupção, reconciliação, backup e restauração;
- recriação da aplicação preservando o banco persistente;
- teste representativo com 3.000 filmes e paginação;
- teste opt-in com biblioteca Plex real, sem credencial versionada;
- auditoria reproduzível de dependências;
- script de smoke test para contêiner, usuário não root, volume e healthchecks;
- guia operacional e relatório explícito dos portões externos.

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

A interface HTTP será publicada por padrão em `http://localhost:8000`. A interface web
estará disponível em `/` e os endpoints operacionais permanecem disponíveis:

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

O guia prático de instalação, atualização, backup, restauração e validação está em
[`docs/operations.md`](docs/operations.md).

### Commit Inicial para Github
