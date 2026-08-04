# euvieouvi v2 — Visão e Escopo

**Status:** Entrega 1 — aprovada em 4 de agosto de 2026  
**Data:** 4 de agosto de 2026  
**Papel do documento:** fonte oficial para delimitar o produto e orientar as próximas entregas.

## 1. Decisão de partida

O `euvieouvi v2` será um projeto novo e limpo. O protótipo e a base de código anteriores não serão usados como fundamento estrutural da nova versão.

Nenhum código de produção será iniciado antes da conclusão e aprovação da documentação de arquitetura correspondente. As decisões documentadas prevalecem sobre ideias, experimentos e implementações anteriores.

## 2. Visão do produto

O `euvieouvi v2` será um sistema **self-hosted** para registrar, sincronizar e consultar o histórico de filmes e séries assistidos pelo usuário.

O sistema deverá consolidar esse histórico em uma base local controlada pelo próprio usuário. O Plex será a primeira fonte integrada, mas o núcleo será preparado para receber outros conectores no futuro sem alterar o conceito central nem acoplar o banco de dados a um provedor específico.

## 3. Objetivos da primeira versão funcional

1. Permitir configurar uma instalação local do sistema.
2. Conectar-se a um servidor Plex.
3. Identificar e permitir selecionar as bibliotecas Plex que participarão da sincronização.
4. Importar e manter localmente o histórico assistido de filmes e episódios.
5. Executar sincronizações incrementais, evitando o reprocessamento integral quando não for necessário.
6. Permitir consultar os dados sincronizados por meio de uma interface web básica.
7. Expor uma API REST organizada para o uso da própria interface e para integrações futuras.
8. Manter separação suficiente entre o núcleo e os conectores para permitir novas fontes posteriormente.

## 4. Escopo incluído

### 4.1 Configuração

- Configuração inicial da aplicação.
- Configuração da conexão com o Plex.
- Consulta das bibliotecas disponíveis no Plex.
- Seleção das bibliotecas habilitadas para sincronização.
- Persistência local das configurações necessárias.

### 4.2 Banco de dados local

- Banco SQLite.
- Estrutura própria para filmes, séries, temporadas, episódios e registros de visualização.
- Identificadores externos armazenados como referências, sem tornar o domínio dependente do Plex.
- Registro do estado necessário para sincronizações incrementais.
- Base preparada para histórico e estatísticas posteriores, sem implementar antecipadamente recursos avançados.

### 4.3 Conector Plex

- Plex como primeiro conector oficial.
- Leitura das bibliotecas selecionadas.
- Leitura de filmes e episódios assistidos.
- Conversão dos dados recebidos para um modelo neutro do domínio.
- Tratamento independente de filmes e episódios.
- O conector não poderá gravar diretamente no banco de dados.

### 4.4 Sincronização

- Sincronização inicial dos dados relevantes.
- Sincronização incremental nas execuções seguintes.
- Processamento restrito às bibliotecas habilitadas.
- Persistência realizada pelos serviços e repositórios do núcleo, não pelo conector.
- Registro mínimo da execução para permitir diagnóstico de sucesso ou falha.

### 4.5 Interface básica

- Interface web renderizada pelo servidor.
- Configuração da integração Plex.
- Seleção de bibliotecas.
- Acionamento e acompanhamento básico da sincronização.
- Dashboard básico para consulta do histórico importado.
- Uso de HTMX e Bootstrap, sem a necessidade de uma aplicação frontend independente.

### 4.6 API

- API REST em Flask.
- Endpoints organizados por responsabilidade.
- Contratos definidos antes da implementação.
- Uso inicial pela própria interface e preparação para consumidores futuros.

## 5. Escopo explicitamente excluído desta etapa

Os itens abaixo não fazem parte da primeira versão funcional e não deverão ampliar o trabalho atual:

- Integração com TMDb.
- Integração com Trakt.
- Outros conectores além do Plex.
- Estatísticas avançadas.
- Recomendações de conteúdo.
- Recursos sociais.
- Aplicativo móvel nativo.
- Frontend SPA separado.
- Migração ou reaproveitamento estrutural do protótipo anterior.
- Funcionalidades não previstas neste documento apenas porque existiam no protótipo.

Esses itens poderão ser avaliados somente em fases futuras, depois da conclusão do núcleo aprovado.

## 6. Princípios técnicos aprovados

- Implantação **Docker-first**.
- Backend em Python.
- Framework Flask.
- Interface com HTML renderizado no servidor, HTMX e Bootstrap.
- Persistência em SQLite.
- API REST.
- Arquitetura em camadas.
- Componentes testáveis.
- Conectores desacoplados e extensíveis.
- Documentação anterior ao código.
- Ausência de escrita direta no banco pelos conectores.

## 7. Limite arquitetural já aprovado

O fluxo principal do núcleo seguirá esta direção:

`Routes → Services → Repositories → Models → SQLite`

Os conectores ficam fora desse fluxo de persistência direta. Eles coletam dados externos e devolvem objetos de transferência ou modelos neutros para os serviços. Os serviços aplicam as regras do sistema e utilizam os repositórios para ler e gravar dados.

Os detalhes de módulos, dependências, contratos e execução serão formalizados na Entrega 2 — Arquitetura Geral.

## 8. Ordem obrigatória das fases

1. Documentação de visão e arquitetura.
2. Infraestrutura.
3. Banco de dados.
4. Motor de sincronização e Plex.
5. Interface e API.
6. Histórico e estatísticas previstas para fases posteriores.
7. Novos conectores.

Uma fase pode esclarecer detalhes da fase seguinte, mas não deve antecipar implementação que dependa de arquitetura ainda não aprovada.

## 9. Regra de congelamento do escopo

Durante o desenvolvimento desta nova versão:

- Correções, esclarecimentos e ajustes necessários para cumprir o escopo são permitidos.
- Mudanças no conceito geral, na arquitetura fundamental ou nos objetivos do produto não serão incorporadas automaticamente.
- Novas ideias estruturais serão registradas para avaliação futura.
- Uma exceção estrutural somente poderá entrar na versão atual após ser identificada explicitamente, justificada e aprovada pelo responsável pelo projeto.

## 10. Critérios de conclusão da primeira versão funcional

A primeira versão funcional será considerada concluída quando:

1. Puder ser iniciada de forma reproduzível em Docker.
2. Permitir configurar uma conexão Plex válida.
3. Listar e salvar a seleção de bibliotecas Plex.
4. Sincronizar filmes e episódios assistidos das bibliotecas habilitadas.
5. Reexecutar a sincronização de forma incremental e consistente.
6. Persistir o histórico em SQLite por meio das camadas definidas.
7. Permitir consultar o resultado em um dashboard básico.
8. Disponibilizar os contratos REST necessários à operação da versão.
9. Possuir testes para as regras críticas do núcleo e da sincronização.
10. Manter o conector Plex sem acesso direto ao banco.
11. Possuir documentação suficiente para instalação, configuração e continuidade do desenvolvimento.

## 11. Critérios de aprovação desta entrega

Este documento estará aprovado quando houver concordância explícita sobre:

- a visão do produto;
- os objetivos da primeira versão funcional;
- os itens incluídos e excluídos;
- os princípios técnicos;
- a regra de congelamento;
- os critérios de conclusão.

Após a aprovação, qualquer alteração neste documento deverá ser tratada como mudança formal de escopo. A próxima entrega será a **Arquitetura Geral do `euvieouvi v2`**.
