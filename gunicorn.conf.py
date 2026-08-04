"""Gunicorn configuration for the single-process infrastructure."""

import os

bind = f"{os.getenv('EUVIEOUVI_HOST', '0.0.0.0')}:{os.getenv('EUVIEOUVI_PORT', '8000')}"
workers = 1
worker_class = "gthread"
threads = int(os.getenv("EUVIEOUVI_GUNICORN_THREADS", "4"))
timeout = 30
graceful_timeout = 30
keepalive = 5
preload_app = False
accesslog = "-"
errorlog = "-"
capture_output = True
access_log_format = (
    '%(t)s method=%(m)s path="%(U)s" status=%(s)s '
    "response_bytes=%(B)s duration_us=%(D)s request_id=%({x-request-id}o)s"
)
