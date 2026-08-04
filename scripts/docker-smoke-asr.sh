#!/usr/bin/env bash
# ASR smoke test: builds an image with faster-whisper baked in, runs the
# full pipeline (extract_audio → run_asr → align_segments → text_process
# → subtitle_render → output_finalize → quality_checker) on a known
# Chinese audio fixture, and verifies the resulting .srt actually
# contains the expected Chinese characters.
#
#   ./scripts/docker-smoke-asr.sh
#
# What this exercises (real, in-container, with a real ML model):
#   - extract_audio stage (ffmpeg, no shim)
#   - run_asr stage (faster-whisper + Systran/faster-whisper-small,
#     ~488 MB, downloaded on first run and cached in the models host dir)
#   - text_process / segment_cleaner
#   - subtitle_render (SRT)
#   - output_finalize
#   - quality_checker (with real ASR confidence scores)
#
# This is the smoke test that answers "can it actually transcribe
# Chinese?". The cheaper smoke test (scripts/docker-smoke.sh) covers
# everything except real ASR.
#
# What this does NOT cover: GPU acceleration, multi-provider ASR
# (whisperx, qwen-asr), translation API. Those need NAS-level smoke.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

IMAGE="subpipeline:smoke-asr"
CONTAINER="subpipeline-smoke-asr-$$"
HOST_PORT=$((19000 + (RANDOM % 1000)))
FIXTURE_NAME="zh_sample.mp4"
# The fixture audio is stored as .mp3 (the TTS tool produced MPEG ADTS inside
# a .wav container, so we renamed it for accuracy).
FIXTURE_AUDIO="$REPO_ROOT/backend/tests/fixtures/${FIXTURE_NAME%.mp4}.mp3"
# The faster-whisper-small model commonly normalizes to Traditional Chinese
# (天氣 / 公園), while the fixture was synthesized in Simplified (天气 / 公园).
# We accept either form so the smoke test passes regardless of the model's
# script choice.
EXPECTED_PHRASES=("今天" "天氣|天气" "公園|公园")

# Use a persistent host dir for the model so the 488 MB faster-whisper-small
# only downloads once across runs. Bump the path or rm -rf to force redownload.
# We look for an existing checkout in the repo's models/ first (most devs
# already have faster-whisper-small from prior runs); otherwise we fall back
# to a cache dir and let the container download on first run.
HOST_REPO_MODELS="$REPO_ROOT/models/faster-whisper-small"
if [[ -f "$HOST_REPO_MODELS/model.bin" ]]; then
  MODELS_HOST_DIR="$HOST_REPO_MODELS"
  MODEL_SOURCE="repo (bind-mount, no download)"
else
  MODELS_HOST_DIR="${SUBPIPELINE_SMOKE_MODELS:-$HOME/.cache/subpipeline-smoke-models}"
  mkdir -p "$MODELS_HOST_DIR"
  MODEL_SOURCE="host cache (will download on first run)"
fi
echo "==> Model source: $MODEL_SOURCE → $MODELS_HOST_DIR"

# Bind-mount a fresh dir for /data so we can drop the fixture in.
DATA_HOST_DIR="$(mktemp -d)"
if [[ ! -f "$FIXTURE_AUDIO" ]]; then
  echo "!! Missing fixture audio: $FIXTURE_AUDIO"
  echo "   See backend/tests/fixtures/ for the expected file."
  exit 1
fi

# Wrap the audio into a minimal mp4 (1 fps blue frame + the audio). ffmpeg is
# available on the host because the user almost certainly has it for the
# NAS deployment; if not, the script errors with a clear hint.
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "!! ffmpeg not found on host. Install it (brew install ffmpeg) to build the mp4 fixture."
  exit 1
fi
AUDIO_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$FIXTURE_AUDIO")
ffmpeg -y -loglevel error -f lavfi \
  -i "color=c=blue:s=320x240:r=1:d=${AUDIO_DUR}" \
  -i "$FIXTURE_AUDIO" \
  -c:v libx264 -tune stillimage -preset ultrafast -pix_fmt yuv420p \
  -c:a aac -b:a 64k -shortest \
  "$DATA_HOST_DIR/$FIXTURE_NAME"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$DATA_HOST_DIR"
}
trap cleanup EXIT

echo "==> Build ASR smoke image (smoke + faster-whisper, no torch/whisperx)"
DOCKER_BUILDKIT=1 docker build \
  -f container/Dockerfile \
  --build-arg PIP_INDEX_URL=https://pypi.org/simple \
  --build-arg SMOKE_BUILD=1 \
  --build-arg ASR_SMOKE=1 \
  -t "$IMAGE" \
  .

echo "==> Start container (host port $HOST_PORT, models cache: $MODELS_HOST_DIR)"
# Two cases:
#   - MODELS_HOST_DIR = the model dir itself (repo cache): bind to
#     /models/faster-whisper-small so the model_manager sees it as installed
#   - MODELS_HOST_DIR = a generic cache dir: bind to /models/ so the
#     model_manager can write faster-whisper-small/ inside it
if [[ "$MODELS_HOST_DIR" == */faster-whisper-small ]]; then
  MODELS_BIND="$MODELS_HOST_DIR:/models/faster-whisper-small"
else
  MODELS_BIND="$MODELS_HOST_DIR:/models"
fi
docker run -d --name "$CONTAINER" \
  -p "$HOST_PORT:8000" \
  -v "$DATA_HOST_DIR:/data" \
  -v "$MODELS_BIND" \
  -e SUBPIPELINE_PORT=8000 \
  -e SUBPIPELINE_DB_PATH=/models/subpipeline.db \
  -e SUBPIPELINE_MODELS_DIR=/models \
  -e SUBPIPELINE_BROWSE_ROOTS=/data \
  -e HF_ENDPOINT="${HF_ENDPOINT:-}" \
  "$IMAGE"

# Wait for the API to be reachable.
echo "==> Wait for API (up to 30s)"
for i in $(seq 1 30); do
  if curl -sf "http://localhost:$HOST_PORT/api/health" >/dev/null 2>&1; then
    echo "    API up after ${i}s"
    break
  fi
  sleep 1
done
if ! curl -sf "http://localhost:$HOST_PORT/api/health" >/dev/null 2>&1; then
  echo "!! API did not come up within 30s"
  docker logs --tail 60 "$CONTAINER" 2>&1
  exit 1
fi

API="http://localhost:$HOST_PORT"

FAIL=0

# 1) Configure the system for Chinese-only ASR (no translation, monolingual srt).
echo "==> Set config: faster-whisper / small / zh source / no translation / monolingual"
curl -sS -X PUT "$API/api/config" -H "Content-Type: application/json" -d '{
  "whisper": {"provider": "faster-whisper", "model_name": "faster-whisper-small", "device": "cpu", "align_provider": "none"},
  "subtitle": {"bilingual": false, "source_language": "zh"},
  "translation": {"enabled": false}
}' >/dev/null
echo "    [OK]   config updated"

# 1a) Mark setup complete — without this, the worker's run_forever loop sleeps
#     instead of polling for tasks (it gates on is_setup_complete()).
echo "==> Mark setup complete (so the worker will start polling)"
curl -sS -X POST "$API/api/system/setup-complete" -H "Content-Type: application/json" \
  -d '{"setup_complete": true}' >/dev/null
echo "    [OK]   setup_complete=true"

# 2) Ensure the model is installed. If we bound the repo's pre-downloaded
#    copy into the container, it's already there; otherwise trigger a
#    download and wait for it.
echo "==> Check / install model: faster-whisper-small"
MODEL_STATE=$(curl -sS "$API/api/models" | python3 -c "
import json, sys
for m in json.load(sys.stdin)['items']:
    if m['name'] == 'faster-whisper-small':
        print(m.get('status', ''))
        sys.exit(0)
print('not_found')
")
if [[ "$MODEL_STATE" == "installed" ]]; then
  echo "    [OK]   model already installed (bind-mounted from $MODELS_HOST_DIR)"
else
  echo "    not installed ($MODEL_STATE) — triggering download (~488 MB, may take a while)"
  DOWNLOAD_START=$(date +%s)
  curl -sS -X POST "$API/api/models/faster-whisper-small/download" >/dev/null
  for i in $(seq 1 120); do  # up to 10 min
    STATUS=$(curl -sS "$API/api/models" | python3 -c "
import json, sys
for m in json.load(sys.stdin)['items']:
    if m['name'] == 'faster-whisper-small':
        print(m.get('status', ''), m.get('progress', 0), m.get('error', '') or '')
        sys.exit(0)
print('not_found 0')
")
    STATE=$(echo "$STATUS" | awk '{print $1}')
    PROGRESS=$(echo "$STATUS" | awk '{print $2}')
    ERROR=$(echo "$STATUS" | cut -d' ' -f3-)
    if [[ "$STATE" == "installed" ]]; then
      ELAPSED=$(( $(date +%s) - DOWNLOAD_START ))
      echo "    [OK]   model installed after ${ELAPSED}s"
      break
    fi
    if [[ -n "$ERROR" && "$STATE" != "downloading" ]]; then
      echo "    [FAIL] model download error: $ERROR"
      docker logs --tail 80 "$CONTAINER" 2>&1 | tail -40
      FAIL=1
      break
    fi
    if (( i % 6 == 0 )); then
      echo "    ... status=$STATE progress=${PROGRESS}% (${i}*5s)"
    fi
    sleep 5
  done
  if [[ "$FAIL" -ne 0 ]]; then exit 1; fi
  if [[ "$STATE" != "installed" ]]; then
    echo "!! Model not installed after 10 min (last status: $STATE, progress: ${PROGRESS}%)"
    docker logs --tail 80 "$CONTAINER" 2>&1 | tail -40
    exit 1
  fi
fi

# 2a) Restart the worker so it picks up the new config. We do this BEFORE
#     submitting the task so the task's config_snapshot is taken by a worker
#     that has re-read the config from the DB.
echo "==> Restart worker to pick up new config"
# Find the worker PID by exact cmdline match. We can't use a substring grep
# here because the bash subshell that runs this loop has the string
# "app.worker_process" in its OWN command line (in the grep pattern), and
# would match itself. Match the exact start of the cmdline instead.
WORKER_PID=$(docker exec -u root "$CONTAINER" python3 -c '
import os
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    try:
        with open(f"/proc/{entry}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
    # The worker is exactly "python3 -m app.worker_process"
    if cmd == "python3 -m app.worker_process":
        print(entry)
        break
')
if [[ -z "$WORKER_PID" ]]; then
  echo "    [FAIL] worker PID not found"
  exit 1
fi
# The slim image has no `kill` binary; use python's os.kill to send SIGTERM.
docker exec -u root "$CONTAINER" python3 -c "import os, sys; os.kill(int(sys.argv[1]), 15)" "$WORKER_PID" 2>/dev/null || true
# Wait for the old worker to exit and the new one to start (startsecs=5)
sleep 8
echo "    [OK]   worker restarted (killed pid $WORKER_PID, new worker has fresh config cache)"

# 3) Submit the fixture as a manual task.
echo "==> Submit task: /data/$FIXTURE_NAME"
TASK_RESP=$(curl -sS -X POST "$API/api/tasks/manual" -H "Content-Type: application/json" \
  -d "{\"file_path\": \"/data/$FIXTURE_NAME\"}")
TASK_ID=$(echo "$TASK_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['task']['id'])")
echo "    [OK]   task_id=$TASK_ID"

# 4) Poll the task until completion (up to 5 min for short audio + tiny model).
echo "==> Wait for task to complete (up to 5 min)"
TASK_START=$(date +%s)
for i in $(seq 1 60); do
  TASK=$(curl -sS "$API/api/tasks/$TASK_ID")
  STATUS=$(echo "$TASK" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
  STAGE=$(echo "$TASK" | python3 -c "import json,sys; print(json.load(sys.stdin).get('stage', ''))")
  ELAPSED=$(( $(date +%s) - TASK_START ))
  case "$STATUS" in
    completed|done)
      echo "    [OK]   task completed in ${ELAPSED}s (final stage: $STAGE)"
      break
      ;;
    failed)
      echo "    [FAIL] task failed in ${ELAPSED}s (stage: $STAGE)"
      echo "$TASK" | python3 -m json.tool | head -30
      docker logs --tail 80 "$CONTAINER" 2>&1 | tail -40
      exit 1
      ;;
    pending|queued|processing)
      if (( i % 6 == 0 )); then
        echo "    ... $STATUS / $STAGE (${ELAPSED}s)"
      fi
      sleep 5
      ;;
    *)
      echo "    [FAIL] unknown status: $STATUS"
      exit 1
      ;;
  esac
done
if [[ "$STATUS" != "completed" && "$STATUS" != "done" ]]; then
  echo "!! Task did not complete within 5 min (last status: $STATUS)"
  docker logs --tail 80 "$CONTAINER" 2>&1 | tail -40
  exit 1
fi

# 5) Pull the subtitle and verify it contains the expected Chinese phrases.
echo "==> Validate subtitle content"
SUB=$(curl -sS "$API/api/tasks/$TASK_ID/subtitle")
SRT=$(echo "$SUB" | python3 -c "import json,sys; print(json.load(sys.stdin)['content'])")
SRT_PATH=$(echo "$SUB" | python3 -c "import json,sys; print(json.load(sys.stdin)['path'])")
echo "    subtitle file: $SRT_PATH"
echo "    --- first 30 lines of srt ---"
echo "$SRT" | head -30 | sed 's/^/      /'
echo "    --- end ---"
MISSING=()
for phrase in "${EXPECTED_PHRASES[@]}"; do
  # Each phrase may be "A|B" — accept either form (e.g. simplified vs traditional).
  if echo "$SRT" | grep -qE "$phrase"; then
    echo "    [OK]   found expected phrase: $phrase"
  else
    echo "    [FAIL] missing expected phrase: $phrase"
    MISSING+=("$phrase")
  fi
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "==> FAILED. Missing Chinese phrases: ${MISSING[*]}"
  exit 1
fi

# 6) Sanity check: at least one CJK character is present and the srt has a
#    proper SRT structure (timestamp → text blocks).
echo "==> Validate SRT structure"
STRUCT_OK=$(echo "$SRT" | python3 -c "
import re, sys
text = sys.stdin.read()
# SRT cue: index line, timestamp line '-->' line, text line(s), blank line
cues = re.findall(r'^\d+\s*\n\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}\s*\n(.+?)(?=\n\s*\n|\Z)', text, re.MULTILINE | re.DOTALL)
cjk = re.findall(r'[\u4e00-\u9fff]', text)
print(f'cues={len(cues)} cjk_chars={len(cjk)}')
")
echo "    $STRUCT_OK"
if ! echo "$STRUCT_OK" | grep -qE "cues=[1-9][0-9]*"; then
  echo "    [FAIL] no valid SRT cues found"
  exit 1
fi
if ! echo "$STRUCT_OK" | grep -qE "cjk_chars=[1-9][0-9]*"; then
  echo "    [FAIL] no CJK characters in subtitle"
  exit 1
fi
echo "    [OK]   SRT structure valid (>=1 cue, >=1 CJK char)"

# 7) Spot-check quality report was attached.
QUALITY=$(curl -sS "$API/api/tasks/$TASK_ID" | python3 -c "
import json, sys
t = json.load(sys.stdin)
rp = t.get('result_payload') or {}
qr = (rp.get('quality_report') or {})
print(f\"score={qr.get('score', '?')} issues={len(qr.get('issues', []))} suspect={qr.get('is_suspect', '?')}\")
")
echo "==> quality_checker: $QUALITY"

echo "==> All ASR smoke checks passed. Container: $CONTAINER (will be cleaned up)"
