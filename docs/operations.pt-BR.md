# Guia operacional

Português do Brasil · [English](operations.md)

## Implantação

Use Docker Engine e Docker Compose v2. Copie `compose.yaml.sample` para `compose.yaml`. O Docker
não exige `.env`; os parâmetros operacionais da aplicação ficam persistidos no SQLite.

```bash
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 euvieouvi
./scripts/validate-deployment.sh
```

O exemplo publica a porta 8000, usa `euvieouvi_data` em `/data`, executa com UID/GID
10001, remove capabilities, impede elevação e deixa a raiz somente leitura, exceto volume e `/tmp`.

A imagem compila SQLite 3.53.4 a partir do arquivo oficial, confere o SHA3-256 antes da compilação
e valida o runtime Python durante o build. O startup de produção recusa versões SQLite afetadas
pela condição de corrida no reset do WAL. Confira o runtime ativo com:

```bash
docker compose exec -T euvieouvi python -c 'import sqlite3; print(sqlite3.sqlite_version)'
```

A aplicação cria `/data/.euvieouvi-master.key` no primeiro início com modo `0600`. Credenciais do
Plex, Jellyfin, TMDB e webhooks são criptografadas em repouso com essa chave. Preserve-a com
segurança: um banco copiado não recupera suas credenciais sem a chave correspondente. O
`EUVIEOUVI_MASTER_KEY` externo continua opcional para instalações avançadas com gerenciador de
segredos, mas precisa estar disponível antes da leitura do banco.

Para bind mount, crie `compose.override.yaml` e permita escrita ao UID 10001:

```yaml
services:
  euvieouvi:
    volumes:
      - ./data:/data
```

## Configuração inicial

1. Abra `/setup` e salve nível de log, fuso, threads HTTP e timeout SQLite.
2. Salve a URL Plex e use **Entrar com o Plex**. O login ocorre no Plex; a aplicação nunca recebe
   nem armazena a senha da conta. No Jellyfin, salve a URL, inicie **Quick Connect** e aprove o código
   exibido em um cliente já autenticado.
3. Teste cada conexão.
4. Descubra e habilite bibliotecas.
5. Execute a primeira sincronização e configure agendamentos, metadados, backups e webhooks.

O Jellyfin retorna pelo Quick Connect o token e a identidade do usuário; a senha nunca é enviada ao
euvieouvi. O Quick Connect precisa estar habilitado no servidor. Fontes antigas com API key
administrativa continuam compatíveis até serem reconectadas. Webhooks de outro usuário são ignorados.

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
  backup /data/euvieouvi.db /data/backups/pre-upgrade.db
docker compose cp euvieouvi:/data/backups/pre-upgrade.db ./pre-upgrade.db
docker compose cp euvieouvi:/data/.euvieouvi-master.key ./euvieouvi-master.key
docker compose build --pull
docker compose up -d --force-recreate
./scripts/validate-deployment.sh
docker compose logs --tail=100 euvieouvi
```

Guarde a chave copiada com controle de acesso equivalente ao de uma senha. O startup aplica
Alembic e criptografa de forma idempotente credenciais legadas em texto puro antes de atender
requisições. Uma sync de baixo nível perdida vira `interrupted`, enquanto seu job
operacional é reenfileirado e inicia uma nova sync a partir do último checkpoint confirmado. Jobs
que aguardavam continuam na fila. O worker reivindica somente um job global, renova um lease de 45
segundos, grava heartbeat e repete falhas do worker/dispatch com backoff limitado (três tentativas
por padrão, configurável de 1 a 10 na página Jobs). O cancelamento persistente sobrevive a
reinícios. A reconciliação de startup também reconstrói a
projeção persistente do catálogo
quando ela estiver ausente ou desatualizada. Essa projeção guarda grupos canônicos Plex/Jellyfin,
disponibilidade, resumos de reprodução e progresso de séries para que cada página não releia todo o
histórico. Escritas em fontes, identidades e reproduções a marcam transacionalmente como
desatualizada; a próxima leitura do catálogo também a repara se o startup ou um job ainda não o fez.
Escritas de reprodução registram os IDs afetados e atualizam apenas sua hierarquia; alterações de
identidade/fonte solicitam rebuild completo porque podem mudar a associação canônica.

## Backup e restauração

A interface cria, baixa, apaga, agenda, retém e restaura backups SQLite. Backup funciona com o
serviço ativo. Para restauração manual mais segura, pare o serviço principal:

```bash
docker compose stop euvieouvi
docker compose run --rm --no-deps euvieouvi python -m euvieouvi.database.backup \
  restore /data/backups/pre-upgrade.db /data/euvieouvi.db
docker compose up -d
./scripts/validate-deployment.sh
```

Mantenha cópia externa até conferir catálogo, histórico, credenciais, imagens e sync. Backups
contêm segredos e exigem a mesma proteção do banco ativo.

## Monitoramento e logs

```bash
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:8000/health/ready
curl --fail http://localhost:8000/health/metrics
docker compose logs -f --tail=200 euvieouvi
```

Logs usam UTC e request ID. Segredos são removidos defensivamente, inclusive o sufixo secreto das
URLs de webhook Plex/Jellyfin no access log, mas revise antes de compartilhar. Readiness falha se
SQLite ou migração estiver incorreto; queda de conector não derruba o catálogo.

Cada job também grava logs em `/data/job-logs`; a quantidade mantida por job é configurada na
página **Jobs**. Para migrar uma instalação antiga, pare o contêiner, copie todo o conteúdo do
volume antigo (`/app/instance`) para o novo volume `/data`, preserve UID/GID 10001 e só então
recrie o serviço. Não copie apenas o `.db`: preserve também `backups/` e `images/`.

As páginas inicial e Jobs recebem estado ao vivo por server-sent events (SSE), com reconexão
automática e polling de contingência. O proxy reverso não deve armazenar `/jobs/events` em buffer;
a resposta já envia `X-Accel-Buffering: no` e keepalives periódicos. A página Jobs também mostra
idade das filas, heartbeat do worker, latência/falhas dos conectores e operações recentes com pelo
menos um segundo. Após cinco falhas transitórias consecutivas, o processo pausa novas chamadas à
fonte por 60 segundos; autenticação, configuração, resposta malformada e item ausente não abrem o
circuito. A proteção é local ao processo e reinicia junto com ele.

O scheduler grava no máximo uma amostra operacional a cada cinco minutos e remove amostras com
mais de 30 dias. A interface e `/health/metrics` resumem as últimas 24 horas. Há alertas quando o
heartbeat de um job passa de 45 segundos (crítico após 90), um job ou atualização instantânea
espera mais de cinco minutos, um circuito de conector está aberto ou há falhas consecutivas de
conector. Tamanhos do banco e WAL também são acompanhados. São resumos pequenos, não traces de
requisições.

Use **Baixar diagnóstico** na página Jobs para gerar um ZIP limitado com contagens operacionais,
até 288 amostras operacionais recentes, jobs/tarefas/erros recentes e o final de no máximo 20 logs
de jobs. Credenciais e formatos de token
conhecidos são ocultados; banco, valores das configurações, payloads das tarefas e histórico
completo de logs não são incluídos. Mesmo assim, revise antes de compartilhar: títulos, nomes de
fontes, endereços internos e identificadores de mídia podem ser privados.

A ordem diária recomendada é **Sincronizar Plex → Assistidos Plex para Jellyfin → Assistidos
Jellyfin para Plex → Sincronizar Jellyfin** (padrões 03:00, 03:15, 03:30 e 03:45). O dispatcher
persistente prioriza essa ordem quando várias linhas estão prontas e impede jobs operacionais
concorrentes. Horários personalizados existentes são preservados no upgrade.

O job **Otimizar dados** gira históricos de jobs/sincronizações, remove imagens órfãs, executa
`PRAGMA optimize` e compacta o SQLite com `VACUUM`. É automatizado e seguro para iniciar pela
interface, mas o `VACUUM` obtém brevemente bloqueio exclusivo e exige espaço temporário. Os arquivos
temporários do SQLite ficam em `/data/sqlite-tmp`, não no `/tmp` limitado do contêiner; mantenha ao
menos duas vezes o tamanho do banco livre e agende fora de reprodução/sincronização ativa.

Atualizações instantâneas de webhook são confirmadas em `async_tasks` antes do envio externo. A fila
é drenada a cada evento, periodicamente, durante atividade web ou pelo job **Processar fila de
atualizações**. Falhas permanecem com backoff exponencial, progresso mais novo recebido durante o
processamento vira um item sucessor e reinicializações devolvem trabalhos interrompidos a pendente.
Uma conclusão na fila substitui um progresso parcial anterior da mesma mídia. Mapeamentos de origem
ausentes são recuperados por consulta pontual; referências remotas antigas que retornam 404 são
desativadas e a próxima referência válida é tentada. Após 12 falhas consecutivas, o item é isolado
da drenagem automática; o job manual da fila reinicia e tenta novamente itens isolados, impedindo
que um evento defeituoso faça todas as execuções falharem indefinidamente.

Os jobs direcionais de assistidos criam uma baseline completa e depois comparam apenas estados,
históricos, referências e identificadores alterados. Uma reconciliação completa é forçada pelo menos
a cada sete dias. Alterar/desativar o Plex ou Jellyfin limpa esses checkpoints. As APIs remotas ainda
exigem uma escrita por diferença real porque não oferecem atualização em lote compatível; o estado
local é confirmado em lotes.

Imagens existentes no Plex/Jellyfin são servidas por proxy sob demanda sem expor credenciais nem
manter cópia local duplicada. Episódio ou faixa sem arte própria herda a primeira imagem disponível
da série/temporada ou álbum/artista; entradas canônicas equivalentes também são consideradas. Quando
uma imagem está ausente ou retorna 404, a aplicação enfileira o
job de imagens; ele resolve apenas IDs exatos TMDB/IMDb/TheTVDB/MusicBrainz e armazena capas do TMDB
ou Cover Art Archive. Pesquisas ambíguas por título não são executadas.

Quando o Jellyfin não retorna IDs estruturados, tokens oficiais em nomes de filme/série
(como `[tmdb-15119]`, `[tvdbid-254049]` ou `[imdbid-tt0281433]`) fornecem identidade exata nas
sincronizações e consultas pontuais de eventos. IDs estruturados têm prioridade. O job de
reconciliação também recupera esses IDs de títulos antigos, limpa o título exibido, registra cada
item e une duplicatas confirmadas entre fontes com seu histórico. Tokens conflitantes e fontes
ambíguas ficam para revisão. Após confirmar a série, coordenadas únicas de episódio recuperam
filhos sem identificadores.

## Validação de carga e soak somente leitura

O repositório inclui um teste reproduzível de readiness, métricas, estado dos jobs e SSE em modo
único. Ele não enfileira jobs nem altera dados da aplicação:

```bash
python scripts/operational_load_test.py \
  --base-url http://localhost:8000 --duration 60 --concurrency 6
```

Para um soak mais longo, use `--duration 1800`. O comando retorna código diferente de zero se os
erros passarem de 1% ou a latência p95 passar de 1000 ms; ajuste `--max-error-rate` e
`--max-p95-ms` apenas quando o hardware ou proxy tiver um objetivo de serviço diferente e explícito.

## Diagnóstico

- **Readiness falha após atualização:** veja logs e execute
  `docker compose exec euvieouvi flask --app euvieouvi.wsgi db current`.
- **Falha de dependência no sync:** teste a fonte, redescubra bibliotecas, confira o usuário e veja
  o log/erros por biblioteca. Itens Plex/Jellyfin malformados são identificados por ID e título,
  contados como falha segura e ignorados sem descartar o restante da página.
- **Catálogo sem os dois ícones:** sincronize ambas as bibliotecas. IDs exatos unem registros;
  mídias de nível superior sem IDs usam título/ano exatos normalizados, enquanto itens hierárquicos
  ficam limitados ao próprio pai para impedir correspondências falsas.
- **Webhook não conclui:** confira URL secreta, evento, usuário Jellyfin e disponibilidade. Eventos
  anteriores ao catálogo são reconciliados na próxima sync.
- **Nada em reprodução:** habilite play/resume do Plex ou playback-start do Jellyfin.
- **SQLite busy:** evite filesystem de rede, mantenha um contêiner e ajuste o timeout na página Sistema.
- **Credenciais não podem ser descriptografadas:** restaure a `.euvieouvi-master.key` correspondente
  em `/data`, com UID/GID 10001 e modo `0600`; outra chave não abre credenciais do banco existente.
- **Capa ausente:** confira fonte/permissões; capas externas exigem o provedor de metadados.

## Identificação e controle acumulativo de assistidos

Em **Metadados → Identificação de mídias**, mapeie raízes vistas pelos servidores para uma
mesma raiz lógica (`/mnt/tv => /shared/tv`, `/media/series => /shared/tv`). Só configure raízes
que realmente apontam para o mesmo conteúdo. Caminhos completos são comparados com
maiúsculas/minúsculas; nomes de arquivos isolados nunca são evidência suficiente.
As próximas sincronizações completas coletam caminhos do Plex e Jellyfin, sem precisar
montar a mídia no euvieouvi. A conciliação de séries usa arquivos de episódios com coordenadas
compatíveis; ambiguidades, edições simultâneas e correções manuais conflitantes não são unidas.
Os caminhos coletados podem ser vistos em **Referências físicas** nos detalhes da mídia.
Uma série dividida em duas entradas no mesmo servidor pode ser unida quando todos os caminhos
de ambas foram coletados e outro servidor agrupa esses mesmos arquivos em uma única série.
IDs iguais, por si só, não liberam essa união; arquivos sem confirmação mantêm o grupo separado.

Nos detalhes de filmes e séries, **Corrigir identificação** busca no TMDB ou aceita um ID
exato, inclusive para registros que só existem no histórico. A escolha substitui metadados/IDs
do catálogo, preserva eventos e datas de reprodução, protege a identidade em futuras syncs e
envia pôster e imagem de fundo disponíveis ao job de imagens. Não altera os metadados no
Plex/Jellyfin. A credencial TMDB configurada é necessária; a correção pode ser refeita.
A correção também concilia imediatamente registros compatíveis e seguros do catálogo,
preservando o registro selecionado e o histórico. Referências antigas indisponíveis não
conflitam com referências atuais; edições simultâneas continuam protegidas. As URLs das
imagens mudam quando a arte muda, e capas ausentes não ficam no cache do navegador. A consulta
em lote de séries já existente no Plex também preenche ano, resumo e imagens, sem consulta
individual por episódio.

Em **Ordenar**, escolha **Não identificados primeiro** para destacar registros sem IDs de
TMDB, TVDB, IMDb ou MusicBrainz. A verificação inclui todos os registros agrupados; o ID interno
do Plex/Jellyfin não basta. A opção fica salva entre visitas, e crescente/decrescente ordena
os títulos dentro de cada grupo. A ausência de ID é um indicativo para revisão, não uma
confirmação de identificação incorreta.

Conclusões são acumulativas: respostas “não assistido” e atualizações de progresso não apagam
uma conclusão histórica. Uma data mais antiga também não substitui a última reprodução
conhecida. Jobs e webhooks propagam conclusões nos dois sentidos, nunca “desassistidos”.

A ordenação **Última reprodução** usa datas reais do histórico dos servidores, datas de
reprodução informadas pelos servidores ou webhooks válidos. A hora da sincronização nunca
conta como reprodução. Progresso importado sem data permanece sem data, mas continua salvo
e sendo propagado. Webhooks na fila mantêm a hora original do evento, mesmo após uma tentativa
posterior. A migração recalcula o catálogo sem alterar o histórico ou as datas de assistidos.

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
