(() => {
  "use strict";
  const language = document.documentElement.dataset.uiLanguage || "en";
  if (language !== "en") {
    document.documentElement.classList.add("i18n-ready");
    return;
  }

  const messages = new Map(Object.entries({
    "Corrigir identificação": "Fix match",
    "Identificação de mídias": "Media identity",
    "Identificação manual protegida": "Protected manual match",
    "Referências físicas": "Physical media references",
    "Até 100 caminhos coletados nas sincronizações.": "Up to 100 file paths collected during synchronization.",
    "Buscar no TMDB": "Search TMDB",
    "ID TMDB": "TMDB ID",
    "Aplicar ID": "Apply ID",
    "Usar esta identificação": "Use this match",
    "Nenhum resultado encontrado.": "No results found.",
    "Escolha o filme ou a série correta no TMDB. O histórico será preservado e a identificação ficará protegida nas próximas sincronizações. Esta correção altera somente o catálogo, não os metadados dos servidores.": "Choose the correct movie or show on TMDB. History is preserved and this match is protected during future syncs. Only the catalog is changed, not server metadata.",
    "Mapeie as pastas que apontam para os mesmos arquivos físicos. Não é necessário montar os arquivos no euvieouvi: os caminhos são informados pelos servidores.": "Map folders pointing to the same physical files. You do not need to mount media in euvieouvi: file paths are supplied by the servers.",
    "Use uma linha por pasta: origem => compartilhado. Exemplo: Plex /mnt/series => /shared/series e Jellyfin /media/tv => /shared/series. A comparação respeita maiúsculas e não usa apenas nomes de arquivos. Não mapeie pastas com conteúdos diferentes para a mesma raiz.": "One folder per line: source => shared. Example: Plex /mnt/series => /shared/series and Jellyfin /media/tv => /shared/series. Paths are case sensitive; filenames alone are never matched. Do not map folders containing different media to the same root.",
    "Configure a credencial TMDB em Metadados para corrigir a identificação.": "Configure TMDB credentials under Metadata to fix matches.",
    "Aguarde a sincronização terminar antes de corrigir a identificação.": "Wait for synchronization to finish before fixing a match.",
    "Identificação corrigida. Histórico preservado; imagens adicionadas à fila de download.": "Match corrected. History preserved; artwork queued for download.",
    "Não foi possível consultar o TMDB. Verifique a credencial e tente novamente.": "TMDB lookup failed. Check credentials and try again.",
    "Aguarde a sincronização terminar antes de alterar os caminhos.": "Wait for synchronization to finish before changing file paths.",
    "Caminhos salvos. A próxima sincronização coletará as referências físicas completas.": "Paths saved. The next sync will collect a full physical-media baseline.",
    "Use um caminho absoluto por linha no formato origem => compartilhado, sem raízes repetidas.": "Use one absolute path mapping per line: source => shared, without duplicate source roots.",
    "Pular para o conteúdo": "Skip to content",
    "Navegação principal": "Main navigation",
    "Sincronizando": "Synchronizing",
    "Abrir navegação": "Open navigation",
    "Início": "Home",
    "Catálogo": "Catalog",
    "Histórico": "History",
    "Bibliotecas": "Libraries",
    "Configurações": "Settings",
    "Sistema": "System",
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
    "Não identificados primeiro": "Unidentified first",
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
    "Fila persistente com execução única, recuperação após reinício e ordem recomendada Plex → assistidos → Jellyfin.": "Persistent single-job queue with restart recovery and the recommended Plex → watched state → Jellyfin order.",
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
    "Cancelar execução": "Cancel run",
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
    "Propaga conclusões e progresso parcial do Plex para o Jellyfin.": "Propagates Plex completions and partial progress to Jellyfin.",
    "Assistidos: Jellyfin → Plex": "Watched: Jellyfin → Plex",
    "Propaga conclusões do Jellyfin para o Plex.": "Propagates Jellyfin completions to Plex.",
    "Propaga conclusões e progresso parcial do Jellyfin para o Plex, respeitando conflitos do Plex.": "Propagates Jellyfin completions and partial progress to Plex while respecting Plex conflicts.",
    "Logs/execuções mantidos por job": "Logs/executions retained per job",
    "Máximo de tentativas por job": "Maximum attempts per job",
    "Saúde operacional": "Operational health",
    "Fila, worker e conectores sem expor credenciais.": "Queue, worker, and connectors without exposing credentials.",
    "Baixar diagnóstico": "Download diagnostics",
    "Jobs aguardando": "Waiting jobs",
    "Job em execução": "Running job",
    "Atualizações pendentes": "Pending updates",
    "Operações lentas recentes": "Recent slow operations",
    "Fonte": "Source",
    "Circuito": "Circuit",
    "Falhas consecutivas": "Consecutive failures",
    "Latência média": "Average latency",
    "Último erro": "Latest error",
    "aberto": "open",
    "fechado": "closed",
    "Operação normal.": "Operating normally.",
    "Nenhum alerta ativo.": "No active alerts.",
    "Alertas nas últimas 24h": "Alerts in the last 24 hours",
    "SQLite e armazenamento": "SQLite and storage",
    "Runtime": "Runtime",
    "Banco": "Database",
    "WAL atual": "Current WAL",
    "Máximos nas últimas 24h": "24-hour maximums",
    "Jobs na fila": "Queued jobs",
    "Latência de conector": "Connector latency",
    "Estado dos conectores": "Connector status",
    "latência indisponível": "latency unavailable",
    "Saúde operacional requer atenção.": "Operational health needs attention.",
    "Ver diagnóstico": "View diagnostics",
    "Configuração, estado e ações dos jobs": "Job configuration, status, and actions",
    "Abre em nova aba": "Opens in a new tab",
    "Aplicar reconciliação do catálogo": "Apply catalog reconciliation",
    "Desmarcado executa somente uma simulação. Quando marcado, o job cria um backup antes de unir apenas duplicatas confirmadas por identificadores externos.": "When disabled, this runs as a simulation. When enabled, the job creates a backup before merging only duplicates confirmed by external identifiers.",
    "Parâmetros de metadados": "Metadata settings",
    "Provedores, idioma e credenciais continuam protegidos na configuração específica.": "Providers, language, and credentials remain protected in their dedicated settings.",
    "Configurar metadados": "Configure metadata",
    "Histórico operacional": "Operational history",
    "Use “Execuções” para o histórico e “Acompanhar” para progresso, bibliotecas, erros e cancelamento.": "Use “Executions” for history and “Follow” for progress, libraries, errors, and cancellation.",
    "Enriquece campos ausentes do catálogo.": "Enriches missing catalog fields.",
    "Baixar imagens do catálogo": "Download catalog images",
    "Recupera imagens ausentes por IDs exatos e baixa as imagens externas pendentes.": "Recovers missing artwork through exact IDs and downloads pending external images.",
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
    "Entre na página oficial do Plex. Sua senha nunca é recebida ou armazenada pelo euvieouvi.": "Sign in on the official Plex page. Your password is never received or stored by euvieouvi.",
    "Metadados externos": "External metadata",
    "Nome local": "Local name",
    "URL do servidor": "Server URL",
    "Token já configurado. Deixe vazio para mantê-lo.": "Token already configured. Leave blank to keep it.",
    "Obrigatório no primeiro cadastro.": "Required for the initial setup.",
    "Usuário acompanhado": "Tracked user",
    "Selecione um usuário": "Select a user",
    "Teste a conexão para carregar os usuários": "Test the connection to load users",
    "Entre com o Plex e teste a conexão para carregar os usuários": "Sign in with Plex and test the connection to load users",
    "Conta Plex:": "Plex account:",
    "conectada": "connected",
    "não conectada": "not connected",
    "Entrar com o Plex": "Sign in with Plex",
    "Reconectar conta Plex": "Reconnect Plex account",
    "Configuração do sistema": "System settings",
    "Parâmetros operacionais persistidos no banco de dados.": "Operational settings persisted in the database.",
    "Nível de log": "Log level",
    "Fuso horário IANA": "IANA time zone",
    "Threads HTTP": "HTTP threads",
    "Timeout de espera do SQLite (ms)": "SQLite busy timeout (ms)",
    "Requer reinício do container.": "Requires a container restart.",
    "Requer reinício do container para todas as conexões.": "Requires a container restart for all connections.",
    "Infraestrutura Docker": "Docker infrastructure",
    "A porta publicada e o volume precisam existir antes que o aplicativo consiga abrir o banco, portanto permanecem no Compose.": "The published port and volume must exist before the application can open the database, so they remain in Compose.",
    "Porta interna": "Internal port",
    "Dados persistentes": "Persistent data",
    "Chave mestra": "Master key",
    "criada automaticamente com permissão 0600.": "created automatically with 0600 permissions.",
    "Preserve a chave mestra junto aos backups. Sem ela, as credenciais criptografadas não podem ser recuperadas.": "Preserve the master key with backups. Without it, encrypted credentials cannot be recovered.",
    "Configurações Jellyfin": "Jellyfin settings",
    "Configurações do Jellyfin": "Jellyfin settings",
    "Autorize pelo Quick Connect. Sua senha nunca é recebida ou armazenada pelo euvieouvi.": "Authorize with Quick Connect. euvieouvi never receives or stores your password.",
    "Configurar Plex": "Configure Plex",
    "Usuário autenticado:": "Authenticated user:",
    "API key legada": "Legacy API key",
    "Salve o servidor e conecte uma conta pelo Quick Connect.": "Save the server and connect an account with Quick Connect.",
    "Conta Jellyfin:": "Jellyfin account:",
    "Reconectar com Quick Connect": "Reconnect with Quick Connect",
    "Conectar com Quick Connect": "Connect with Quick Connect",
    "Autorizar no Jellyfin": "Authorize in Jellyfin",
    "No Jellyfin já conectado, abra Configurações → Quick Connect e informe:": "In a signed-in Jellyfin client, open Settings → Quick Connect and enter:",
    "No Jellyfin já conectado, abra": "In a signed-in Jellyfin client, open",
    "Configurações → Quick Connect": "Settings → Quick Connect",
    "e informe:": "and enter:",
    "Aguardando autorização automaticamente…": "Waiting for authorization automatically…",
    "A autorização pendente expirou. Inicie o Quick Connect novamente.": "The pending authorization expired. Start Quick Connect again.",
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
    "Aguardando o worker persistente.": "Waiting for the persistent worker.",
    "Execução reivindicada pelo worker persistente.": "Run claimed by the persistent worker.",
    "Execução iniciada pelo worker persistente.": "Run started by the persistent worker.",
    "Cancelamento solicitado; aguardando ponto seguro.": "Cancellation requested; waiting for a safe point.",
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
    ,"Conta Jellyfin conectada com segurança.": "Jellyfin account connected securely."
    ,"Salve o endereço do servidor antes de conectar ao Jellyfin.": "Save the server address before connecting to Jellyfin."
    ,"Ative o Quick Connect no painel do servidor Jellyfin.": "Enable Quick Connect in the Jellyfin server dashboard."
    ,"Não foi possível iniciar o Quick Connect do Jellyfin.": "Jellyfin Quick Connect could not be started."
    ,"A autorização do Jellyfin expirou ou falhou. Inicie novamente.": "Jellyfin authorization expired or failed. Start it again."
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
    ,"Job colocado na fila persistente.": "Job added to the persistent queue."
    ,"Cancelamento persistente solicitado; o job parará no próximo ponto seguro.": "Persistent cancellation requested; the job will stop at the next safe point."
    ,"Este job não está mais ativo.": "This job is no longer active."
    ,"Nenhum enriquecimento está em execução.": "No enrichment is running."
    ,"Não foi possível limpar catálogo e histórico.": "The catalog and history could not be cleaned."
    ,"O Jellyfin não respondeu. Verifique URL, autorização e usuário.": "Jellyfin did not respond. Check the URL, authorization, and user."
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
    [/^Conta Jellyfin conectada com segurança \((.+)\)\.$/u, "Jellyfin account connected securely ($1)."],
    [/^Início:\s*(.+)$/u, "Start: $1"],
    [/^Fuso horário:\s*(.+)\. Jobs agendados são executados no máximo uma vez por dia\.$/u, "Time zone: $1. Scheduled jobs run at most once per day."],
    [/^(\d+)% · (\d+) processados · (\d+) atualizados · (\d+) falhas$/u, "$1% · $2 processed · $3 updated · $4 failures"],
    [/^(\d+) processados · (\d+) atualizados · (\d+) falhas$/u, "$1 processed · $2 updated · $3 failures"],
    [/^(\d+) de (\d+) processados · (\d+) atualizados · (\d+) falhas$/u, "$1 of $2 processed · $3 updated · $4 failures"],
    [/^Tentativa (\d+)\/(\d+)(?: · heartbeat (.+))?$/u, (_value, attempt, maximum, heartbeat) => `Attempt ${attempt}/${maximum}${heartbeat ? ` · heartbeat ${heartbeat}` : ""}`],
    [/^tentativas: (\d+)\/(\d+)$/u, "attempts: $1/$2"],
    [/^mais antig[oa]: (\d+)s$/u, "oldest: $1s"],
    [/^heartbeat: (\d+)s$/u, "heartbeat: $1s"],
    [/^(\d+) alerta\(s\) operacional\(is\)$/u, "$1 operational alert(s)"],
    [/^(\d+) alerta\(s\) ativo\(s\)\.$/u, "$1 active alert(s)."],
    [/^(\d+) amostras$/u, "$1 samples"],
    [/^(\d+) falhas consecutivas · (.+)$/u, "$1 consecutive failures · $2"],
    [/^SQLite (.+) requer atualização para uma versão WAL segura\.$/u, "SQLite $1 must be upgraded to a WAL-safe release."],
    [/^Heartbeat do job está atrasado em (\d+) segundos\.$/u, "The job heartbeat is $1 seconds late."],
    [/^Job aguarda na fila há (\d+) segundos\.$/u, "A job has been queued for $1 seconds."],
    [/^Atualização instantânea aguarda há (\d+) segundos\.$/u, "An instant update has been waiting for $1 seconds."],
    [/^Circuito aberto para (.+)\.$/u, "Open circuit for $1."],
    [/^(.+) tem (\d+) falhas consecutivas\.$/u, "$1 has $2 consecutive failures."],
    [/^Em reprodução no (.+) desde (.+)$/u, "Playing on $1 since $2"],
    [/^Atualizada em (.+)$/u, "Updated at $1"],
    [/^Capa de (.+)$/u, "Cover for $1"],
    [/^Agendar (.+)$/u, "Schedule $1"],
    [/^Horário de (.+)$/u, "Time for $1"],
    [/^Ações de (.+)$/u, "Actions for $1"],
    [/^Progresso de (.+)$/u, "Progress for $1"],
    [/^Não assistida · (\d+) de (\d+) episódios · Progresso: (\d+)%$/u, "Not watched · $1 of $2 episodes · Progress: $3%"],
    [/^Assistindo · (\d+) de (\d+) episódios · Progresso: (\d+)%$/u, "Watching · $1 of $2 episodes · Progress: $3%"],
    [/^Assistida · (\d+) de (\d+) episódios · Progresso: (\d+)%$/u, "Watched · $1 of $2 episodes · Progress: $3%"],
    [/^Assistido (\d+) (?:vez|vezes) · Progresso: (\d+)%$/u, "Watched $1 time(s) · Progress: $2%"],
    [/^Assistindo · Progresso: (\d+)%$/u, "Watching · Progress: $1%"],
    [/^Não assistido · Progresso: (\d+)%$/u, "Not watched · Progress: $1%"]
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
  document.documentElement.classList.add("i18n-ready");
})();
