# euvieouvi v2 — Índice da Documentação Aprovada

> **Registro histórico:** este índice descreve o plano original da v2 e não representa o estado
> atual do produto. Consulte [`docs/README.pt-BR.md`](../README.pt-BR.md) para a documentação
> mantida. O código, migrações, testes e OpenAPI prevalecem em caso de divergência.

**Status:** Documentação-base encerrada  
**Data de aprovação final:** 4 de agosto de 2026  
**Próxima etapa:** Fase 1 — Fundação do projeto.

## Regra de autoridade

Os documentos abaixo são a fonte oficial do `euvieouvi v2`. Implementações, ideias e protótipos anteriores não prevalecem sobre eles.

Mudanças de conceito, escopo, tecnologia fundamental ou fronteiras arquiteturais exigem alteração formal e aprovação explícita antes do código.

## Entregas aprovadas

| Ordem | Documento | Conteúdo principal | Status |
| --- | --- | --- | --- |
| 1 | `euvieouvi-v2-visao-e-escopo.md` | visão, limites e critérios da v2 | Aprovado |
| 2 | `euvieouvi-v2-arquitetura-geral.md` | camadas, componentes e dependências | Aprovado |
| 3 | `euvieouvi-v2-infraestrutura.md` | Docker, processo, volume e operação | Aprovado |
| 4 | `euvieouvi-v2-banco-de-dados.md` | entidades, integridade e migrações | Aprovado |
| 5 | `euvieouvi-v2-motor-sync-plex.md` | paginação, incrementalidade, episódios e Plex | Aprovado |
| 6 | `euvieouvi-v2-api-rest.md` | endpoints, contratos JSON e OpenAPI | Aprovado |
| 7 | `euvieouvi-v2-interface-web.md` | Jinja, HTMX, Bootstrap e fluxos web | Aprovado |
| 8 | `euvieouvi-v2-seguranca-testes-roadmap.md` | segurança, testes, qualidade e implementação | Aprovado |

## Decisões centrais congeladas

- Projeto limpo, sem base estrutural no protótipo anterior.
- Aplicação self-hosted e Docker-first.
- Python, Flask, Jinja, HTMX e Bootstrap.
- SQLite com SQLAlchemy 2.x e Alembic/Flask-Migrate.
- Monólito modular em camadas.
- Fluxo `Routes → Services → Repositories → Models → SQLite`.
- Connectors sem acesso direto ao banco.
- Plex como único connector da primeira versão.
- Filmes e episódios tratados individualmente.
- Séries parcialmente assistidas nunca ignoradas.
- Paginação obrigatória e sincronização idempotente.
- Checkpoint somente depois de persistência confirmada.
- Uma sincronização por vez.
- API `/api/v1` e interface pt-BR.
- Sem autenticação interna; operação em rede confiável ou proxy protegido.
- Release inicial planejado: `2.0.0`.

## Backlog fora da primeira versão

- agendamento automático, inclusive horário;
- autenticação, usuários e roles;
- TMDb, Trakt e outros connectors;
- estatísticas avançadas;
- recomendações;
- capas externas;
- edição manual do histórico;
- SPA, WebSocket ou SSE;
- tema escuro e internacionalização completa;
- backup automático;
- banco servidor e sincronizações concorrentes.

## Ordem de implementação

1. Fundação do projeto.
2. Infraestrutura executável.
3. Banco e repositories.
4. Contratos e connector Plex.
5. Motor de sincronização.
6. API REST.
7. Interface Web.
8. Integração e endurecimento.
9. Release `2.0.0`.

## Primeiro change autorizado

Somente a Fase 1 — Fundação do projeto:

- estrutura de diretórios;
- `pyproject.toml`;
- application factory;
- configuração;
- extensões vazias;
- erros básicos;
- logging e request ID;
- testes iniciais;
- formatação, lint e verificação de tipos.

Esse primeiro change não incluirá models definitivos, migrações, connector Plex, motor de sincronização, API funcional ou templates finais.
