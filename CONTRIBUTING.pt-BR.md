# Contribuindo com o euvieouvi

Português do Brasil · [English](CONTRIBUTING.md)

Obrigado por melhorar o euvieouvi. Mantenha alterações focadas, testadas, documentadas e seguras
para instalações self-hosted existentes.

## Antes de começar

- Procure issues e pull requests existentes para evitar trabalho duplicado.
- Use uma issue antes de implementar mudanças substanciais de comportamento ou schema.
- Nunca inclua tokens, URLs de webhook, bancos, backups, metadados privados ou logs pessoais.
- Não inclua alterações locais sem relação com o commit.

## Fluxo de desenvolvimento

1. Crie uma branch de tópico a partir da branch padrão.
2. Instale Python 3.12 e dependências com `pip install -e ".[dev]"`.
3. Implemente uma alteração pequena e coesa com testes.
4. Adicione migração Alembic para mudanças de schema; nunca reescreva migração já lançada.
5. Atualize documentação em inglês e português do Brasil quando o comportamento mudar.
6. Execute:

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

Testes não devem exigir credenciais reais nem rede. Use fixtures sanitizadas e transports mockados.
Testes com servidores reais devem permanecer opt-in.

## Expectativas de código

- Use Python 3.12 e tipagem estrita.
- Preserve a fronteira: conectores não acessam o banco.
- Preserve paginação, idempotência, checkpoint após commit e uma única sincronização.
- Trate migrações, restauração e credenciais como mudanças de alto risco.
- Proteja escritas web com CSRF e mantenha erros da API coerentes com o OpenAPI.
- Não enfraqueça privilégios do contêiner, filesystem somente leitura ou remoção de segredos.

## Commits e pull requests

Use assuntos imperativos e claros. O pull request deve explicar problema, solução, impacto em
migração/configuração, testes e incluir imagens para mudanças visuais. Vincule a issue quando
existir. Não misture formatação ou refatoração com comportamento sem relação.

Ao contribuir, você confirma ter direito de enviar o trabalho. O repositório atualmente não possui
licença; aceitar uma contribuição não concede por si só direito de redistribuição.
