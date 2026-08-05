# Projeto de evolução: catálogo completo de mídia

## Objetivo

Evoluir o euvieouvi para manter o inventário e o histórico de filmes, séries e músicas do
Plex, com consulta visual, disponibilidade histórica e sincronização diária configurável.

## Premissas aprovadas

- Plex é a fonte primária; TMDB, MusicBrainz e Cover Art Archive serão opcionais.
- Itens removidos do Plex e suas imagens permanecem no histórico.
- Imagens ficam em arquivos sob `instance/images`; o banco guarda somente referências.
- SQLite, paginação, checkpoints, botão manual e proteção contra sincronizações concorrentes
  permanecem.
- Séries são exibidas como série > temporada > episódio; música como artista > álbum > faixa.
- Filtros e ordenação são processados no servidor, sempre com direção ascendente/descendente.

## Fases

1. **Fundação:** tipos musicais, migração segura e agendamento diário persistente.
2. **Plex Music:** descoberta, inventário, histórico de faixas e reconciliação completa.
3. **Imagens:** cache local redimensionado, placeholders e preservação histórica.
4. **Catálogo:** grade visual, detalhes hierárquicos, indicadores e paginação.
5. **Consulta:** filtros e ordenações equivalentes às premissas aprovadas.
6. **Enriquecimento:** TMDB/MusicBrainz opcional, assíncrono e tolerante a falhas.

## Critérios gerais de aceite

- Migrações preservam a instalação e o histórico existentes.
- Uma falha ou varredura incompleta nunca marca todo o catálogo como removido.
- Eventos históricos são idempotentes e novas reproduções não substituem as anteriores.
- Sincronização manual e diária usam o mesmo motor e nunca executam simultaneamente.
- Toda fase inclui testes unitários, integração, lint, tipagem e documentação operacional.

## Estado

- Fase 1: concluída (modelo, migração e sincronização diária configurável).
- Fase 2: iniciada (descoberta, inventário, hierarquia e histórico musical implementados;
  validação ampliada contra uma biblioteca Plex real ainda pendente).
- Fases 3–6: planejadas.
