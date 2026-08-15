(() => {
  "use strict";
  const language = document.documentElement.dataset.uiLanguage || "en";
  if (language !== "en") return;

  const messages = new Map(Object.entries({
    "Pular para o conteúdo": "Skip to content",
    "Navegação principal": "Main navigation",
    "Sincronizando": "Synchronizing",
    "Abrir navegação": "Open navigation",
    "Início": "Home",
    "Catálogo": "Catalog",
    "Histórico": "History",
    "Bibliotecas": "Libraries",
    "Configurações": "Settings",
    "Metadados": "Metadata",
    "Aparência": "Appearance",
    "Alternar tema claro e escuro": "Toggle light and dark theme",
    "Alternar tema": "Toggle theme",
    "Sucesso": "Success",
    "Erro": "Error",
    "Atenção": "Warning",
    "Informação": "Information",
    "Sobre esta instalação": "About this installation",
    "Sobre": "About",
    "Aplicação": "Application",
    "Versão": "Version",
    "Armazenamento": "Storage",
    "Rede": "Network",
    "SQLite local": "Local SQLite",
    "A interface e a API devem permanecer em rede confiável ou atrás de proxy reverso.": "The interface and API should remain on a trusted network or behind a reverse proxy.",
    "Filmes, séries e músicas preservados no seu histórico.": "Movies, shows, and music preserved in your history.",
    "Tipos do catálogo": "Catalog types",
    "Filmes": "Movies",
    "Séries": "TV shows",
    "Música": "Music",
    "Buscar": "Search",
    "Título, artista ou álbum": "Title, artist, or album",
    "Gênero": "Genre",
    "Gênero de filmes": "Movie genre",
    "Gênero de séries": "TV show genre",
    "Gênero musical": "Music genre",
    "Todos": "All",
    "Ordenar": "Sort",
    "Título": "Title",
    "Título original": "Original title",
    "Ano": "Year",
    "Última reprodução": "Last played",
    "Primeira reprodução": "First played",
    "Reproduções": "Plays",
    "Data de inclusão": "Date added",
    "Última atualização": "Last updated",
    "Data de remoção": "Date removed",
    "Duração": "Duration",
    "Avaliação": "Rating",
    "Disponibilidade": "Availability",
    "Toda disponibilidade": "Any availability",
    "Disponível em servidor": "Available on a server",
    "Somente no histórico": "History only",
    "Conclusão": "Completion",
    "Todos os estados": "All states",
    "Assistidos ou ouvidos": "Watched or listened",
    "Sem conclusão": "Not completed",
    "Direção": "Direction",
    "Direção da ordenação": "Sort direction",
    "Ordem crescente": "Ascending order",
    "Ordem decrescente": "Descending order",
    "Anterior": "Previous",
    "Próxima": "Next",
    "Nenhuma mídia encontrada": "No media found",
    "Ajuste os filtros ou execute uma sincronização.": "Adjust the filters or run a synchronization.",
    "Disponível no Plex": "Available on Plex",
    "Disponível no Jellyfin": "Available on Jellyfin",
    "Filme": "Movie",
    "Série": "TV show",
    "Episódio": "Episode",
    "Seu inventário e histórico local de filmes, séries e músicas.": "Your local inventory and history of movies, shows, and music.",
    "Abrir jobs": "Open jobs",
    "Próxima etapa": "Next step",
    "Continuar configuração": "Continue setup",
    "Resumo do catálogo": "Catalog summary",
    "Episódios": "Episodes",
    "Artistas": "Artists",
    "Álbuns": "Albums",
    "Faixas": "Tracks",
    "Filmes assistidos": "Watched movies",
    "Episódios assistidos": "Watched episodes",
    "Faixas ouvidas": "Listened tracks",
    "Última sincronização": "Latest synchronization",
    "Ver no painel de jobs": "View in jobs dashboard",
    "Nenhuma sincronização executada.": "No synchronization has run.",
    "Atividade recente": "Recent activity",
    "Concluído": "Completed",
    "Reprodução parcial": "Partial playback",
    "Nenhum evento histórico real conhecido.": "No known real history events.",
    "Histórico de conclusões": "Completion history",
    "Buscar título": "Search title",
    "Tipo": "Type",
    "Estado": "State",
    "Com conclusão": "Completed",
    "Filtrar": "Filter",
    "sincronização": "synchronization",
    "Nenhum resultado": "No results",
    "Jobs e tarefas": "Jobs and tasks",
    "Execute, agende e acompanhe cada operação de forma independente.": "Run, schedule, and monitor each operation independently.",
    "Operação": "Operation",
    "Estado e progresso": "Status and progress",
    "Agendamento diário": "Daily schedule",
    "Última execução": "Last run",
    "Ações": "Actions",
    "Agendada": "Scheduled",
    "Manual/API": "Manual/API",
    "Ver execuções": "View runs",
    "Acompanhar sincronização": "Monitor synchronization",
    "Executar agora": "Run now",
    "Salvar configuração": "Save settings",
    "Processar automaticamente atualizações instantâneas de assistidos": "Automatically process instant watched updates",
    "Mantém a fila durável de eventos de Plex e Jellyfin sendo processada assim que houver atividade. Se um servidor estiver indisponível, os itens permanecem na fila para nova tentativa.": "Processes the durable Plex and Jellyfin event queue as soon as activity occurs. If a server is unavailable, items remain queued for retry.",
    "Aplicar reconciliação do catálogo": "Apply catalog reconciliation",
    "Logs disponíveis": "Available logs",
    "Nenhum log disponível.": "No logs available.",
    "Carregando…": "Loading…",
    "Parâmetros de metadados": "Metadata parameters",
    "Configurar metadados": "Configure metadata",
    "Histórico operacional": "Operational history",
    "Histórico de execuções manuais e agendadas.": "History of manual and scheduled runs.",
    "Voltar aos jobs": "Back to jobs",
    "Gatilho": "Trigger",
    "Resultado": "Result",
    "Ver log": "View log",
    "Nenhuma execução.": "No runs.",
    "Nunca executado": "Never run",
    "Sincronizar Plex": "Synchronize Plex",
    "Atualiza catálogo e histórico do Plex.": "Updates the Plex catalog and history.",
    "Sincronizar Jellyfin": "Synchronize Jellyfin",
    "Atualiza catálogo e histórico do Jellyfin.": "Updates the Jellyfin catalog and history.",
    "Assistidos: Plex → Jellyfin": "Watched: Plex → Jellyfin",
    "Propaga conclusões do Plex para o Jellyfin.": "Propagates Plex completions to Jellyfin.",
    "Assistidos: Jellyfin → Plex": "Watched: Jellyfin → Plex",
    "Propaga conclusões do Jellyfin para o Plex.": "Propagates Jellyfin completions to Plex.",
    "Enriquece campos ausentes do catálogo.": "Enriches missing catalog fields.",
    "Baixar imagens do catálogo": "Download catalog images",
    "Baixa e armazena localmente as imagens pendentes.": "Downloads and locally stores pending images.",
    "Reconciliar catálogo": "Reconcile catalog",
    "Une duplicatas confirmadas por identificadores externos estáveis.": "Merges duplicates confirmed by stable external identifiers.",
    "Processar fila de atualizações": "Process update queue",
    "Repete atualizações instantâneas pendentes entre serviços.": "Retries pending instant updates between services.",
    "Remove logs excedentes e imagens órfãs e otimiza o SQLite.": "Removes excess logs and orphaned images and optimizes SQLite.",
    "Idioma": "Language",
    "Português (Brasil)": "Brazilian Portuguese",
    "Usar português brasileiro em toda a interface.": "Use Brazilian Portuguese throughout the interface.",
    "Tema": "Theme",
    "Usar preferência do sistema": "Use system preference",
    "Acompanha a configuração clara ou escura do dispositivo.": "Follows the device light or dark setting.",
    "Claro": "Light",
    "Mantém a aparência clara atual.": "Keeps the light appearance.",
    "Escuro": "Dark",
    "Mantém a aparência escura atual.": "Keeps the dark appearance.",
    "Salvar aparência": "Save appearance",
    "Informações sobre as capas do catálogo": "Catalog cover information",
    "Escolha quais identificadores podem aparecer sobre as imagens. Todos permanecem ocultos por padrão.": "Choose which identifiers may appear over images. All are hidden by default.",
    "Faixa do tipo de mídia": "Media type banner",
    "Disponibilidade no Plex": "Plex availability",
    "Disponibilidade no Jellyfin": "Jellyfin availability",
    "Estado assistido ou ouvido": "Watched or listened status",
    "Salvar": "Save",
    "Fonte habilitada": "Source enabled",
    "Último teste:": "Last test:",
    "não testado": "not tested",
    "Testar conexão": "Test connection",
    "Escolher bibliotecas": "Choose libraries",
    "Descobrir bibliotecas": "Discover libraries",
    "Selecionada": "Selected",
    "Selecionar": "Select",
    "Disponível": "Available",
    "Indisponível": "Unavailable",
    "Atividade atual": "Current activity",
    "Webhooks de conclusões": "Completion webhooks",
    "Conclusões recentes via webhook": "Recent webhook completions",
    "Eventos recebidos do Plex e Jellyfin.": "Events received from Plex and Jellyfin.",
    "Quantidade": "Quantity",
    "Nenhuma conclusão recebida.": "No completion received.",
    "URL do webhook Plex": "Plex webhook URL",
    "URL do webhook Jellyfin": "Jellyfin webhook URL",
    "Usuário Plex para histórico e propagação": "Plex user for history and propagation",
    "Salvar filtro": "Save filter",
    "Fields (obrigatório)": "Fields (required)",
    "Backup e restauração": "Backup and restore",
    "Criar backup": "Create backup",
    "Restaurar backup": "Restore backup",
    "Otimizar dados": "Optimize data",
    "Atualizar metadados": "Update metadata",
    "Enriquecer agora": "Enrich now",
    "Cancelar enriquecimento": "Cancel enrichment",
    "Enriquecimento em execução": "Enrichment running",
    "Último resultado:": "Latest result:",
    "ainda não executado": "not run yet",
    "Sincronização": "Synchronization",
    "Cancelar": "Cancel",
    "Lidos": "Read",
    "Inseridos": "Inserted",
    "Atualizados": "Updated",
    "Inalterados": "Unchanged",
    "Conclusões": "Completions",
    "Erros": "Errors",
    "Etapa atual": "Current stage",
    "Ainda sem resultados por biblioteca.": "No per-library results yet.",
    "Erros seguros": "Safe errors",
    "Página não encontrada": "Page not found",
    "O recurso solicitado não existe.": "The requested resource does not exist.",
    "Voltar ao início": "Back to home",
    "Não encontrado": "Not found",
    "Não foi possível concluir": "Unable to complete",
    "Não foi possível concluir a solicitação": "The request could not be completed",
    "ID da solicitação:": "Request ID:",
    "Configurações Plex": "Plex settings",
    "Configurações do Plex": "Plex settings",
    "O token é usado somente para conectar ao servidor e nunca volta a ser exibido.": "The token is used only to connect to the server and is never displayed again.",
    "Metadados externos": "External metadata",
    "Nome local": "Local name",
    "URL do servidor": "Server URL",
    "Token já configurado. Deixe vazio para mantê-lo.": "Token already configured. Leave blank to keep it.",
    "Obrigatório no primeiro cadastro.": "Required for the initial setup.",
    "Usuário acompanhado": "Tracked user",
    "Selecione um usuário": "Select a user",
    "Teste a conexão para carregar os usuários": "Test the connection to load users",
    "Configurações Jellyfin": "Jellyfin settings",
    "Configurações do Jellyfin": "Jellyfin settings",
    "A API key e o usuário são usados somente para leitura da biblioteca e do estado assistido.": "The API key and user are used only to read the library and watched status.",
    "Configurar Plex": "Configure Plex",
    "API key já configurada. Deixe vazio para mantê-la.": "API key already configured. Leave blank to keep it.",
    "Crie uma API key no painel do Jellyfin.": "Create an API key in the Jellyfin dashboard.",
    "ID do usuário Jellyfin": "Jellyfin user ID",
    "Usuário cujas conclusões serão acompanhadas.": "User whose completions will be tracked.",
    "Complementa somente campos ausentes usando identificadores exatos.": "Fills only missing fields using exact identifiers.",
    "O Plex continua sendo a fonte principal. Nenhuma pesquisa aproximada por título ou artista é executada automaticamente.": "Plex remains the primary source. No approximate title or artist searches run automatically.",
    "Ativar TMDB para filmes e séries": "Enable TMDB for movies and shows",
    "Token de leitura da API TMDB": "TMDB API read token",
    "Obrigatório somente se o TMDB for ativado.": "Required only when TMDB is enabled.",
    "Ativar MusicBrainz para faixas com MBID": "Enable MusicBrainz for tracks with an MBID",
    "Sem chave; respeita o limite oficial de uma solicitação por segundo.": "No key required; respects the official one-request-per-second limit.",
    "Executar após sincronizações concluídas": "Run after completed synchronizations",
    "Idioma TMDB": "TMDB language",
    "Backup e retenção": "Backup and retention",
    "Dump SQLite agendado, retenção e restauração com sobrescrita.": "Scheduled SQLite dump, retention, and overwrite restoration.",
    "Ativar backup diário": "Enable daily backup",
    "Horário backup": "Backup time",
    "Fuso:": "Time zone:",
    "Manter últimos backups": "Keep latest backups",
    "Manter últimas sincronizações": "Keep latest synchronizations",
    "Backup agora": "Back up now",
    "Cria dump em instance/backups/": "Creates a dump in instance/backups/",
    "Histórico de backups internos": "Internal backup history",
    "Arquivo": "File",
    "Tamanho": "Size",
    "Data": "Date",
    "Restaurar": "Restore",
    "Apagar": "Delete",
    "Nenhum backup interno.": "No internal backups.",
    "Restaurar de arquivo externo": "Restore from an external file",
    "Arquivo .db": ".db file",
    "Restaurar externo": "Restore external file",
    "Recriar catálogo e histórico": "Rebuild catalog and history",
    "Digite RECARREGAR": "Type RECARREGAR",
    "Apagar catálogo e histórico": "Delete catalog and history",
    "Importar export offline do Trakt": "Import an offline Trakt export",
    "Export .zip": ".zip export",
    "Fonte para associar o histórico": "Source to associate with history",
    "Selecione…": "Select…",
    "Modo": "Mode",
    "Dry-run (não grava)": "Dry run (does not write)",
    "Aplicar e criar backup": "Apply and create backup",
    "Usuário Plex dono do histórico": "Plex user who owns the history",
    "Intervalo de progresso": "Progress interval",
    "Executar importador Trakt": "Run Trakt importer",
    "Preparando…": "Preparing…",
    "Aguardando início.": "Waiting to start.",
    "Escolha as bibliotecas coletadas em cada servidor. Jellyfin e Plex aparecem separados abaixo.": "Choose the libraries collected from each server. Jellyfin and Plex are listed separately below.",
    "desativado": "disabled",
    "Atualizar": "Refresh",
    "Ausente": "Missing",
    "Sim": "Yes",
    "Não": "No",
    "Voltar ao catálogo": "Back to catalog",
    "Temporada": "Season",
    "avaliação": "rating",
    "Estúdio:": "Studio:",
    "Gêneros:": "Genres:",
    "Estado de reprodução": "Playback status",
    "Última vez:": "Last time:",
    "Nenhuma reprodução conhecida.": "No known playback.",
    "Nunca concluído": "Never completed",
    "Conteúdo": "Content",
    "Histórico completo": "Full history",
    "Paginação do histórico": "History pagination",
    "Histórico anterior": "Previous history",
    "Próximo histórico": "Next history",
    "Nenhuma referência encontrada.": "No reference found.",
    "removido": "removed",
    "disponível": "available"
    ,"A fonte selecionada não existe.": "The selected source does not exist."
    ,"A importação offline do Trakt requer o banco SQLite local.": "Offline Trakt import requires the local SQLite database."
    ,"Agendamentos dos jobs atualizados.": "Job schedules updated."
    ,"Arquivo deve ser .db SQLite.": "The file must be a SQLite .db file."
    ,"Backup não encontrado.": "Backup not found."
    ,"Cancelamento do enriquecimento solicitado.": "Enrichment cancellation requested."
    ,"Cancelamento solicitado. Os dados já confirmados serão preservados.": "Cancellation requested. Already committed data will be preserved."
    ,"Configuração de metadados atualizada.": "Metadata settings updated."
    ,"Configuração do Jellyfin salva com segurança.": "Jellyfin settings saved securely."
    ,"Configuração do Plex salva com segurança.": "Plex settings saved securely."
    ,"Configurações de backup e retenção salvas.": "Backup and retention settings saved."
    ,"Configure a fonte antes de descobrir bibliotecas.": "Configure the source before discovering libraries."
    ,"Digite RECARREGAR para confirmar a limpeza.": "Type RECARREGAR to confirm cleanup."
    ,"Enriquecimento iniciado em segundo plano.": "Enrichment started in the background."
    ,"Esta sincronização já terminou.": "This synchronization has already finished."
    ,"Filtro de usuário Plex atualizado.": "Plex user filter updated."
    ,"Há sincronização ativa; aguarde terminar antes de importar.": "A synchronization is active; wait for it to finish before importing."
    ,"Há sincronização ativa; aguarde terminar antes de limpar os dados.": "A synchronization is active; wait for it to finish before cleaning data."
    ,"Há sincronização ativa; aguarde terminar antes de restaurar.": "A synchronization is active; wait for it to finish before restoring."
    ,"Informe o token de leitura do TMDB para ativar a integração.": "Enter the TMDB read token to enable the integration."
    ,"Job iniciado em segundo plano.": "Job started in the background."
    ,"Nenhum enriquecimento está em execução.": "No enrichment is running."
    ,"Não foi possível limpar catálogo e histórico.": "The catalog and history could not be cleaned."
    ,"O Jellyfin não respondeu. Verifique URL, API key e usuário.": "Jellyfin did not respond. Check the URL, API key, and user."
    ,"O Plex não respondeu ao teste. Verifique URL, token e disponibilidade.": "Plex did not respond to the test. Check the URL, token, and availability."
    ,"O enriquecimento já está em execução.": "Enrichment is already running."
    ,"O export do Trakt deve ser um arquivo .zip.": "The Trakt export must be a .zip file."
    ,"O job já está ativo ou sua fonte não está disponível.": "The job is already active or its source is unavailable."
    ,"Preferência de aparência atualizada.": "Appearance preference updated."
    ,"Quantidade de eventos recentes atualizada.": "Recent event count updated."
    ,"Restauração por upload concluída e sobrescrita.": "Uploaded restoration completed and applied."
    ,"Salve a configuração antes de testar.": "Save the settings before testing."
    ,"Selecione a fonte à qual o histórico será associado.": "Select the source to associate with the history."
    ,"Selecione o arquivo ZIP exportado pelo Trakt.": "Select the ZIP file exported by Trakt."
    ,"Selecione um arquivo .db para restaurar.": "Select a .db file to restore."
    ,"Selecione um idioma válido.": "Select a valid language."
    ,"Selecione uma preferência de tema válida.": "Select a valid theme preference."
    ,"Seleção da biblioteca atualizada.": "Library selection updated."
    ,"Uma biblioteca indisponível não pode ser selecionada.": "An unavailable library cannot be selected."
  }));

  const dynamic = [
    [/^Início:\s*(.+)$/u, "Start: $1"],
    [/^(\d+)% · (\d+) processados · (\d+) atualizados · (\d+) falhas$/u, "$1% · $2 processed · $3 updated · $4 failures"],
    [/^(\d+) processados · (\d+) atualizados · (\d+) falhas$/u, "$1 processed · $2 updated · $3 failures"],
    [/^(\d+) de (\d+) processados · (\d+) atualizados · (\d+) falhas$/u, "$1 of $2 processed · $3 updated · $4 failures"],
    [/^Em reprodução no (.+) desde (.+)$/u, "Playing on $1 since $2"],
    [/^Atualizada em (.+)$/u, "Updated at $1"],
    [/^Capa de (.+)$/u, "Cover for $1"],
    [/^Agendar (.+)$/u, "Schedule $1"],
    [/^Horário de (.+)$/u, "Time for $1"],
    [/^Ações de (.+)$/u, "Actions for $1"],
    [/^Progresso de (.+)$/u, "Progress for $1"]
  ];

  function translated(value) {
    const exact = messages.get(value);
    if (exact) return exact;
    for (const [pattern, replacement] of dynamic) {
      if (pattern.test(value)) return value.replace(pattern, replacement);
    }
    return value;
  }

  function translateText(node) {
    if (node.parentElement?.closest('[translate="no"], code, pre, textarea, script, style')) return;
    const value = node.nodeValue || "";
    const leading = value.match(/^\s*/u)?.[0] || "";
    const trailing = value.match(/\s*$/u)?.[0] || "";
    const core = value.trim();
    if (!core) return;
    const result = translated(core);
    if (result !== core) node.nodeValue = leading + result + trailing;
  }

  function translateElement(element) {
    if (element.closest('[translate="no"]')) return;
    for (const attribute of ["title", "aria-label", "placeholder", "onsubmit"]) {
      if (element.hasAttribute(attribute)) {
        const value = element.getAttribute(attribute) || "";
        element.setAttribute(attribute, translated(value));
      }
    }
  }

  function translateTree(root) {
    if (root.nodeType === Node.TEXT_NODE) return translateText(root);
    if (!(root instanceof Element) && root !== document) return;
    if (root instanceof Element) translateElement(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.nodeType === Node.TEXT_NODE) translateText(node);
      else translateElement(node);
    }
  }

  translateTree(document);
  new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) translateTree(node);
    }
  }).observe(document.body, {childList: true, subtree: true});
})();
