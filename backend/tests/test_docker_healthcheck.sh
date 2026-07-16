#!/usr/bin/env bash
# Verifies the Docker Compose healthcheck reports "healthy" after start.
#
# Usage:
#   bash backend/tests/test_docker_healthcheck.sh [cpu|gpu]
#
# Requires Docker daemon running. Exits non-zero on any failure.
set -euo pipefail

VARIANT="${1:-cpu}"

if [ "$VARIANT" = "gpu" ]; then
  COMPOSE_FILE="docker-compose.gpu.yml"
else
  COMPOSE_FILE="docker-compose.yml"
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# Use a throwaway project name + container name to avoid clashing with a
# running stack on the dev host.
export COMPOSE_PROJECT_NAME="subpipeline-hc-test"
CONTAINER="${COMPOSE_PROJECT_NAME}-subpipeline-1"

cleanup() {
  docker compose -f "$COMPOSE_FILE" down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[up] $COMPOSE_FILE"
docker compose -f "$COMPOSE_FILE" up -d --build

# start_period in compose is 60s; allow up to 120s for the first healthy state.
echo "[wait] polling health for up to 120s"
DEADLINE=$(( $(date +%s) + 120 ))
STATUS=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  STATUS="$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo unknown)"
  echo "    status=$STATUS"
  if [ "$STATUS" = "healthy" ]; then
    echo "PASS: healthcheck is healthy"
    exit 0
  fi
  sleep 5
done

echo "FAIL: container did not become healthy within 120s (last status: $STATUS)" >&2
exit 1
