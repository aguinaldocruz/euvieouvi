# euvieouvi v2

Aplicação self-hosted para registrar, sincronizar e consultar o histórico de filmes e
séries assistidos. O Plex será o primeiro conector.

## Estado atual

Fase 1 — Fundação do projeto.

Esta fase contém somente:

- estrutura inicial do pacote;
- application factory do Flask;
- configuração por ambiente;
- ponto de inicialização de extensões;
- erros básicos;
- logging e request ID;
- testes e ferramentas de qualidade.

Banco, Plex, sincronização, API funcional e interface serão implementados somente nas
fases aprovadas correspondentes.

## Requisitos

- Python 3.12 ou superior compatível;
- Git.

## Ambiente de desenvolvimento

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Defina uma chave local e inicie o servidor de desenvolvimento:

```bash
export EUVIEOUVI_ENV=development
export EUVIEOUVI_SECRET_KEY=change-me-for-local-development
flask --app euvieouvi:create_app run
```

No Windows PowerShell:

```powershell
$env:EUVIEOUVI_ENV = "development"
$env:EUVIEOUVI_SECRET_KEY = "change-me-for-local-development"
flask --app euvieouvi:create_app run
```

## Qualidade

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

## Documentação

A documentação aprovada está em [`docs/architecture`](docs/architecture). O índice é
[`euvieouvi-v2-indice-documentacao.md`](docs/architecture/euvieouvi-v2-indice-documentacao.md).

