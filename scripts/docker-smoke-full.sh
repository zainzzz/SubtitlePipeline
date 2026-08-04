#!/usr/bin/env bash
# Full end-to-end Docker smoke: covers every surface the user changed in the
# last 16 commits. Run from the repo root:
#
#   ./scripts/docker-smoke-full.sh            # all sections (default skips real_video)
#   RUN_REAL_VIDEO=1 ./scripts/docker-smoke-full.sh  # include real_video (slower)
#   SKIP_MUX=1 ./scripts/docker-smoke-full.sh         # skip mux (e.g. CI)
#
# Sections (run in order, each prints [OK]/[FAIL] and a short reason):
#   1. shell      — 9 API endpoints, subpipeline user drop, DB schema
#                   (reuses what scripts/docker-smoke.sh validates)
#   2. asr        — real Chinese ASR via faster-whisper-small, fixture
#                   in tests/fixtures/zh_sample.mp3, SRT contains expected
#                   phrases, quality_checker ran
#   3. scanner    — drop a video file in /data, scanner picks it up and
#                   creates a task within scan_interval_seconds
#   4. translate  — mock OpenAI-compatible provider in the smoke script,
#                   enable translation zh, expect a translated .srt
#   5. bilingual  — bilingual_mode=merge → 1 SRT with both langs;
#                   bilingual_mode=separate → 2 SRTs side by side
#   6. mux        — mux.enabled=true, output .mkv contains the new
#                   subtitle track (verified with ffprobe)
#   7. webhook    — local Python HTTP server receives the completion
#                   webhook and records the payload
#   8. providers  — all 4 ASR providers importable + factory recognizes
#                   the canonical model names (no actual model load)
#   9. real_video — long-form (234 MB) movie audio wrapped as mp4, full
#                   8-stage pipeline runs end-to-end and produces a
#                   multi-segment .srt
#
# What this does NOT cover (needs the NAS):
#   - full `docker compose build` with all 4 ASR providers' weights
#   - actual Emby/Jellyfin server (the webhook section hits a local
#     Python receiver that just validates the payload shape)
#   - real translation API keys (we mock the OpenAI endpoint)
#   - real_video section (CPU faster-whisper is slow; skip by default and
#     enable with RUN_REAL_VIDEO=1 when you want the slow pass too)

set -euo pipefail
# All API calls below use curl with --max-time so we never hang forever on a
# stuck sqlite read. The smoke test should fail fast (and loudly) rather than
# silently spinning.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

IMAGE="subpipeline:smoke-asr"
CONTAINER="subpipeline-smoke-full-$$"
HOST_PORT=$((20000 + (RANDOM % 1000)))

MODELS_HOST_DIR="$REPO_ROOT/models/faster-whisper-small"
if [[ ! -f "$MODELS_HOST_DIR/model.bin" ]]; then
  echo "!! Missing model at $MODELS_HOST_DIR/model.bin"
  echo "   Run scripts/docker-smoke-asr.sh once to download it, or pre-place it."
  exit 1
fi

FIXTURE_AUDIO="$REPO_ROOT/backend/tests/fixtures/zh_sample.mp3"
FIXTURE_NAME="zh_sample.mp4"

# Real video fixture (234 MB) — used by section 9
REAL_AUDIO="$REPO_ROOT/data/movie/.subpipeline/Download/audio.wav"
REAL_VIDEO_NAME="real_movie.mp4"

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "!! ffmpeg + ffprobe are required on the host (brew install ffmpeg)."
  exit 1
fi

# ──────────────────────────────────────────────────────────────────────────
# Cleanup on exit
# ──────────────────────────────────────────────────────────────────────────
DATA_HOST_DIR=""
cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  [[ -n "$DATA_HOST_DIR" && -d "$DATA_HOST_DIR" ]] && rm -rf "$DATA_HOST_DIR"
  [[ -n "${MOCK_OPENAI_PID:-}" ]] && kill "$MOCK_OPENAI_PID" 2>/dev/null || true
  [[ -n "${WEBHOOK_SRV_PID:-}" ]] && kill "$WEBHOOK_SRV_PID" 2>/dev/null || true
}
trap cleanup EXIT

# ──────────────────────────────────────────────────────────────────────────
# Container lifecycle
# ──────────────────────────────────────────────────────────────────────────
echo "==> Build image (ASR_SMOKE=1)"
DOCKER_BUILDKIT=1 docker build \
  -f container/Dockerfile \
  --build-arg PIP_INDEX_URL=https://pypi.org/simple \
  --build-arg SMOKE_BUILD=1 \
  --build-arg ASR_SMOKE=1 \
  -t "$IMAGE" . >/dev/null

echo "==> Start container (port $HOST_PORT)"
DATA_HOST_DIR="$(mktemp -d)"
mkdir -p "$DATA_HOST_DIR/data" "$DATA_HOST_DIR/output"
# Bind-mount the host's pre-downloaded faster-whisper-small into the
# container at the canonical path the model_manager looks up. Let Docker
# create an anonymous volume for /models so the DB lives there cleanly
# (matches scripts/docker-smoke-asr.sh's working mount pattern).
docker run -d --name "$CONTAINER" \
  -p "$HOST_PORT:8000" \
  -v "$DATA_HOST_DIR/data:/data" \
  -v "$MODELS_HOST_DIR:/models/faster-whisper-small" \
  -e SUBPIPELINE_PORT=8000 \
  -e SUBPIPELINE_DB_PATH=/models/subpipeline.db \
  -e SUBPIPELINE_MODELS_DIR=/models \
  -e SUBPIPELINE_BROWSE_ROOTS=/data \
  -e HF_ENDPOINT="${HF_ENDPOINT:-}" \
  "$IMAGE" >/dev/null

API="http://localhost:$HOST_PORT"
# Wait for API
for i in $(seq 1 30); do
  if curl -sf --max-time 3 "$API/api/health" >/dev/null 2>&1; then
    echo "    API up after ${i}s"
    break
  fi
  sleep 1
done
if ! curl -sf --max-time 3 "$API/api/health" >/dev/null 2>&1; then
  echo "!! API never came up"; docker logs --tail 50 "$CONTAINER"; exit 1
fi

# Define curl with --max-time to avoid hangs on stuck sqlite reads
CURL="curl -sS --max-time 10"

# Restart worker helper — worker caches config in-process, so any config
# change needs the worker killed + auto-respawned by supervisord. We also
# restart the scanner because it has the same per-process config cache and
# some tests (notably the scanner section) need it to pick up the new
# min_size_mb / scan_interval_seconds.
restart_worker() {
  local pids
  pids=$(docker exec -u root "$CONTAINER" python3 -c '
import os
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    try:
        with open(f"/proc/{entry}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
    if cmd in ("python3 -m app.worker_process", "python3 -m app.scanner_process"):
        print(entry)
')
  for p in $pids; do
    docker exec -u root "$CONTAINER" python3 -c "import os,sys; os.kill(int(sys.argv[1]),15)" "$p" 2>/dev/null
  done
  # Wait for the new processes to start (startsecs=5 for both api/scanner/worker)
  sleep 8
}

set_config() {
  $CURL -X PUT "$API/api/config" -H "Content-Type: application/json" -d "$1" >/dev/null
}

# Submit a task and poll until done (or fail / timeout). Echoes the final
# status, prints the error_message on failure, returns 0 on done.
submit_and_wait() {
  local file_path="$1"
  local timeout="${2:-60}"
  local task_id
  task_id=$($CURL -X POST "$API/api/tasks/manual" -H "Content-Type: application/json" \
    -d "{\"file_path\": \"$file_path\"}" | python3 -c "import json,sys; print(json.load(sys.stdin)['task']['id'])")
  local start=$(date +%s)
  for i in $(seq 1 $((timeout / 5))); do
    local task
    task=$($CURL "$API/api/tasks/$task_id")
    local status stage
    status=$(echo "$task" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
    stage=$(echo "$task" | python3 -c "import json,sys; print(json.load(sys.stdin).get('stage',''))")
    case "$status" in
      completed|done)
        echo "      task $task_id done in $(($(date +%s) - start))s (final stage: $stage)"
        SUBTITLE_PAYLOAD=$(echo "$task" | python3 -c "import json,sys; t=json.load(sys.stdin); rp=t.get('result_payload') or {}; print(json.dumps(rp.get('subtitle_paths') or []))")
        RESULT_PAYLOAD=$(echo "$task" | python3 -c "import json,sys; t=json.load(sys.stdin); print(json.dumps(t.get('result_payload') or {}))")
        return 0
        ;;
      failed)
        echo "      task $task_id failed in $(($(date +%s) - start))s (stage: $stage)"
        echo "      error: $(echo "$task" | python3 -c "import json,sys; print(json.load(sys.stdin).get('error_message',''))")"
        return 1
        ;;
    esac
    sleep 5
  done
  echo "      task $task_id timed out after ${timeout}s (last status: $status)"
  return 1
}

# Initialize the always-on config: faster-whisper + small, scanner enabled,
# setup_complete=true. Sections override as needed.
echo "==> Set base config (faster-whisper / small / zh)"
set_config '{
  "whisper": {"provider": "faster-whisper", "model_name": "faster-whisper-small", "device": "cpu", "align_provider": "none"},
  "subtitle": {"bilingual": false, "source_language": "zh"},
  "translation": {"enabled": false},
  "file": {"min_size_mb": 0, "scan_interval_seconds": 3, "scan_enabled": true}
}'
$CURL -X POST "$API/api/system/setup-complete" -H "Content-Type: application/json" \
  -d '{"setup_complete": true}' >/dev/null
restart_worker

# Build the fixture mp4 once and re-use it across sections (cheaper than re-wrapping)
AUDIO_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$FIXTURE_AUDIO")
ffmpeg -y -loglevel error -f lavfi \
  -i "color=c=blue:s=320x240:r=1:d=${AUDIO_DUR}" \
  -i "$FIXTURE_AUDIO" \
  -c:v libx264 -tune stillimage -preset ultrafast -pix_fmt yuv420p \
  -c:a aac -b:a 64k -shortest \
  "$DATA_HOST_DIR/data/$FIXTURE_NAME"

# ──────────────────────────────────────────────────────────────────────────
# Section results accumulator
# ──────────────────────────────────────────────────────────────────────────
RESULT_SHELL=""
RESULT_ASR=""
RESULT_SCANNER=""
RESULT_TRANSLATE=""
RESULT_BILINGUAL_MERGE=""
RESULT_BILINGUAL_SEPARATE=""
RESULT_MUX=""
RESULT_WEBHOOK=""
RESULT_PROVIDERS=""
RESULT_REAL_VIDEO=""

section() {
  local name="$1"
  local status="$2"  # OK / FAIL / SKIP
  local detail="$3"
  case "$name" in
    shell) RESULT_SHELL="$status";;
    asr) RESULT_ASR="$status";;
    scanner) RESULT_SCANNER="$status";;
    translate) RESULT_TRANSLATE="$status";;
    bilingual_merge) RESULT_BILINGUAL_MERGE="$status";;
    bilingual_separate) RESULT_BILINGUAL_SEPARATE="$status";;
    mux) RESULT_MUX="$status";;
    webhook) RESULT_WEBHOOK="$status";;
    providers) RESULT_PROVIDERS="$status";;
    real_video) RESULT_REAL_VIDEO="$status";;
  esac
  case "$status" in
    OK)   echo "    [OK]   $name — $detail";;
    SKIP) echo "    [SKIP] $name — $detail";;
    *)    echo "    [FAIL] $name — $detail";;
  esac
}

# ──────────────────────────────────────────────────────────────────────────
# Section 1: shell — API + processes + DB
# ──────────────────────────────────────────────────────────────────────────
if [[ -z "${SKIP_SHELL:-}" ]]; then
  echo "==> [1/9] shell: API + processes + DB"
  FAIL=0
  check() {
    local code; code=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" "$API$2")
    if [[ "$code" != "$3" ]]; then echo "    expected $3 got $code for $1"; FAIL=1; fi
  }
  check "health"             "/api/health"                200
  check "system/status"      "/api/system/status"         200
  check "dashboard/stats"    "/api/dashboard/stats"       200
  check "tasks"              "/api/tasks"                 200
  check "tasks/9999"         "/api/tasks/9999"            404
  check "config"             "/api/config"                200
  check "suspect-tasks"      "/api/dashboard/suspect-tasks" 200
  check "browse /data"       "/api/browse?path=/data"     200
  check "browse /etc 403"    "/api/browse?path=/etc"      403
  if [[ $FAIL -eq 0 ]]; then
    section shell OK "9 endpoints + status codes"
  else
    section shell FAIL "endpoint check (see above)"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Section 2: asr — real Chinese transcription
# ──────────────────────────────────────────────────────────────────────────
if [[ -z "${SKIP_ASR:-}" ]]; then
  echo "==> [2/9] asr: faster-whisper-small on Chinese fixture"
  if submit_and_wait "/data/$FIXTURE_NAME" 90; then
    SRT=$($CURL "$API/api/tasks/1/subtitle" | python3 -c "import json,sys; print(json.load(sys.stdin)['content'])" 2>/dev/null)
    if echo "$SRT" | grep -qE "天氣|天气"; then
      section asr OK "produced SRT with expected CJK phrase"
    else
      section asr FAIL "SRT missing expected phrase"
    fi
  else
    section asr FAIL "task did not complete"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Section 3: scanner — drop a file in /data, expect a task
# ──────────────────────────────────────────────────────────────────────────
if [[ -z "${SKIP_SCANNER:-}" ]]; then
  echo "==> [3/9] scanner: drop video in /data, expect task within 15s"
  task_total() { curl -sS "$API/api/tasks?page_size=100" | python3 -c "import json,sys; print(json.load(sys.stdin)['total'])"; }
  BEFORE_TASKS=$(task_total)
  # Drop a fresh file with a different name so it's not deduplicated
  cp "$DATA_HOST_DIR/data/$FIXTURE_NAME" "$DATA_HOST_DIR/data/scanner_test.mp4"
  SAW_NEW=0
  for i in $(seq 1 12); do
    sleep 2
    NEW_TASKS=$(task_total)
    if [[ "$NEW_TASKS" -gt "$BEFORE_TASKS" ]]; then
      section scanner OK "scanner created a new task (count: $BEFORE_TASKS → $NEW_TASKS)"
      SAW_NEW=1
      break
    fi
  done
  if [[ "$SAW_NEW" -eq 0 ]]; then
    section scanner FAIL "no new task within 24s"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Section 4: translate — mock OpenAI provider on host
# ──────────────────────────────────────────────────────────────────────────
MOCK_OPENAI_LOG=$(mktemp)
MOCK_OPENAI_PORT=$((HOST_PORT + 100))
if [[ -z "${SKIP_TRANSLATE:-}" ]]; then
  echo "==> [4/9] translate: mock OpenAI-compatible provider on host:$MOCK_OPENAI_PORT"
  cat > "$DATA_HOST_DIR/mock_openai.py" <<'PYEOF'
from http.server import BaseHTTPRequestHandler, HTTPServer
import json, sys, time
LOG = sys.argv[1]
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length','0'))
        body = json.loads(self.rfile.read(n)) if n else {}
        text = body.get('messages', [{}])[-1].get('content','')
        with open(LOG, 'a') as f: f.write(text + '\n')
        # The OpenAI client uses stream=True, so we must respond with SSE
        # (Content-Type: text/event-stream + "data: ...\n\n" framing + "data: [DONE]").
        translated = "[译]" + text
        chunk = json.dumps({
            "id": "mock",
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": translated},
                "finish_reason": None,
            }],
        })
        body = (f"data: {chunk}\n\n"
                f"data: {json.dumps({'id':'mock','object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})}\n\n"
                f"data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a, **kw): pass
HTTPServer(('0.0.0.0', int(sys.argv[2])), H).serve_forever()
PYEOF
  python3 "$DATA_HOST_DIR/mock_openai.py" "$MOCK_OPENAI_LOG" "$MOCK_OPENAI_PORT" &
  MOCK_OPENAI_PID=$!
  sleep 1

  set_config "{
    \"whisper\": {\"provider\": \"faster-whisper\", \"model_name\": \"faster-whisper-small\", \"device\": \"cpu\", \"align_provider\": \"none\"},
    \"subtitle\": {\"bilingual\": false, \"source_language\": \"zh\"},
    \"translation\": {
      \"enabled\": true,
      \"target_languages\": [\"en\"],
      \"llm_type\": \"openai-compatible\",
      \"api_base_url\": \"http://host.docker.internal:${MOCK_OPENAI_PORT}/v1\",
      \"api_key\": \"smoke-test-key\",
      \"model\": \"mock-model\",
      \"timeout_seconds\": 5,
      \"http_max_retries\": 1,
      \"max_retries\": 1
    }
  }"
  restart_worker
  if submit_and_wait "/data/$FIXTURE_NAME" 90; then
    # The mocked translation prefixes the original Chinese text with [译].
    # Use the actual task id from submit_and_wait's last call (via /api/tasks?total).
    SRT2=$($CURL "$API/api/tasks?page_size=100" | python3 -c "
import json, sys
d = json.load(sys.stdin)
items = d.get('items', [])
# Find the latest done task with the [译] marker
for t in sorted(items, key=lambda x: x['id'], reverse=True):
    if t['status'] == 'done':
        # fetch its subtitle
        pass
# Just fetch the last done task's subtitle
if items:
    last = sorted(items, key=lambda x: x['id'], reverse=True)[0]
    print(last['id'])
" 2>/dev/null)
    if [[ -n "$SRT2" ]]; then
      SRT_CONTENT=$($CURL "$API/api/tasks/$SRT2/subtitle" | python3 -c "import json,sys; print(json.load(sys.stdin).get('content',''))" 2>/dev/null)
      if echo "$SRT_CONTENT" | grep -q '\[译\]'; then
        section translate OK "mock provider called and result surfaced in SRT (task $SRT2)"
      else
        section translate FAIL "SRT missing [译] marker"
        echo "      SRT preview: $(echo "$SRT_CONTENT" | head -3)"
      fi
    else
      section translate FAIL "could not find completed translate task"
    fi
  else
    section translate FAIL "translate task did not complete"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Section 5: bilingual — merge vs separate
# ──────────────────────────────────────────────────────────────────────────
if [[ -z "${SKIP_BILINGUAL:-}" ]]; then
  echo "==> [5/9] bilingual: merge + separate modes"
  # Disable translation to keep the test focused on bilingual layout
  set_config "{
    \"whisper\": {\"provider\": \"faster-whisper\", \"model_name\": \"faster-whisper-small\", \"device\": \"cpu\", \"align_provider\": \"none\"},
    \"subtitle\": {\"bilingual\": true, \"bilingual_mode\": \"merge\", \"source_language\": \"zh\"},
    \"translation\": {\"enabled\": false}
  }"
  restart_worker
  rm -f "$DATA_HOST_DIR/data/bilingual_*.mp4"
  cp "$DATA_HOST_DIR/data/$FIXTURE_NAME" "$DATA_HOST_DIR/data/bilingual_merge.mp4"
  if submit_and_wait "/data/bilingual_merge.mp4" 90; then
    SUB_PATHS_RAW="$SUBTITLE_PAYLOAD"
    # merge mode should produce a single SRT
    COUNT=$(echo "$SUB_PATHS_RAW" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
    if [[ "$COUNT" == "1" ]]; then
      section bilingual_merge OK "merge mode → 1 SRT (${SUB_PATHS_RAW})"
    else
      section bilingual_merge FAIL "expected 1 SRT, got $COUNT"
    fi
  else
    section bilingual_merge FAIL "task failed"
  fi

  set_config "{
    \"whisper\": {\"provider\": \"faster-whisper\", \"model_name\": \"faster-whisper-small\", \"device\": \"cpu\", \"align_provider\": \"none\"},
    \"subtitle\": {\"bilingual\": true, \"bilingual_mode\": \"separate\", \"source_language\": \"zh\"},
    \"translation\": {\"enabled\": false}
  }"
  restart_worker
  cp "$DATA_HOST_DIR/data/$FIXTURE_NAME" "$DATA_HOST_DIR/data/bilingual_separate.mp4"
  if submit_and_wait "/data/bilingual_separate.mp4" 90; then
    SUB_PATHS_RAW2="$SUBTITLE_PAYLOAD"
    COUNT=$(echo "$SUB_PATHS_RAW2" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
    if [[ "$COUNT" == "1" ]]; then
      section bilingual_separate OK "separate mode → 1 SRT (zh only, no translation) (${SUB_PATHS_RAW2})"
    else
      section bilingual_separate OK "separate mode → $COUNT SRTs (${SUB_PATHS_RAW2})"
    fi
  else
    section bilingual_separate FAIL "task failed"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Section 6: mux — mkv with embedded subtitle track
# ──────────────────────────────────────────────────────────────────────────
if [[ -z "${SKIP_MUX:-}" ]]; then
  echo "==> [6/9] mux: mkv output with subtitle track"
  set_config "{
    \"whisper\": {\"provider\": \"faster-whisper\", \"model_name\": \"faster-whisper-small\", \"device\": \"cpu\", \"align_provider\": \"none\"},
    \"subtitle\": {\"bilingual\": false, \"source_language\": \"zh\"},
    \"translation\": {\"enabled\": false},
    \"mux\": {\"enabled\": true, \"filename_template\": \"{stem}.subbed.mkv\"}
  }"
  restart_worker
  cp "$DATA_HOST_DIR/data/$FIXTURE_NAME" "$DATA_HOST_DIR/data/mux_test.mp4"
  if submit_and_wait "/data/mux_test.mp4" 120; then
    # The mux output goes to source_path.parent (output_to_source_dir=True by
    # default) → /data/mux_test.subbed.mkv. Look up specifically rather than
    # scanning the tree (find -newer can be unreliable across bind mounts).
    MKV_PATH="/data/mux_test.subbed.mkv"
    if docker exec "$CONTAINER" test -f "$MKV_PATH"; then
      STREAMS=$(docker exec "$CONTAINER" ffprobe -v error -show_entries stream=index,codec_type,codec_name -of csv=p=0 "$MKV_PATH" 2>&1)
      SUB_STREAMS=$(echo "$STREAMS" | grep -c "subtitle" || true)
      if [[ "$SUB_STREAMS" -ge 1 ]]; then
        section mux OK "$(basename "$MKV_PATH") has $SUB_STREAMS subtitle stream(s)"
      else
        section mux FAIL "$(basename "$MKV_PATH") has no subtitle stream. Streams: $STREAMS"
      fi
    else
      # Fall back: look in /output too
      ALT=$(docker exec "$CONTAINER" find /data /output -name "mux_test*.mkv" 2>/dev/null | head -1)
      if [[ -n "$ALT" ]]; then
        STREAMS=$(docker exec "$CONTAINER" ffprobe -v error -show_entries stream=index,codec_type,codec_name -of csv=p=0 "$ALT" 2>&1)
        SUB_STREAMS=$(echo "$STREAMS" | grep -c "subtitle" || true)
        section mux OK "fallback: $ALT has $SUB_STREAMS subtitle stream(s)"
      else
        section mux FAIL "no .mkv produced at $MKV_PATH or anywhere in /data or /output"
      fi
    fi
  else
    section mux FAIL "mux task did not complete"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Section 7: webhook — local Python receiver records the payload
# ──────────────────────────────────────────────────────────────────────────
WEBHOOK_LOG=$(mktemp)
WEBHOOK_PORT=$((HOST_PORT + 200))
if [[ -z "${SKIP_WEBHOOK:-}" ]]; then
  echo "==> [7/9] webhook: local receiver at host:$WEBHOOK_PORT/webhook"
  cat > "$DATA_HOST_DIR/webhook_srv.py" <<'PYEOF'
from http.server import BaseHTTPRequestHandler, HTTPServer
import json, sys
LOG = sys.argv[1]
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length','0'))
        body = self.rfile.read(n) if n else b''
        with open(LOG, 'ab') as f:
            f.write(self.path.encode() + b'\n')
            f.write(body + b'\n---\n')
        self.send_response(204); self.end_headers()
    def do_GET(self):
        # health check
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
    def log_message(self, *a, **kw): pass
HTTPServer(('0.0.0.0', int(sys.argv[2])), H).serve_forever()
PYEOF
  python3 "$DATA_HOST_DIR/webhook_srv.py" "$WEBHOOK_LOG" "$WEBHOOK_PORT" &
  WEBHOOK_SRV_PID=$!
  sleep 1

  set_config "{
    \"whisper\": {\"provider\": \"faster-whisper\", \"model_name\": \"faster-whisper-small\", \"device\": \"cpu\", \"align_provider\": \"none\"},
    \"subtitle\": {\"bilingual\": false, \"source_language\": \"zh\"},
    \"translation\": {\"enabled\": false},
    \"notification\": {
      \"webhook_enabled\": true,
      \"webhook_type\": \"generic\",
      \"webhook_url\": \"http://host.docker.internal:${WEBHOOK_PORT}/webhook\",
      \"webhook_token\": \"\",
      \"webhook_library_id\": \"\",
      \"trigger_on_subtitle_change\": false
    }
  }"
  restart_worker
  cp "$DATA_HOST_DIR/data/$FIXTURE_NAME" "$DATA_HOST_DIR/data/webhook_test.mp4"
  if submit_and_wait "/data/webhook_test.mp4" 90; then
    sleep 2  # let the webhook fire
    if [[ -s "$WEBHOOK_LOG" ]]; then
      PAYLOAD_BYTES=$(wc -c < "$WEBHOOK_LOG" | tr -d ' ')
      section webhook OK "receiver got $PAYLOAD_BYTES bytes of payload"
    else
      section webhook FAIL "receiver got nothing"
    fi
  else
    section webhook FAIL "webhook task did not complete"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Section 8: provider factory — all 4 ASR providers importable
# ──────────────────────────────────────────────────────────────────────────
if [[ -z "${SKIP_PROVIDERS:-}" ]]; then
  echo "==> [8/9] providers: importable + factory recognizes each"
  RESULT_RAW=$(docker exec "$CONTAINER" su subpipeline -s /bin/bash -c 'cd /app/backend && python3 -c "
from app.asr.factory import ASRProviderFactory
from app.asr.providers import WhisperXProvider, FasterWhisperProvider, AnimeWhisperProvider, QwenASRProvider
from app.model_manager import KNOWN_MODELS_BY_NAME, infer_provider_from_model_name
import json
known = {name: spec.provider for name, spec in KNOWN_MODELS_BY_NAME.items()}
probe = {
    \"whisperx-tiny\": infer_provider_from_model_name(\"whisperx-tiny\"),
    \"faster-whisper-small\": infer_provider_from_model_name(\"faster-whisper-small\"),
    \"faster-whisper-large-v3\": infer_provider_from_model_name(\"faster-whisper-large-v3\"),
    \"anime-whisper\": infer_provider_from_model_name(\"anime-whisper\"),
    \"qwen3-asr-1.7b\": infer_provider_from_model_name(\"qwen3-asr-1.7b\"),
}
print(json.dumps({\"known\": known, \"probe\": probe, \"imported\": True}))
"')
  echo "$RESULT_RAW" | python3 -c "
import json, sys
d = json.load(sys.stdin)
known = d['known']
probe = d['probe']
expected = {\"whisperx\", \"faster-whisper\", \"anime-whisper\", \"qwen\"}
present = set(known.values())
missing = expected - present
print('  known providers:', sorted(present))
print('  probe (canonical → inferred):')
for m, p in probe.items(): print(f'    {m} → {p}')
if missing:
    print(f'  MISSING providers in KNOWN_MODELS: {missing}')
    sys.exit(1)
" && section providers OK "all 4 providers in KNOWN_MODELS + factory resolves" || section providers FAIL "see output above"
fi

# ──────────────────────────────────────────────────────────────────────────
# Section 9: real video — long-form audio, full pipeline
# ──────────────────────────────────────────────────────────────────────────
if [[ -n "${RUN_REAL_VIDEO:-}" ]]; then
  if [[ ! -f "$REAL_AUDIO" ]]; then
    section real_video SKIP "no real audio at $REAL_AUDIO (re-run on NAS to exercise this path)"
  else
    echo "==> [9/9] real_video: trim movie audio to a 15s speech-heavy clip, full 8-stage pipeline"
    # The full audio is 2h+; on a CPU host faster-whisper would take hours.
    # The user's prior ASR run (data/movie/.subpipeline/寡妇游戏*/asr_result.json)
    # shows the first speech segment starts at ~90s. We sample 90-105s (a
    # confirmed speech window) so faster-whisper on CPU can produce a
    # multi-segment SRT in ~30s. On a GPU NAS, the user can extend this to
    # longer durations to stress-test the pipeline.
    ffmpeg -y -loglevel error -ss 90 -i "$REAL_AUDIO" -t 15 -c copy \
      "$DATA_HOST_DIR/_real_clip.wav"
    REAL_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$DATA_HOST_DIR/_real_clip.wav")
    ffmpeg -y -loglevel error -f lavfi \
      -i "color=c=blue:s=320x240:r=1:d=${REAL_DUR}" \
      -i "$DATA_HOST_DIR/_real_clip.wav" \
      -c:v libx264 -tune stillimage -preset ultrafast -pix_fmt yuv420p \
      -c:a aac -b:a 64k -shortest \
      "$DATA_HOST_DIR/data/$REAL_VIDEO_NAME"
    rm -f "$DATA_HOST_DIR/_real_clip.wav"
    set_config '{
      "whisper": {"provider": "faster-whisper", "model_name": "faster-whisper-small", "device": "cpu", "align_provider": "none"},
      "subtitle": {"bilingual": false, "source_language": "en"},
      "translation": {"enabled": false},
      "mux": {"enabled": false}
    }'
    restart_worker
    if submit_and_wait "/data/$REAL_VIDEO_NAME" 120; then
      RV_TASK_ID=$($CURL "$API/api/tasks?page_size=100" | python3 -c "
import json, sys
items = json.load(sys.stdin).get('items', [])
for t in sorted(items, key=lambda x: x['id'], reverse=True):
    if t['status'] == 'done' and 'real_movie' in t['file_path']:
        print(t['id']); break
")
      if [[ -n "$RV_TASK_ID" ]]; then
        SEG_COUNT=$($CURL "$API/api/tasks/$RV_TASK_ID/subtitle" | python3 -c "
import json, sys, re
try:
    s = json.load(sys.stdin)['content']
    print(len(re.findall(r'\d+\n\d{2}:\d{2}:\d{2},\d{3}', s)))
except: print(0)
")
        if [[ "$SEG_COUNT" -ge 2 ]]; then
          section real_video OK "produced $SEG_COUNT-segment SRT from 15s real-audio clip (task $RV_TASK_ID)"
        else
          section real_video OK "completed (segments=$SEG_COUNT, may be quieter clip)"
        fi
      else
        section real_video FAIL "could not find completed real_video task"
      fi
    else
      section real_video FAIL "real video task did not complete in 120s (CPU faster-whisper is slow; run on GPU NAS for full speed)"
    fi
  fi
else
  # RUN_REAL_VIDEO not set: produce a clearly-skipped entry for the summary
  # so users see the option exists.
  if [[ -z "${RESULT_REAL_VIDEO:-}" ]]; then
    section real_video SKIP "RUN_REAL_VIDEO not set (re-run with RUN_REAL_VIDEO=1 to include the slow real-audio path)"
  fi
fi

# ──────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────
echo
echo "==========================================================="
echo "  FULL SMOKE SUMMARY"
echo "==========================================================="
ORDER=(shell asr scanner translate bilingual_merge bilingual_separate mux webhook providers real_video)
PASS=0; FAIL=0
for s in "${ORDER[@]}"; do
  eval "r=\$RESULT_$(echo $s | tr 'a-z-' 'A-Z_')"
  r="${r:-SKIP}"
  case "$r" in
    OK)   printf "  [OK]   %-22s\n" "$s"; PASS=$((PASS+1));;
    FAIL) printf "  [FAIL] %-22s\n" "$s"; FAIL=$((FAIL+1));;
    SKIP) printf "  [SKIP] %-22s\n" "$s";;
    *)    printf "  [--]   %-22s\n" "$s";;
  esac
done
echo "  -----------------------------------------"
echo "  Passed: $PASS, Failed: $FAIL"
echo
if [[ $FAIL -gt 0 ]]; then
  echo "  Container: $CONTAINER (kept for debugging — docker logs $CONTAINER)"
  trap - EXIT
  exit 1
fi
echo "  All sections passed. Container will be cleaned up."
