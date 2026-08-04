# NAS First-Deploy Checklist

> **Use this the first time you stand up SubtitlePipeline on the NAS.** The
> host-side Docker smoke tests (`scripts/docker-smoke.sh`,
> `scripts/docker-smoke-asr.sh`, `scripts/docker-smoke-full.sh`) only cover
> non-ML paths and CPU faster-whisper. This checklist covers everything that
> can only be verified on the real NAS box: full ML build, real Emby/Jellyfin
> webhook target, real translation API, GPU ASR throughput.

Work top to bottom. Every step has a **verify** line — actually run it,
look at the output, only tick the box when the expected result appears.

## 0. Pre-flight (5 min)

- [ ] NAS has at least 30 GB free for model weights (faster-whisper-small
      alone is 488 MB; whisperx-tiny is 151 MB; larger models scale up).
- [ ] NAS has at least 4 GB RAM free. Faster-whisper-small on CPU uses
      ~1.5 GB; on GPU it can use 3-4 GB VRAM.
- [ ] Docker ≥ 24.0 and Compose v2 installed (`docker --version`,
      `docker compose version`).
- [ ] The repo is cloned on the NAS and the `dev` branch is checked out
      (or main, depending on which you want deployed).
- [ ] Media library is bind-mounted at `/data` (or whatever path you set
      in `docker-compose.yml`'s `volumes:` block). Verify:
      `ls /data | head` shows your movie/TV folders.

## 1. Build the full ML image (10-30 min)

```bash
# from repo root
docker compose build
```

**Verify**: build finishes without errors. Image size is ~6-8 GB (vs the
~1 GB smoke images).

**Why this matters on the NAS**: only the NAS has the network bandwidth /
mirror config to download whisperx / torch / qwen-asr reliably. Building
on the host will likely fail with mirror 403s.

## 2. Boot the stack

```bash
docker compose up -d
docker compose ps  # both api and worker should be Up
docker compose logs -f --tail=100 api worker scanner
```

**Verify**:
- `docker compose ps` shows `api`, `scanner`, `worker` all `Up` for at least
  `startsecs=5` (5 seconds)
- `curl http://localhost:8000/api/health` returns `{"status":"ok"}`
- The supervisord log shows: `success: worker entered RUNNING state`

## 3. Open the web UI and finish the wizard

Navigate to `http://<nas-ip>:8000/`.

**Verify**:
- The setup wizard appears
- You can pick an ASR model (e.g. `faster-whisper-small` for CPU, or
  `whisx-large-v3` if you have a GPU)
- The wizard completes; the dashboard shows up

## 4. Download a model (5-30 min depending on size)

In the web UI: **Settings → Models** → pick `faster-whisper-small` → Download.

**Verify**:
- Model status goes from `downloading` → `ready`
- `/models/faster-whisper-small/model.bin` exists on the NAS filesystem
  (check via the host path that your `models:` volume maps to)

## 5. Translation API connection (2 min)

In the web UI: **Settings → Translation** → fill in your OpenAI-compatible
API base URL + key.

**Verify**:
- "Test connection" button returns success
- The translated smoke test (covered in `scripts/docker-smoke-full.sh`
  section 4 with a local mock) will work end-to-end with the real key

## 6. Webhook target = your Emby/Jellyfin (5 min)

In the web UI: **Settings → Notification**:
- Set `webhook_enabled` to `true`
- Set `webhook_type` to `emby` (or `jellyfin` / `plex` / `generic`)
- Set `webhook_url` to your media server's base URL
- Set `webhook_token` to the API key

**Verify (manual)**: trigger a library scan from the Emby/Jellyfin admin
panel and watch the worker logs:
```bash
docker compose logs -f worker
```
You should see `[app.webhook] Webhook (emby) sent for task N` after the
scan completes.

**Verify (full end-to-end)**: run `RUN_REAL_VIDEO=1
./scripts/docker-smoke-full.sh` on the NAS. The webhook section will hit
your real Emby/Jellyfin URL and you can confirm via the server's API log
that the request arrived.

## 7. Drop a real video in /data (2 min)

```bash
cp /path/to/some-movie.mp4 /data/
```

**Verify** (wait up to `scan_interval_seconds`, default 5):
- `curl http://localhost:8000/api/tasks?page_size=10` shows a new task
  for the file you just dropped
- The task progresses through stages:
  `extract_audio → run_asr → align_segments → text_process →
  translate → subtitle_render → output_finalize` (and `mux` if enabled)
- A `.srt` file appears in `/data/<movie-stem>.forced.<lang>.srt` (or
  in `/output/` if `output_to_source_dir` is `false`)
- The dashboard's "Tasks" page shows the task as `done`

## 8. Confirm Emby/Jellyfin picks up the new subtitle (1 min)

In Emby/Jellyfin admin: trigger a library refresh for the movie's folder.

**Verify**:
- Emby/Jellyfin now lists the movie with the new subtitle track
- The subtitle displays correctly when you play the movie
- If you used the `output_to_source_dir=true` config, the .srt is in the
  same folder as the .mp4; Emby will pick it up automatically on refresh

## 9. Quality check (1 min, optional)

In the web UI: **Dashboard** → check the "可疑字幕" (suspect subtitles) card.

**Verify**:
- The card is empty (no suspect subtitles) OR
- Any suspect entries have a `score` ≥ `suspect_score_threshold` (default 70)
  and show a clear reason like "low ASR confidence" or "empty translation"

## 10. Watch directories (if you want multi-folder scanning)

By default only `/data` is scanned. To watch additional directories:

In the web UI: **Settings → File** → set `input_dirs` to a comma-separated
list of paths inside the container (e.g. `/data/movies,/data/tv`).

**Verify**:
- The watcher picks up files from each path within `scan_interval_seconds`
- The `/api/browse?path=<path>` endpoint can read each path
- The `browse_roots` env var (in `docker-compose.yml`) includes each path

## Common issues on first deploy

| Symptom | Likely cause | Fix |
|---|---|---|
| Worker keeps restarting | `restart_required: true` on a system config change (e.g. `whisper.model_name`); worker has stale in-process config cache | Restart the worker: `docker compose restart worker` |
| `whisperx not installed` error after picking whisperx | The smoke image doesn't have whisperx; you need the full ML build | `docker compose build` (Section 1) |
| Translation fails with "no text content" | API key is wrong, or the model doesn't exist on the server | Re-check the API key in Settings → Translation; "Test connection" should pass first |
| Webhook fires but Emby/Jellyfin shows no new scan | `webhook_token` is wrong, or the library IDs don't match | Test with the same token via curl directly against your media server |
| Scanner isn't picking up new files | The new file extension isn't in `allowed_extensions` (default `.mp4 .mkv .mov .avi`), or the file is in `exclude_dirs` | Add the extension in Settings → File |
| Subtitle renders but Emby/Jellyfin doesn't show it | The .srt is named differently than the .mp4, OR the media server hasn't been refreshed | Confirm `.srt` stem matches `.mp4` stem; trigger a manual library refresh |

## What's NOT covered by this checklist

- **GPU performance tuning**: the default config works on CPU but is
  ~5-10x slower than GPU. If you have an NVIDIA GPU, set
  `whisper.device: cuda` in Settings → Whisper, and the ASR stage
  should drop from ~30s/min of audio to ~3s/min.
- **Long-form reliability on 4+ hour videos**: the smoke tests use
  ≤15s clips. If you process full-length movies, monitor disk space
  in `/config/work` and `pre_asr_resource_check` (enabled by default
  to refuse runs that don't fit).
- **Multi-language detection**: the smoke uses `source_language: zh`
  and `source_language: en`. If you watch lots of Japanese / Korean
  content, set `source_language: auto` and watch a few runs to confirm
  the detector picks the right language.
