# Política de segurança

Português do Brasil · [English](SECURITY.md)

## Versões suportadas

O euvieouvi ainda é pré-release. Correções de segurança são aplicadas ao código mais recente da
branch padrão. Commits antigos, forks e imagens sem manutenção não recebem suporte.

## Relato de vulnerabilidade

Não abra issue, discussion ou pull request público. Use **Report a vulnerability**, o fluxo privado
de security advisory do GitHub deste repositório. Informe versão/commit, implantação, reprodução,
impacto e mitigação sugerida. Remova credenciais, tokens de webhook, conteúdo do banco e histórico
pessoal.

Se o relato privado estiver indisponível, contate o proprietário por um canal privado publicado no
perfil do GitHub e solicite um método seguro sem divulgar detalhes.

Quando possível, mantenedores devem confirmar o relato completo, validar severidade, coordenar
correção/divulgação e creditar o pesquisador quando solicitado e apropriado. Prazos não são
garantidos, pois este é um projeto voluntário.

## Limite de implantação

A aplicação não possui autenticação interna e não deve ser exposta diretamente a rede não
confiável. Use proxy reverso autenticado, TLS, restrição de rede e `EUVIEOUVI_SECRET_KEY` longo e
aleatório. Proteja instance e backups: contêm credenciais, tokens, configurações e histórico.
