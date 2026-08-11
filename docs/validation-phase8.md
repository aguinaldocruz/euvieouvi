# Relatório de validação — Fase 8

> **Registro histórico:** resultados abaixo pertencem à execução datada e não descrevem a suíte ou
> versão atual. Consulte [`docs/README.pt-BR.md`](README.pt-BR.md) para a documentação mantida.

Data: 4 de agosto de 2026.

## Resultado automatizado

| Portão | Resultado |
| --- | --- |
| Formatação e lint | aprovado |
| Tipagem estrita | aprovada |
| Suíte padrão | 111 aprovados, 1 teste Plex real ignorado por ausência de configuração opt-in |
| Cobertura | 90,99% |
| Regressão Futurama | 161 episódios, 144 assistidos, repetição idempotente aprovada |
| Interrupção/reconciliação | aprovada com executor órfão, cancelamento e checkpoints confirmados |
| Backup/restauração | integridade SQLite e dados restaurados aprovados |
| Recriação da aplicação | banco persistente e readiness aprovados |
| Volume representativo | 3.000 filmes, paginação API e consultas web aprovadas |
| Redaction de logs | token, secret e Authorization aprovados |
| Auditoria de dependências | nenhuma vulnerabilidade conhecida encontrada por `pip-audit` |
| Imagem Python | `3.12.13-slim-bookworm`; versão de segurança mais recente da série 3.12 |
| OpenAPI | contrato 3.1 válido |
| Wheel | templates e assets locais presentes |

## Portões externos pendentes

Este ambiente não dispõe de Docker nem das variáveis do Plex real. Portanto, dois testes
não podem ser declarados executados aqui:

1. `pytest -m real_plex tests/integration/test_real_plex.py`, usando biblioteca pequena;
2. `./scripts/validate-deployment.sh`, após build e recriação com o mesmo volume.

Os testes e procedimentos estão versionados em
[`docs/operations.md`](operations.md). O token Plex não deve ser colado em relatório,
issue, log ou conversa; configure-o somente no ambiente do comando.

## Conclusão

O código automatizável da Fase 8 está aprovado. O portão integral da fase permanece
condicionado aos dois testes externos acima no host de implantação. Nenhuma mudança de
escopo foi introduzida.
