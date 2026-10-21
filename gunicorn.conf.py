"""Gunicorn configuration for the single-process infrastructure."""

import os
from pathlib import Path

from gunicorn.glogging import Logger

from euvieouvi.config import read_database_runtime_settings
from euvieouvi.logging import redact_access_path


class RedactingAccessLogger(Logger):
    """Keep webhook credentials out of container access logs."""

    def atoms(self, resp, req, environ, request_time):  # type: ignore[no-untyped-def]
        values = super().atoms(resp, req, environ, request_time)
        values["U"] = redact_access_path(str(values.get("U", "")))
        return values


_instance_path = Path(os.getenv("EUVIEOUVI_INSTANCE_PATH", "/data"))
_database_uri = os.getenv("EUVIEOUVI_DATABASE_URI", f"sqlite:///{_instance_path / 'euvieouvi.db'}")
_runtime_settings = read_database_runtime_settings(_database_uri)
try:
    _configured_threads = int(_runtime_settings.get("system.gunicorn_threads", "8"))
except ValueError:
    _configured_threads = 8

bind = "0.0.0.0:8000"
workers = 1
worker_class = "gthread"
threads = min(max(_configured_threads, 1), 32)
timeout = 30
graceful_timeout = 30
keepalive = 5
preload_app = False
accesslog = "-"
errorlog = "-"
capture_output = True
logger_class = RedactingAccessLogger
# Gunicorn 26 enables a home-directory control socket by default. The image's
# root filesystem is read-only and this application does not use that channel.
control_socket_disable = True
access_log_format = (
    '%(t)s method=%(m)s path="%(U)s" status=%(s)s '
    "response_bytes=%(B)s duration_us=%(D)s request_id=%({x-request-id}o)s"
)
