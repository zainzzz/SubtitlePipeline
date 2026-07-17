#!/usr/bin/env bash
# Verifies docker-compose log rotation configuration.
#
# The compose files configure:
#   logging:
#     driver: "json-file"
#     options:
#       max-size: "50m"
#       max-file: "3"
#
# This test verifies:
#   1. Both docker-compose.yml and docker-compose.gpu.yml contain the
#      logging block with the expected max-size / max-file values.
#   2. The theoretical maximum disk usage is max-size * max-file = 150 MB.
#
# Usage:
#   bash backend/tests/test_docker_log_rotation.sh
#
# Does NOT require a running Docker daemon — it validates the *configuration*
# so that CI can catch regressions even without Docker.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

EXPECTED_MAX_SIZE="50m"
EXPECTED_MAX_FILE="3"
EXPECTED_LIMIT_MB=150  # 50 * 3

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "PASS: $*"
}

# ── Validate docker-compose.yml ────────────────────────────────────────────
COMPOSE_CPU="docker-compose.yml"
COMPOSE_GPU="docker-compose.gpu.yml"

for f in "$COMPOSE_CPU" "$COMPOSE_GPU"; do
  if [ ! -f "$f" ]; then
    fail "$f not found"
  fi

  echo "Checking $f ..."

  # Check logging driver.
  if ! grep -q 'driver: "json-file"' "$f"; then
    fail "$f: missing 'driver: \"json-file\"' in logging block"
  fi

  # Check max-size.
  if ! grep -q "max-size: \"$EXPECTED_MAX_SIZE\"" "$f"; then
    fail "$f: missing or incorrect max-size (expected $EXPECTED_MAX_SIZE)"
  fi

  # Check max-file.
  if ! grep -q "max-file: \"$EXPECTED_MAX_FILE\"" "$f"; then
    fail "$f: missing or incorrect max-file (expected $EXPECTED_MAX_FILE)"
  fi

  pass "$f: log rotation configured (max-size=$EXPECTED_MAX_SIZE, max-file=$EXPECTED_MAX_FILE, limit≈${EXPECTED_LIMIT_MB}MB)"
done

# ── Validate supervisord.conf graceful shutdown settings ──────────────────
SUPERVISOR_CONF="container/supervisord.conf"
if [ ! -f "$SUPERVISOR_CONF" ]; then
  fail "$SUPERVISOR_CONF not found"
fi

echo "Checking $SUPERVISOR_CONF ..."

# stopwaitsecs should be ≥ 10 (allow time for graceful shutdown).
# Use sed instead of grep -P for macOS portability.
STOPWAIT=$(grep 'stopwaitsecs=' "$SUPERVISOR_CONF" | head -1 | sed 's/.*stopwaitsecs=//')
if [ -z "$STOPWAIT" ] || [ "$STOPWAIT" -lt 10 ]; then
  fail "$SUPERVISOR_CONF: stopwaitsecs should be ≥ 10 (got: ${STOPWAIT:-missing})"
fi
pass "$SUPERVISOR_CONF: stopwaitsecs=$STOPWAIT (≥10, allows graceful shutdown)"

# stopsignal should be TERM (graceful, not KILL).
if ! grep -q 'stopsignal=TERM' "$SUPERVISOR_CONF"; then
  fail "$SUPERVISOR_CONF: missing stopsignal=TERM"
fi
pass "$SUPERVISOR_CONF: stopsignal=TERM"

# killasgroup / stopasgroup should be true (clean process group shutdown).
if ! grep -q 'killasgroup=true' "$SUPERVISOR_CONF"; then
  fail "$SUPERVISOR_CONF: missing killasgroup=true"
fi
if ! grep -q 'stopasgroup=true' "$SUPERVISOR_CONF"; then
  fail "$SUPERVISOR_CONF: missing stopasgroup=true"
fi
pass "$SUPERVISOR_CONF: killasgroup=true, stopasgroup=true"

echo ""
echo "All log rotation + graceful shutdown config checks passed."
