FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    EUVIEOUVI_ENV=production \
    EUVIEOUVI_HOST=0.0.0.0 \
    EUVIEOUVI_PORT=8000 \
    EUVIEOUVI_INSTANCE_PATH=/data

ARG APP_UID=10001
ARG APP_GID=10001

RUN groupadd --gid "${APP_GID}" euvieouvi \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home euvieouvi

WORKDIR /app

COPY pyproject.toml README.md requirements.lock ./
COPY src ./src
COPY migrations ./migrations
COPY scripts ./scripts
RUN python -m pip install --requirement requirements.lock \
    && python -m pip install --no-deps .

COPY gunicorn.conf.py ./gunicorn.conf.py
COPY docker/entrypoint.sh /usr/local/bin/euvieouvi-entrypoint

RUN mkdir -p /data \
    && chown -R euvieouvi:euvieouvi /data \
    && chmod 0555 /usr/local/bin/euvieouvi-entrypoint

USER euvieouvi

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "euvieouvi.healthcheck"]

ENTRYPOINT ["euvieouvi-entrypoint"]
CMD ["gunicorn", "--config", "/app/gunicorn.conf.py", "euvieouvi.wsgi:app"]
