# euvieouvi v2 — API REST

**Status:** Entrega 6 — aprovada em 4 de agosto de 2026  
**Data:** 4 de agosto de 2026  
**Base:** Entregas 1 a 5 aprovadas em 4 de agosto de 2026.

## 1. Objetivo

Definir o contrato HTTP/JSON da primeira versão do `euvieouvi v2`. A API permitirá configurar o Plex, descobrir e selecionar bibliotecas, iniciar e acompanhar sincronizações e consultar catálogo e histórico local.

A API não implementará regras de negócio. Routes validarão o contrato HTTP e chamarão os mesmos services usados pelas web routes.

## 2. Limites

Incluído:

- saúde da aplicação;
- configuração da fonte Plex;
- teste de conexão;
- descoberta e seleção de bibliotecas;
- início, cancelamento e consulta de sincronizações;
- consulta paginada de mídia, estados e eventos assistidos;
- resumo básico para o dashboard.

Excluído:

- autenticação e autorização de usuários;
- endpoints TMDb, Trakt ou outros connectors;
- recomendações;
- estatísticas avançadas;
- alteração manual do histórico;
- exclusão destrutiva de catálogo ou execuções;
- webhooks públicos;
- agendamento automático.

## 3. Base URL e versionamento

- Prefixo: `/api/v1`.
- Healthchecks operacionais permanecem fora da versão funcional: `/health/live` e `/health/ready`.
- Mudança incompatível exige novo prefixo principal, como `/api/v2`.
- Inclusão de campo opcional, endpoint novo ou novo valor documentado poderá ocorrer dentro de `v1`.
- Campos existentes não mudarão de significado silenciosamente.

## 4. Formato geral

- JSON UTF-8.
- `Content-Type: application/json` para corpos JSON.
- Nomes de campos em `snake_case`, alinhados ao domínio Python e ao banco.
- Datas e horários em ISO 8601 UTC, terminados em `Z`.
- Datas sem horário em `YYYY-MM-DD`.
- Durações e posições em milissegundos, com sufixo `_ms`.
- IDs internos expostos como inteiros.
- IDs externos expostos como texto.
- Campos desconhecidos em requisições serão rejeitados por padrão.
- Valores ausentes e `null` terão significados distintos quando o contrato permitir ambos.

## 5. Respostas de sucesso

### 5.1 Recurso único

O recurso será devolvido diretamente, sem envelope genérico desnecessário:

```json
{
  "id": 12,
  "name": "Plex principal",
  "connector_type": "plex",
  "enabled": true
}
```

### 5.2 Coleção paginada

```json
{
  "items": [],
  "pagination": {
    "limit": 50,
    "next_cursor": null,
    "has_more": false
  }
}
```

### 5.3 Operação assíncrona

Criação de sincronização retorna `202 Accepted`, o recurso `sync_run` e cabeçalho `Location` apontando para sua consulta.

## 6. Formato de erro

Todos os erros seguirão uma estrutura estável:

```json
{
  "error": {
    "code": "sync_already_running",
    "message": "A synchronization is already running.",
    "status": 409,
    "request_id": "01J...",
    "details": []
  }
}
```

Regras:

- `code` é estável e apropriado para clientes.
- `message` é segura e legível.
- `status` repete o status HTTP.
- `request_id` permite correlação com logs.
- `details` contém erros de campos quando aplicável.
- Stack traces, SQL, tokens e respostas Plex não serão expostos.

Exemplo de validação:

```json
{
  "error": {
    "code": "validation_error",
    "message": "The request contains invalid fields.",
    "status": 422,
    "request_id": "01J...",
    "details": [
      {
        "field": "base_url",
        "code": "invalid_url",
        "message": "A valid HTTP or HTTPS URL is required."
      }
    ]
  }
}
```

## 7. Status HTTP

| Status | Uso |
| --- | --- |
| `200 OK` | consulta ou alteração síncrona bem-sucedida |
| `201 Created` | novo recurso persistente criado |
| `202 Accepted` | sincronização aceita para segundo plano |
| `204 No Content` | ação sem corpo de retorno |
| `400 Bad Request` | JSON malformado ou erro de protocolo |
| `404 Not Found` | recurso interno inexistente |
| `409 Conflict` | estado atual impede a operação |
| `415 Unsupported Media Type` | corpo enviado com tipo incorreto |
| `422 Unprocessable Content` | estrutura válida com campos inválidos |
| `502 Bad Gateway` | resposta inválida ou falha do Plex |
| `503 Service Unavailable` | dependência indisponível ou aplicação não pronta |
| `504 Gateway Timeout` | timeout ao acessar o Plex |
| `500 Internal Server Error` | falha inesperada |

## 8. Paginação local

Coleções usarão paginação por cursor opaco:

- `limit`: padrão 50, mínimo 1, máximo 200;
- `cursor`: valor retornado pela página anterior;
- `sort`: somente valores explicitamente permitidos por endpoint;
- `order`: `asc` ou `desc` quando aplicável.

O cursor codificará valores de ordenação e desempate, não um offset Plex. Clientes não deverão interpretá-lo ou modificá-lo.

Toda ordenação terá desempate por `id`. Um cursor incompatível com filtros ou ordenação será rejeitado com `422`.

## 9. Filtros

- Parâmetros repetidos serão usados somente onde documentado.
- Booleanos aceitam apenas `true` ou `false`.
- Filtros desconhecidos serão rejeitados.
- Texto de busca será normalizado e terá comprimento máximo.
- Intervalos de data usarão `watched_from` inclusivo e `watched_to` exclusivo.
- O banco continuará sendo a fonte das consultas; a API não consultará o Plex para listar o histórico local.

## 10. Endpoint de liveness

### `GET /health/live`

Confirma que o processo HTTP responde.

Resposta `200`:

```json
{
  "status": "alive"
}
```

Não acessa banco nem Plex.

## 11. Endpoint de readiness

### `GET /health/ready`

Confirma inicialização, acesso ao banco e compatibilidade da migração.

Resposta pronta:

```json
{
  "status": "ready",
  "database": "ready",
  "schema": "current"
}
```

Indisponibilidade do Plex não falhará readiness.

## 12. Fonte Plex

### 12.1 `GET /api/v1/sources`

Lista fontes configuradas. Nunca devolve segredo.

Campos resumidos:

- `id`;
- `connector_type`;
- `name`;
- `base_url`;
- `enabled`;
- `has_secret`;
- `last_connection_test_at`;
- `last_connection_status`;
- `created_at`;
- `updated_at`.

### 12.2 `POST /api/v1/sources`

Cria uma fonte Plex.

Requisição:

```json
{
  "connector_type": "plex",
  "name": "Plex principal",
  "base_url": "http://192.168.15.10:32400",
  "secret": "plex-token",
  "enabled": true
}
```

Regras:

- `connector_type` aceita somente `plex` nesta versão;
- nome é único;
- URL aceita somente HTTP ou HTTPS, sem credencial embutida;
- segredo é obrigatório na criação;
- resposta devolve `has_secret: true`, nunca o valor.

### 12.3 `GET /api/v1/sources/{source_id}`

Consulta uma fonte sem segredo.

### 12.4 `PATCH /api/v1/sources/{source_id}`

Permite alterar somente:

- `name`;
- `base_url`;
- `secret`;
- `enabled`.

Omissão de `secret` mantém o existente. `secret: null` não será aceito como exclusão implícita.

Alterar identidade ou URL da fonte exigirá novo teste e poderá invalidar checkpoints após confirmação explícita do service.

### 12.5 Exclusão

Não haverá `DELETE /sources/{id}` nesta versão porque a fonte pode possuir histórico. Desabilitar será a operação segura.

## 13. Teste de conexão Plex

### `POST /api/v1/sources/{source_id}/connection-test`

Executa teste leve e síncrono.

Resposta `200`:

```json
{
  "source_id": 1,
  "status": "succeeded",
  "server_name": "plexsrv",
  "server_identifier": "...",
  "server_version": "...",
  "capabilities": ["libraries", "history"]
}
```

Possíveis erros:

- `plex_authentication_failed`;
- `plex_unreachable`;
- `plex_timeout`;
- `plex_invalid_response`.

O teste não descobre bibliotecas nem inicia sincronização.

## 14. Descoberta de bibliotecas

### `POST /api/v1/sources/{source_id}/library-discoveries`

Executa descoberta síncrona, pois é uma operação curta esperada. Se os testes demonstrarem duração imprópria, sua implementação poderá usar o executor existente sem mudar o resultado funcional.

Resposta `200`:

```json
{
  "source_id": 1,
  "discovered": 4,
  "supported": 2,
  "unsupported": 2,
  "libraries": []
}
```

Uma falha incompleta não marcará bibliotecas anteriores como indisponíveis.

## 15. Bibliotecas

### 15.1 `GET /api/v1/libraries`

Filtros:

- `source_id`;
- `media_type`;
- `enabled`;
- `available`.

Ordenação padrão por `name`, com desempate por `id`.

Recurso:

```json
{
  "id": 3,
  "source_id": 1,
  "external_id": "1",
  "name": "Filmes",
  "media_type": "movie",
  "enabled": true,
  "available": true,
  "discovered_at": "2026-08-04T16:00:00Z",
  "last_seen_at": "2026-08-04T16:00:00Z"
}
```

### 15.2 `GET /api/v1/libraries/{library_id}`

Consulta uma biblioteca.

### 15.3 `PATCH /api/v1/libraries/{library_id}`

Permite alterar somente `enabled`.

```json
{
  "enabled": true
}
```

Uma biblioteca indisponível não poderá ser habilitada até reaparecer em descoberta bem-sucedida.

### 15.4 Atualização em lote

`PATCH /api/v1/libraries` não será criado inicialmente. A interface poderá enviar alterações individuais. Um endpoint em lote somente será adicionado se houver necessidade concreta e contrato transacional explícito.

## 16. Início da sincronização

### `POST /api/v1/sync-runs`

Requisição:

```json
{
  "source_id": 1
}
```

Comportamento:

- valida fonte e existência de biblioteca habilitada;
- rejeita concorrência com `409`;
- cria execução `queued`;
- envia ao executor local;
- retorna `202` e `Location: /api/v1/sync-runs/{id}`.

Resposta:

```json
{
  "id": 81,
  "source_id": 1,
  "trigger": "api",
  "status": "queued",
  "created_at": "2026-08-04T16:30:00Z"
}
```

Não será aceito parâmetro de biblioteca arbitrário: a execução usa o snapshot de todas as bibliotecas habilitadas da fonte.

## 17. Consulta de sincronizações

### 17.1 `GET /api/v1/sync-runs`

Filtros:

- `source_id`;
- `status`;
- `created_from`;
- `created_to`.

Ordenação padrão: `created_at desc`, `id desc`.

### 17.2 `GET /api/v1/sync-runs/{sync_run_id}`

Devolve:

- estado e timestamps;
- heartbeat;
- contadores;
- resumo seguro;
- resultado por biblioteca;
- erros sanitizados limitados;
- último checkpoint confirmado por biblioteca.

O endpoint é apropriado para polling da interface. Não manterá conexão aberta.

### 17.3 `GET /api/v1/sync-runs/active`

Devolve a execução ativa ou `204 No Content` quando não houver.

## 18. Cancelamento

### `POST /api/v1/sync-runs/{sync_run_id}/cancellation`

Solicita cancelamento cooperativo.

- retorna `202` quando o pedido é registrado;
- retorna `409` se a execução já terminou;
- não interrompe um commit em andamento;
- resultado final será `interrupted` com motivo de cancelamento nesta versão.

Repetir a solicitação enquanto o cancelamento estiver pendente é idempotente.

## 19. Consulta de catálogo

### 19.1 `GET /api/v1/media`

Filtros:

- `kind`: movie, show, season ou episode;
- `library_id`;
- `parent_id`;
- `query`;
- `year`;
- `available`;
- `watched`;
- `watched_from`;
- `watched_to`.

Ordenações permitidas inicialmente:

- `title`;
- `year`;
- `last_watched_at`;
- `updated_at`.

Cada item resumido poderá incluir:

- identidade e tipo;
- título, ano e hierarquia resumida;
- duração;
- disponibilidade em fonte;
- estado assistido agregado;
- último horário assistido;
- número conhecido de visualizações.

### 19.2 `GET /api/v1/media/{media_id}`

Devolve:

- metadados internos;
- pai e filhos resumidos quando apropriado;
- referências externas sanitizadas;
- identificadores de catálogo;
- estado assistido;
- resumo de eventos conhecidos.

Não devolve payload Plex bruto.

## 20. Eventos assistidos

### `GET /api/v1/watch-events`

Filtros:

- `media_id`;
- `source_id`;
- `library_id`;
- `kind`;
- `watched_from`;
- `watched_to`;
- `completed`.

Ordenação padrão: `watched_at desc`, `id desc`.

Recurso:

```json
{
  "id": 9001,
  "media_id": 200,
  "source_id": 1,
  "watched_at": "2026-08-03T23:45:00Z",
  "completed": true,
  "progress_ms": null,
  "duration_ms": 1440000,
  "view_number": 2
}
```

O endpoint lista somente ocorrências conhecidas. `view_count` agregado estará no estado do item, não será expandido artificialmente em eventos.

## 21. Estado assistido

### `GET /api/v1/watch-states`

Filtros:

- `media_id`;
- `source_id`;
- `completed`;
- `observed_from`;
- `observed_to`.

Este endpoint oferece acesso explícito ao estado agregado sem confundi-lo com histórico de eventos.

Não haverá alteração manual por API nesta versão.

## 22. Dashboard básico

### `GET /api/v1/dashboard/summary`

Resposta conceitual:

```json
{
  "media": {
    "movies": 0,
    "shows": 0,
    "episodes": 0
  },
  "watched": {
    "movies": 0,
    "episodes": 0
  },
  "sources": {
    "configured": 0,
    "enabled": 0
  },
  "libraries": {
    "available": 0,
    "enabled": 0
  },
  "last_sync_run": null,
  "active_sync_run": null
}
```

Este é um resumo operacional e de catálogo. Não inclui rankings, tendências ou estatísticas avançadas.

## 23. Segredos

- `secret` será aceito somente em criação ou alteração da fonte.
- Nenhuma resposta devolverá segredo ou fragmento identificável.
- Será usado apenas `has_secret`.
- Logs registrarão `source_id`, nunca o token.
- Erro de validação não ecoará o valor recebido.
- Documentação interativa não incluirá segredo real como exemplo.

## 24. Concorrência e idempotência HTTP

- `PATCH` será idempotente para o mesmo corpo.
- Repetir cancelamento pendente terá o mesmo efeito.
- Iniciar sincronização enquanto existe uma ativa retorna a execução existente no campo de detalhe do erro `409`.
- Não haverá cabeçalho genérico `Idempotency-Key` na primeira versão.
- Descoberta repetida faz upsert, sem duplicar bibliotecas.
- Consultas nunca alteram checkpoint ou estado Plex.

## 25. CORS, proxy e origem

- CORS ficará desabilitado por padrão.
- A web UI da própria aplicação usa mesma origem.
- Habilitação de origens externas não integra esta versão.
- Cabeçalhos de proxy serão confiados somente quando explicitamente configurado o número de proxies confiáveis.
- A aplicação não inferirá segurança de qualquer `X-Forwarded-*` recebido diretamente.

## 26. Segurança sem autenticação interna

A API não possui autenticação própria porque isso não foi aprovado no escopo. Consequências documentadas:

- deve ser exposta apenas em rede confiável ou protegida por proxy reverso;
- não deve ser publicada diretamente na internet;
- o operador controla TLS e acesso externo;
- endpoints de escrita não serão apresentados como públicos;
- adicionar login, usuários, roles ou API keys exige mudança formal de escopo.

## 27. OpenAPI

Antes da implementação das routes será criado `openapi.yaml` correspondente a este documento.

Regras:

- OpenAPI será versionado no repositório.
- Schemas reutilizáveis representarão recursos e erros.
- Exemplos não conterão credenciais reais.
- Testes validarão respostas contra os schemas críticos.
- Alteração de route exigirá atualização do contrato no mesmo change set.
- A documentação interativa, se habilitada, ficará desativável em produção.

## 28. Observabilidade da API

Cada requisição terá:

- `request_id` gerado ou validado;
- método e rota normalizada;
- status;
- duração;
- tamanho aproximado de resposta quando disponível;
- identificação segura do recurso relevante.

Não serão registrados:

- corpos com segredos;
- token Plex;
- parâmetros de cursor em nível informativo;
- payload externo bruto.

## 29. Limites de requisição

- Tamanho máximo de corpo configurado e conservador.
- JSON profundamente aninhado não será necessário.
- Busca textual terá limite de caracteres.
- `limit` máximo será 200.
- Timeouts de acesso ao Plex serão independentes do timeout HTTP geral.
- Endpoint de sincronização devolve rapidamente após enfileirar localmente.

## 30. Mapa de endpoints

| Método | Caminho | Finalidade |
| --- | --- | --- |
| GET | `/health/live` | liveness |
| GET | `/health/ready` | readiness |
| GET | `/api/v1/sources` | listar fontes |
| POST | `/api/v1/sources` | criar fonte Plex |
| GET | `/api/v1/sources/{id}` | consultar fonte |
| PATCH | `/api/v1/sources/{id}` | alterar fonte |
| POST | `/api/v1/sources/{id}/connection-test` | testar Plex |
| POST | `/api/v1/sources/{id}/library-discoveries` | descobrir bibliotecas |
| GET | `/api/v1/libraries` | listar bibliotecas |
| GET | `/api/v1/libraries/{id}` | consultar biblioteca |
| PATCH | `/api/v1/libraries/{id}` | habilitar/desabilitar |
| POST | `/api/v1/sync-runs` | iniciar sincronização |
| GET | `/api/v1/sync-runs` | listar execuções |
| GET | `/api/v1/sync-runs/active` | consultar execução ativa |
| GET | `/api/v1/sync-runs/{id}` | detalhar execução |
| POST | `/api/v1/sync-runs/{id}/cancellation` | solicitar cancelamento |
| GET | `/api/v1/media` | consultar catálogo |
| GET | `/api/v1/media/{id}` | detalhar mídia |
| GET | `/api/v1/watch-events` | consultar eventos conhecidos |
| GET | `/api/v1/watch-states` | consultar estado agregado |
| GET | `/api/v1/dashboard/summary` | resumo básico |

## 31. Testes obrigatórios

### 31.1 Contrato

- JSON e tipos corretos;
- campos obrigatórios e opcionais;
- rejeição de campos desconhecidos;
- segredo nunca aparece em respostas;
- erros seguem formato único;
- status HTTP corretos;
- cursores válidos e inválidos;
- limites mínimo e máximo;
- timestamps UTC.

### 31.2 Fluxos

- criar, testar e alterar fonte;
- descobrir e selecionar bibliotecas;
- fonte ou biblioteca inexistente;
- iniciar e acompanhar sincronização;
- conflito com execução ativa;
- cancelamento cooperativo;
- consulta paginada do catálogo;
- separação de eventos e estado agregado;
- dashboard vazio e populado;
- indisponibilidade do Plex não falha consultas locais nem healthcheck.

### 31.3 Segurança

- token ausente em logs capturados;
- URL com credencial rejeitada;
- CORS desabilitado;
- corpo excessivo rejeitado;
- cabeçalhos de proxy não confiáveis ignorados;
- mensagens internas sanitizadas.

## 32. Decisões que exigiriam mudança formal

- remover versionamento do prefixo;
- criar lógica de negócio nas routes;
- permitir resposta com segredo;
- adicionar autenticação, usuários ou roles;
- habilitar CORS amplo por padrão;
- expor payload Plex bruto;
- criar endpoints destrutivos;
- permitir escrita manual do histórico;
- adicionar agendamento ou outros connectors;
- misturar `watch_events` e `watch_states`.

## 33. Critérios de aprovação desta entrega

Esta entrega estará aprovada quando houver concordância explícita sobre:

- prefixo `/api/v1` e formatos gerais;
- resposta direta e coleção paginada;
- contrato uniforme de erros;
- endpoints de fonte e bibliotecas;
- início, acompanhamento e cancelamento de sincronização;
- consultas de catálogo, eventos e estados;
- resumo básico do dashboard;
- tratamento de segredos;
- ausência de autenticação interna e CORS por padrão;
- OpenAPI versionado antes das routes;
- testes e decisões que exigem mudança formal.

Após a aprovação, a próxima entrega será **Interface Web**.
