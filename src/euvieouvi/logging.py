"""Consistent UTC logging with request correlation."""

from __future__ import annotations

import logging
import re
import sys
import time

from flask import g, has_request_context

_HANDLER_NAME = "euvieouvi_stdout"
_SECRET_PATTERNS = (
    re.compile(r"(?i)(x-plex-token|authorization|secret|token)(\s*[=:]\s*)([^\s,;&]+)"),
    re.compile(r"(?i)([?&](?:x-plex-token|token|secret)=)[^&#\s]+"),
)


class RequestContextFilter(logging.Filter):
    """Attach request and component context to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern in _SECRET_PATTERNS:
            message = pattern.sub(
                _redacted_match,
                message,
            )
        record.msg = message
        record.args = ()
        record.request_id = getattr(g, "request_id", "-") if has_request_context() else "-"
        if not hasattr(record, "component"):
            record.component = record.name
        return True


def _redacted_match(match: re.Match[str]) -> str:
    separator = match.group(2) if match.lastindex and match.lastindex > 1 else ""
    return f"{match.group(1)}{separator}[REDACTED]"


def configure_logging(level: str) -> None:
    """Configure application logging idempotently."""
    root = logging.getLogger()
    root.setLevel(level)

    handler = next((item for item in root.handlers if item.get_name() == _HANDLER_NAME), None)
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler.set_name(_HANDLER_NAME)
        formatter = logging.Formatter(
            fmt=(
                "%(asctime)sZ level=%(levelname)s component=%(component)s "
                "request_id=%(request_id)s message=%(message)s"
            ),
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        handler.addFilter(RequestContextFilter())
        root.addHandler(handler)

    handler.setLevel(level)
