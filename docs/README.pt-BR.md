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
5. **Serviços de fundo:** um worker apoiado pelo banco reivindica jobs independentes com lease
   renovável e delega o trabalho limitado aos executores locais. A fila durável de atualizações
   instantâneas é separada; o scheduler enfileira jobs diários no máximo uma vez por dia e
   persiste amostras limitadas da saúde operacional.

A identidade prefere IDs exatos de provedores. O fallback exato por título/ano é restrito a
filmes com uma única correspondência entre fontes. As referências continuam separadas, permitindo
que uma mídia exiba disponibilidade no Plex e Jellyfin.

Eventos assistidos representam ocorrências concluídas conhecidas. Estados representam o agregado
mais recente informado pelo servidor. Eventos de webhook registram entrega/reprodução atual e são
reconciliados ao histórico quando surge uma referência correspondente.

Jobs mantêm progresso, resultado, logs rotativos e uma execução global ativa. Leases vencidos são
reenfileirados com backoff limitado; a intenção de cancelamento é confirmada antes de ser enviada
ao executor. A ordem preferida é catálogo Plex, assistidos Plex → Jellyfin, assistidos Jellyfin →
Plex e, por último, catálogo Jellyfin. A
reconciliação de assistidos usa checkpoints incrementais com baselines completas periódicas;
mudanças de identidade/configuração de servidor invalidam esses checkpoints.

O runtime Docker fixa SQLite 3.53.4 e a produção recusa runtimes antigos afetados pela condição de
corrida no reset do WAL. O histórico operacional fica retido por 30 dias e é exposto como resumo
de 24 horas, sem credenciais nem payloads de tarefas.

Parâmetros operacionais ficam no SQLite e são editados em `/setup`. Credenciais persistidas de
conectores e provedores usam criptografia autenticada. O Docker cria a chave mestra em `/data`; a
porta interna fixa e o volume de dados permanecem como infraestrutura de bootstrap porque são
necessários antes da abertura do banco. A autorização da conta Plex usa o fluxo PIN hospedado pelo
Plex e nunca armazena a senha Plex.

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
