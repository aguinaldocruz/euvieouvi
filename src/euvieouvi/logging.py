"""Consistent UTC logging with request correlation."""

from __future__ import annotations

import logging
import re
import sys
import time

from flask import g, has_request_context

_HANDLER_NAME = "euvieouvi_stdout"
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(x-plex-token|authorization|secret|token|api[_-]?key)"
        r"(\s*[=:]\s*)([^\s,;&]+)"
    ),
    re.compile(r"(?i)([?&](?:x-plex-token|token|secret|api[_-]?key)=)[^&#\s]+"),
)
_AUTHORIZATION_VALUE = re.compile(r"(?i)(authorization\s*[=:]\s*)[^\r\n]+")
_WEBHOOK_SECRET_PATH = re.compile(r"^(/webhooks/(?:plex|jellyfin)/)[^/?#]+")


class RequestContextFilter(logging.Filter):
    """Attach request and component context to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = redact_text(record.getMessage())
        record.msg = message
        record.args = ()
        record.request_id = getattr(g, "request_id", "-") if has_request_context() else "-"
        if not hasattr(record, "component"):
            record.component = record.name
        return True


def _redacted_match(match: re.Match[str]) -> str:
    separator = match.group(2) if match.lastindex and match.lastindex > 1 else ""
    return f"{match.group(1)}{separator}[REDACTED]"


def redact_access_path(path: str) -> str:
    """Redact webhook credentials before Gunicorn formats its access line."""
    return _WEBHOOK_SECRET_PATH.sub(r"\1<redacted>", path)


def redact_text(value: str) -> str:
    """Remove known credential shapes from diagnostic or log text."""
    result = _AUTHORIZATION_VALUE.sub(r"\1[REDACTED]", value)
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(_redacted_match, result)
    return result


def concise_error(error: Exception, *, limit: int = 2_000) -> str:
    """Format an exception for persistent/UI summaries without SQL parameter dumps."""
    original = getattr(error, "orig", None)
    if isinstance(original, BaseException):
        detail = f"{type(error).__name__} ({type(original).__name__}): {original}"
    else:
        detail = f"{type(error).__name__}: {error}"
    detail = redact_text(detail)
    if len(detail) <= limit:
        return detail
    return detail[: max(0, limit - 22)].rstrip() + "… [detalhes truncados]"


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
