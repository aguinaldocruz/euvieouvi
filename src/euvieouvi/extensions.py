"""Central extension initialization point."""

from flask import Flask


def init_extensions(app: Flask) -> None:
    """Initialize approved extensions as their implementation phases begin."""
    del app
