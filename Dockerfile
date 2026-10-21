FROM python:3.12.13-slim-bookworm AS sqlite-builder

ARG SQLITE_AUTOCONF_VERSION=3530400
ARG SQLITE_SHA3_256=454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential ca-certificates curl \
    && curl --fail --location --proto '=https' --tlsv1.2 \
        --output /tmp/sqlite.tar.gz \
        "https://www.sqlite.org/2026/sqlite-autoconf-${SQLITE_AUTOCONF_VERSION}.tar.gz" \
    && python -c "import hashlib, pathlib; p=pathlib.Path('/tmp/sqlite.tar.gz'); actual=hashlib.sha3_256(p.read_bytes()).hexdigest(); expected='${SQLITE_SHA3_256}'; assert actual == expected, (actual, expected)" \
    && mkdir /tmp/sqlite \
    && tar --extract --gzip --file /tmp/sqlite.tar.gz --directory /tmp/sqlite --strip-components=1 \
    && cd /tmp/sqlite \
    && ./configure --prefix=/usr/local --disable-static --enable-shared --disable-readline \
    && make --jobs=2 \
    && make install

FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    EUVIEOUVI_ENV=production \
    EUVIEOUVI_INSTANCE_PATH=/data

COPY --from=sqlite-builder /usr/local/lib/libsqlite3.so* /usr/local/lib/
RUN ldconfig \
    && python -c "import sqlite3; assert sqlite3.sqlite_version == '3.53.4', sqlite3.sqlite_version"

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
