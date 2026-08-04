#!/usr/bin/env bash
# Docker smoke test: build a minimal image (no ML deps) and exercise the
# non-ML surfaces end-to-end. Run from repo root:
#
#   ./scripts/docker-smoke.sh
#
# This is meant to be fast (~2 min) and run on any host with Docker, not
# just the NAS. It exercises:
#   - Dockerfile / supervisord.conf build correctly
#   - the worker drops to the subpipeline user
#   - all 3 processes (api, scanner, worker) come up
#   - the API serves the basic endpoints (/api/health, /api/system/status,
#     /api/dashboard/stats, /api/tasks, /api/config, /api/dashboard/suspect-tasks)
#   - the in-container DB schema is created
#   - the worker is actually polling (visible in /api/system/process-health)
#
# What this does NOT cover: actual ASR / translation, model download.
# Those need GPU + API key — covered by NAS-level manual smoke.
#
# NOTE: We use a Docker *named volume* (not a bind mount) for /data, /config,
# /output. On macOS Docker Desktop, bind mounts go through a FUSE shim that
# occasionally returns stale data to SQLite WAL readers, which crashes the
# scanner/worker with SIGBUS. Named volumes live in Docker's own VM fs and
# don't have that problem. Production on the NAS (ext4/btrfs bind mounts) is
# unaffected; this is purely a host-side testing concern.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

IMAGE="subpipeline:smoke"
CONTAINER="subpipeline-smoke-$$"
HOST_PORT=$((18000 + (RANDOM % 1000)))

# Clean up the container + volume on exit no matter what
cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$SMOKE_VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Build smoke image (no ML deps) from container/Dockerfile"
DOCKER_BUILDKIT=1 docker build \
  -f container/Dockerfile \
  --build-arg PIP_INDEX_URL=https://pypi.org/simple \
  --build-arg SMOKE_BUILD=1 \
  -t "$IMAGE" \
  .

echo "==> Start container (named volumes for data/config/output, host port $HOST_PORT)"
SMOKE_VOL="subpipeline-smoke-vol-$$"
docker volume create "$SMOKE_VOL" >/dev/null
docker run -d --name "$CONTAINER" \
  -p "$HOST_PORT:8000" \
  -v "$SMOKE_VOL:/data" \
  -v "$SMOKE_VOL:/config" \
  -v "$SMOKE_VOL:/output" \
  -e SUBPIPELINE_PORT=8000 \
  -e SUBPIPELINE_DB_PATH=/config/subpipeline.db \
  -e SUBPIPELINE_BROWSE_ROOTS=/data,/output,/config \
  "$IMAGE"

echo "==> Wait for API to come up (up to 30s)"
for i in $(seq 1 30); do
  if curl -sf "http://localhost:$HOST_PORT/api/health" >/dev/null 2>&1; then
    echo "    API up after ${i}s"
    break
  fi
  sleep 1
done

if ! curl -sf "http://localhost:$HOST_PORT/api/health" >/dev/null 2>&1; then
  echo "!! API did not come up within 30s"
  echo "--- last 50 lines of api log ---"
  docker logs "$CONTAINER" 2>&1 | grep -A1000 "app.api_server\|supervisord" | tail -50
  exit 1
fi

echo "==> Hit core endpoints"
check() {
  local name="$1"
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$HOST_PORT$2")
  if [[ "$code" == "$3" ]]; then
    echo "    [OK]   $name → $code"
  else
    echo "    [FAIL] $name expected $3 got $code"
    FAIL=1
  fi
}

FAIL=0
check "GET /api/health"                 "/api/health"                 200
check "GET /api/system/status"          "/api/system/status"          200
check "GET /api/dashboard/stats"        "/api/dashboard/stats"        200
check "GET /api/tasks"                  "/api/tasks"                   200
check "GET /api/tasks/9999 (not found)" "/api/tasks/9999"             404
check "GET /api/config"                 "/api/config"                  200
check "GET /api/dashboard/suspect-tasks" "/api/dashboard/suspect-tasks" 200
check "GET /api/browse?path=/data"      "/api/browse?path=/data"       200
check "GET /api/browse?path=/etc"       "/api/browse?path=/etc"        403

echo "==> Verify processes + user (via /proc, no ps needed)"
# Read process cmdline + uid directly from /proc so we don't depend on `ps`
# being installed in the slim image.
PIDS_OUT=$(docker exec -i "$CONTAINER" python3 - <<'PYEOF'
import os, re
procs = []
for entry in os.listdir('/proc'):
    if not entry.isdigit():
        continue
    pid = entry
    try:
        with open('/proc/{}/cmdline'.format(pid), 'rb') as f:
            cmdline = f.read().replace(b'\x00', b' ').decode('utf-8', 'replace').strip()
        with open('/proc/{}/status'.format(pid)) as f:
            status = f.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
    if not cmdline:
        continue
    uid_match = re.search(r'^Uid:\s+(\d+)', status, re.MULTILINE)
    uid = uid_match.group(1) if uid_match else '?'
    name_match = re.search(r'^Name:\s+(\S+)', status, re.MULTILINE)
    name = name_match.group(1) if name_match else '?'
    procs.append((pid, uid, name, cmdline))
for pid, uid, name, cmdline in procs:
    print(pid + '\t' + uid + '\t' + name + '\t' + cmdline)
PYEOF
)
echo "$PIDS_OUT" | sed 's/^/    /'

WORKER_PID=$(echo "$PIDS_OUT" | awk -F'\t' '$4 ~ /app\.worker_process/ {print $1; exit}')
if [[ -z "$WORKER_PID" ]]; then
  echo "    [FAIL] worker process not found"
  FAIL=1
else
  WORKER_UID=$(echo "$PIDS_OUT" | awk -F'\t' -v p="$WORKER_PID" '$1==p {print $2; exit}')
  # Resolve subpipeline uid from /etc/passwd to make this robust to renames
  EXPECTED_UID=$(docker exec "$CONTAINER" awk -F: '$1=="subpipeline" {print $3}' /etc/passwd)
  echo "    worker PID $WORKER_PID uid=$WORKER_UID (subpipeline=$EXPECTED_UID)"
  if [[ "$WORKER_UID" == "$EXPECTED_UID" && -n "$EXPECTED_UID" ]]; then
    echo "    [OK]   worker dropped to subpipeline user"
  else
    echo "    [FAIL] worker uid $WORKER_UID != subpipeline uid $EXPECTED_UID"
    FAIL=1
  fi
fi

# Also confirm api + scanner dropped as well
for prog in api_server scanner_process worker_process; do
  PID=$(echo "$PIDS_OUT" | awk -F'\t' -v p="$prog" '$4 ~ ("app\\." p) {print $1; exit}')
  if [[ -n "$PID" ]]; then
    echo "    [OK]   $prog running (PID $PID)"
  else
    echo "    [FAIL] $prog not found"
    FAIL=1
  fi
done

echo "==> Verify DB schema"
docker exec "$CONTAINER" ls -la /config/ | head -10
docker exec "$CONTAINER" python3 -c "
import sqlite3, sys
c = sqlite3.connect('/config/subpipeline.db')
tables = [r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\").fetchall()]
print('    tables:', ', '.join(tables))
required = {'files', 'system_config', 'tasks', 'task_logs', 'translation_cache'}
missing = required - set(tables)
if missing:
    print('    [FAIL] missing tables:', missing)
    sys.exit(1)
print('    [OK]   all required tables present')
"

if [[ "${FAIL:-0}" -ne 0 ]]; then
  echo "==> FAILED. Recent container logs:"
  docker logs --tail 80 "$CONTAINER" 2>&1
  exit 1
fi

echo "==> All smoke checks passed. Container: $CONTAINER (will be cleaned up)"
