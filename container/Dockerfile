FROM node:22.11-bookworm AS frontend-build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json frontend/tsconfig.json frontend/tsconfig.node.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src ./src
RUN npm ci && npm run build

FROM python:3.12.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SUBPIPELINE_DB_PATH=/config/subpipeline.db \
    SUBPIPELINE_FRONTEND_DIST=/app/frontend/dist \
    SUBPIPELINE_PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system subpipeline \
    && useradd  --system --gid subpipeline --home-dir /app --shell /usr/sbin/nologin subpipeline

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
COPY backend/requirements-smoke.txt /app/backend/requirements-smoke.txt
COPY backend/requirements-asr-smoke.txt /app/backend/requirements-asr-smoke.txt
ARG PIP_INDEX_URL=https://pypi.org/simple
# SMOKE_BUILD=1 swaps in a minimal requirements file (no whisperx/torch/etc.)
# so a host without GPU + ML toolchain can still validate the container build
# and the non-ML portions of the codebase. See scripts/docker-smoke.sh.
# ASR_SMOKE=1 layers faster-whisper on top of smoke deps so we can also run
# a real (Chinese) transcription on a known fixture. See
# scripts/docker-smoke-asr.sh.
ARG SMOKE_BUILD=0
ARG ASR_SMOKE=0
RUN if [ "$ASR_SMOKE" = "1" ]; then \
        pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r /app/backend/requirements-asr-smoke.txt; \
    elif [ "$SMOKE_BUILD" = "1" ]; then \
        pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r /app/backend/requirements-smoke.txt; \
    else \
        pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r /app/backend/requirements.txt; \
    fi \
    && mkdir -p /data /output /models /config \
    && chown -R subpipeline:subpipeline /data /output /models /config /app

COPY --chown=subpipeline:subpipeline backend /app/backend
COPY --from=frontend-build --chown=subpipeline:subpipeline /app/frontend/dist /app/frontend/dist
COPY container/supervisord.conf /etc/supervisor/conf.d/subpipeline.conf

EXPOSE 8000
VOLUME ["/data", "/output", "/models", "/config"]

# supervisord itself runs as root (it manages the lifecycle of the 3 worker
# processes). It is configured (see supervisord.conf) to drop privileges to
# the unprivileged `subpipeline` user before spawning api / scanner / worker.
# This means a compromised subpipeline process cannot write outside /app and
# /data, /output, /models, /config without first escalating.
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/subpipeline.conf"]
