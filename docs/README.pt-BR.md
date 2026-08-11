# Documentação do projeto

Português do Brasil · [English](README.md)

Este diretório contém a documentação mantida do euvieouvi. O [README](../README.pt-BR.md) da raiz
apresenta o produto e o início rápido.

## Guias mantidos

| Guia | Objetivo |
| --- | --- |
| [Operação](operations.pt-BR.md) | Implantação, atualização, backup/restauração, monitoramento e diagnóstico. |
| [Importação offline do Trakt](trakt-offline-import.pt-BR.md) | Importação única e segura de um export completo do Trakt. |
| [Contrato OpenAPI](../openapi.yaml) | Rotas REST, parâmetros, payloads e erros. |
| [Contribuição](../CONTRIBUTING.pt-BR.md) | Desenvolvimento, testes, migrações e pull requests. |
| [Segurança](../SECURITY.pt-BR.md) | Versão suportada e relato privado de vulnerabilidades. |

## Comportamento do sistema

A aplicação possui cinco áreas principais:

1. **Web/API:** rotas Flask renderizam Jinja e expõem JSON sob `/api/v1`.
2. **Conectores:** XML do Plex e JSON do Jellyfin são convertidos em DTOs neutros.
3. **Sincronização:** snapshots de bibliotecas são paginados, persistidos, confirmados por
   checkpoint e reconciliados. Um bloqueio global permite uma sincronização ativa.
4. **Persistência:** models e repositories armazenam mídia canônica, referências por fonte,
   imagens, eventos concluídos, estado agregado, auditoria de sync e configurações.
5. **Serviços de fundo:** executores locais cuidam de sync/enriquecimento; o scheduler cuida de
   sincronizações e backups diários.

A identidade prefere IDs exatos de provedores. O fallback exato por título/ano é restrito a
filmes com uma única correspondência entre fontes. As referências continuam separadas, permitindo
que uma mídia exiba disponibilidade no Plex e Jellyfin.

Eventos assistidos representam ocorrências concluídas conhecidas. Estados representam o agregado
mais recente informado pelo servidor. Eventos de webhook registram entrega/reprodução atual e são
reconciliados ao histórico quando surge uma referência correspondente.

## Registros históricos de arquitetura

Os arquivos em [`architecture/`](architecture/) registram decisões e fases anteriores. Permanecem
como histórico, mas declarações de escopo ou disponibilidade podem estar obsoletas. O comportamento
atual, os guias mantidos, migrações, testes e o contrato OpenAPI prevalecem.

[`validation-phase8.md`](validation-phase8.md) é um registro datado, não uma declaração sobre a
versão atual. [`architecture/evolution-media-catalog.md`](architecture/evolution-media-catalog.md)
é histórico de implementação.

## Regras da documentação

- Documentação mantida para usuários existe em inglês e português do Brasil.
- Inglês usa o nome padrão; português usa o sufixo `.pt-BR.md`.
- Mantenha ordem de seções e conteúdo factual equivalentes.
- Use links relativos, comandos cercados, alertas de segurança e nenhuma credencial real.
- Atualize documentação no mesmo pull request de mudanças de comportamento, configuração, API ou schema.
