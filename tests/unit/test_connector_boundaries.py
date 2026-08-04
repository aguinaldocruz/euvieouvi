"""Architectural boundary contract for connectors."""

import ast
from pathlib import Path


def test_connectors_do_not_import_database_or_repositories() -> None:
    root = Path(__file__).parents[2] / "src" / "euvieouvi" / "connectors"
    forbidden = ("euvieouvi.database", "euvieouvi.extensions")
    for source_file in root.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        imported = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        ]
        assert not any(name.startswith(forbidden) for name in imported), source_file
