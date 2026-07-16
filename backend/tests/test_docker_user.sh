#!/usr/bin/env bash
# Verifies the CPU and GPU containers run as a non-root user and that the
# runtime data directories are owned by that user.
#
# Usage:
#   bash backend/tests/test_docker_user.sh [cpu|gpu]
#
# Requires Docker daemon running. Exits non-zero on any failure.
set -euo pipefail

VARIANT="${1:-cpu}"

if [ "$VARIANT" = "gpu" ]; then
  DOCKERFILE="container/Dockerfile.gpu"
  IMAGE="subpipeline-test:gpu"
else
  DOCKERFILE="container/Dockerfile"
  IMAGE="subpipeline-test:cpu"
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

echo "[build] $DOCKERFILE -> $IMAGE"
docker build -f "$DOCKERFILE" -t "$IMAGE" .

echo "[run] id -u  (expect non-zero)"
UID_OUT="$(docker run --rm "$IMAGE" id -u)"
echo "    uid=$UID_OUT"
if [ "$UID_OUT" = "0" ]; then
  echo "FAIL: container runs as root (uid=0)" >&2
  exit 1
fi

echo "[run] stat /app/backend (expect owner=app)"
OWNER_OUT="$(docker run --rm "$IMAGE" stat -c '%U' /app/backend)"
echo "    owner=$OWNER_OUT"
if [ "$OWNER_OUT" != "app" ]; then
  echo "FAIL: /app/backend owned by '$OWNER_OUT', expected 'app'" >&2
  exit 1
fi

echo "[run] stat data volumes (expect owner=app for all)"
for dir in /data /output /models /config; do
  OWNER="$(docker run --rm "$IMAGE" stat -c '%U:%G' "$dir")"
  echo "    $dir -> $OWNER"
  case "$OWNER" in
    app:*) ;;
    *) echo "FAIL: $dir owned by '$OWNER', expected app:*" >&2; exit 1 ;;
  esac
done

echo "PASS: $VARIANT container drops privileges to non-root user 'app'"
