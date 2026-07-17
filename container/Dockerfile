FROM node:22-bookworm AS frontend-build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json frontend/tsconfig.json frontend/tsconfig.node.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src ./src
# `npm install` (not `npm ci`) so optional platform-specific binaries (e.g.
# @rollup/rollup-linux-arm64-gnu) resolve on the current platform even when
# package-lock.json was generated on a different OS.
RUN npm install --no-audit --no-fund && npm run build

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SUBPIPELINE_DB_PATH=/config/subpipeline.db \
    SUBPIPELINE_FRONTEND_DIST=/app/frontend/dist \
    SUBPIPELINE_PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r appgroup && useradd -r -g appgroup -d /app -s /sbin/nologin app

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r /app/backend/requirements.txt \
    && mkdir -p /data /output /models /config \
    && chown -R app:appgroup /data /output /models /config

COPY --chown=app:appgroup backend /app/backend
COPY --chown=app:appgroup --from=frontend-build /app/frontend/dist /app/frontend/dist
COPY --chown=app:appgroup container/supervisord.conf /etc/supervisor/conf.d/subpipeline.conf
RUN chown -R app:appgroup /app/backend /app/frontend

EXPOSE 8000
VOLUME ["/data", "/output", "/models", "/config"]

USER app

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/subpipeline.conf"]
