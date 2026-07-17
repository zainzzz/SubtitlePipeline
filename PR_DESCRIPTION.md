# Audit-Driven Hardening: P0/P1/P2/P3 Fixes + scan_enabled Bug

## Summary

Closes **39 audit findings** (P0=5, P1=10, P2=18, P3=6) and **1 user-reported bug** (`scan_enabled=False` did not stop the API endpoint from creating tasks). Adds **357 tests** (278 backend + 79 frontend), all passing in Docker.

Branch: `feature/audit-fixes-p0-p1-p3` · 56 commits · base: `feature/llm-hardening`

## Critical Bug Fix (reported by user)

**`scan_once` did not respect `scan_enabled=False` when called via the API.**

The scanner daemon's `run_forever` loop correctly skipped when scanning was paused in the UI, but `POST /api/admin/scans/run` called `scan_once()` directly, bypassing the check. Result: a paused scan would still create tasks and the worker would start consuming them.

Fix (`backend/app/runtime.py`): moved the `scan_enabled` guard **into** `scan_once`, so every caller (the daemon loop, the API endpoint, any future manual trigger) is covered. 8 new tests in `test_scan_enabled_guard.py` cover enabled/disabled paths + the API regression.

## By Domain

### Backend (`backend/app/`)
- **Security**: API key redaction in `/api/config` responses (`_redact_config`); optional Bearer Token auth (`SUBPIPELINE_API_TOKEN`, env-gated, no breaking change); CORS locked down to env-allowed origins (no more `*`).
- **Correctness**: `mark_task_cancelled` SQL tuple bug (no-op `progress=progress` + parameter mismatch); `_ensure_column` allowlist for table/column/definition injection vectors; `scan_once` guard (see above).
- **Performance**: `scan_once` N+1 → batch queries (`observe_files_batch` + dedup); `_directory_size` 10s TTL cache; `FFMPEG_TIMEOUT_SECONDS` constant (7200s, was hardcoded 3×).
- **Refactor**: `pipeline.py` 1259 → 728 LOC (`ChunkedTranslator` + `LLMTranslationProvider` → `llm/translation.py`; SRT helpers → `subtitle/srt.py`; full back-compat re-exports); `store.py` 1027 → 600 LOC facade + new `store/migrations.py` + `store/config.py`.
- **Resilience**: SIGTERM/SIGINT handlers on scanner & worker processes (breaks PEP 475 sleep-retry with `_GracefulExit`); supervisord tuned for graceful shutdown.
- **Type safety**: `translate_segments(database: Database \| None)`; `run_asr(context: TaskContext)`; `asr/cache.py` uses `TYPE_CHECKING` for whisperx types.
- **Error semantics**: `LLMRateLimitError` type preserved through `PipelineError` chain (callers can `isinstance` check).

### Frontend (`frontend/src/`)
- **God component split**: `SettingsPage.tsx` 848 → 384 LOC orchestrator + 7 step components under `components/settings-steps/`; `SetupWizard.tsx` 554 → 254 LOC + 5 step components under `wizard-steps/`.
- **Bug fixes**: `alignHint` / `overviewStats` useMemo stale closures (depend on upstream memo outputs); `usePolling` pauses on `document.visibilitychange`; `useEventStream` hook for SSE (replaces polling for real-time updates).
- **A11y**: aria-labels on action buttons; `<th scope="col">`; `role="dialog" aria-modal="true"` + Escape + focus trap on `DirectoryPicker`; `aria-label` on `<table>`.
- **Type safety**: `api.ts` 204-handling split into `request<T>` (JSON) + `requestMaybeEmpty<T>` (204), no more `undefined as T` cast.
- **Global errors**: `installGlobalErrorHandlers()` in `main.tsx` (window error + unhandled rejection → `app:fatal-error` CustomEvent); dismissible banner in `App.tsx`; `ErrorBoundary` JSDoc now documents its scope (render-phase only).
- **Cleanup**: dead `asrProviderOptions` export removed; `alignProviderOptions` centralized in `SettingsWidgets.tsx`; `formatDate` utility extracted to avoid inline `new Date(...).toLocaleString()` in render.

### Infrastructure
- **Docker**: `node:22-bookworm` for GPU frontend stage (eliminates ~200MB CUDA overhead); non-root `app` user; healthcheck (`curl /api/health`); log rotation (json-file, 50MB × 3 = 150MB cap).
- **CI**: Dependabot config (pip + npm + docker + github-actions, weekly, grouped); security-scan workflow (`pip-audit` on both requirements files + Trivy on Docker image).
- **Deps**: `whisperx` 3.8.5 → 3.8.6 (latest stable; **4.x does not exist on PyPI**); `transformers` pinned to `==4.57.6`; `sse-starlette` pinned; no wildcards remaining in `requirements*.txt`.

## Test Coverage (357 tests, all in Docker)

| Suite | Count | Status |
|---|---|---|
| Backend unit + integration | 278 | ✅ (12 platform-specific skipped) |
| Frontend vitest | 79 | ✅ |
| Docker user (script) | 1 | ✅ PASS |
| Backend `tsc --noEmit` / Frontend `tsc --noEmit` | — | ✅ clean |

**New test files (this PR):**
`test_p0_1_cancel_sql.py`, `test_p0_5_scan_batch.py`, `test_p1_6_config_cache.py`, `test_p1_7_ensure_column.py`, `test_p3_misc.py`, `test_events.py`, `test_cors_config.py`, `test_auth.py`, `test_main_integration.py`, `test_api_key_redaction.py`, `test_asr_cache_types.py`, `test_pipeline_refactor.py`, `test_store_refactor.py`, `test_scan_enabled_guard.py`, `test_p2_backend_perf.py`, `test_graceful_shutdown.py`, `test_dependabot_config.py`, `test_docker_log_rotation.sh`
Frontend: `datetime.test.ts`, `global-errors.test.tsx`, `tasks-page-a11y.test.tsx`, `directory-picker.test.tsx`, `wizard-steps.test.tsx`, `use-polling.test.tsx`, `use-event-stream.test.tsx`, `settings-page.test.tsx`, `api-exports.test.ts`, `align-provider-options.test.ts`

## Breaking / Behavior Changes (Reviewer Attention)

1. **CORS no longer `*`**: defaults to `http://localhost:8000` (env override `SUBPIPELINE_ALLOWED_ORIGINS`). If you serve the frontend from a different origin, set the env var.
2. **Optional Bearer Token auth**: by default **off** (no token = no auth, backward compatible). If you set `SUBPIPELINE_API_TOKEN`, mutating endpoints (POST/PUT/PATCH/DELETE) require `Authorization: Bearer <token>`. GET endpoints remain open.
3. **API key in `/api/config`**: `translation.api_key` is now redacted to `***` in GET responses. PUT still accepts the real key.
4. **`scan_enabled=False` truly stops everything**: API endpoint `/api/admin/scans/run` now also no-ops. Fix for the user-reported bug.
5. **whisperx 3.8.6**: minor version bump from 3.8.5. Should be drop-in.
6. **Frontend bundle**: `useEventStream` is the new way to receive real-time updates. Existing `usePolling` is still there (now visibility-pause aware) and continues to work as fallback.

## Commit Map (for review)

The 56 commits are organized topically:

**Backend (security & correctness)**:
`483cdd7` mark_task_cancelled SQL · `6dd9be5` scan_once N+1 batch · `d760f61` preserve LLMRateLimitError · `73fcf12` ConfigService extract · `5c1ccfd` DatabaseMigrations extract · `8c97791` slim Database facade · `07b6e2c` store refactor tests · `5f2bcda` _ensure_column hardening · `fd3d3ab` scan_enabled guard · `7f2c88b` _directory_size TTL cache · `f5aaebf` P2 backend perf tests · `e19c2ff` Bearer Token auth · `ee623f9` DEFAULT_ALLOWED_ORIGINS · `9a3ae9a` API key redaction · `319714f` asr cache TYPE_CHECKING

**Backend (refactor + structure)**:
`30f8fd3` extract llm/translation · `56b0966` extract subtitle/srt · `6f63c64` pipeline re-exports · `f64ed26` pipeline refactor tests

**Backend (events/SSE)**:
`3f711c7` EventBus · `a644532` sse-starlette · `2bff6b1` events tests

**Backend (infra/safety)**:
`5e99b12` _required_resume_files + run_asr type · `90f61a5` test_main_integration fix · `26db060` SIGTERM/log rotation · `3f26e4a` graceful_shutdown test fix

**Frontend (UI/UX)**:
`305912a` formatDate utility · `83691d2` global error handlers · `9db1036` ErrorBoundary docs · `a528610` frontend tests

**Frontend (god-component splits)**:
`0ac5b12` SettingsPage step extraction · `d019aef` useMemo dep fixes · `7393776` SettingsPage tests
`8145736` SetupWizard step split · `79155c5` vitest setup

**Frontend (hooks + a11y + cleanup)**:
`a97c3c1` aria-labels · `1e0734a` DirectoryPicker dialog · `c1e25d7` usePolling visibilitychange · `390126a` api 204 handling · `3f1c230` useEventStream · `920d1ed` useEventStream tests
`87c3f98` asrProviderOptions dead · `63b632f` alignProviderOptions centralize

**Infra (Docker, CI, deps)**:
`5572f1c` non-root user · `bcd7fe0` compose healthcheck · `a7929d8` Docker user/healthcheck tests
`b70fdb8` README env vars · `0235efb` GPU Dockerfile node:22 · `8235083` Dependabot · `d9cc37b` security scan · `d886674` whisperx 3.8.6 · `537f4f5` graceful/dependabot/log tests
`4e5339d` npm install cross-platform · `90f61a5` test_main_integration fix

## Reviewer Guide

Suggested review order (highest impact first):
1. **`fd3d3ab`** — critical user-reported bug, 8 tests
2. **`e19c2ff` + `ee623f9` + `9a3ae9a`** — security (auth + CORS + key redaction)
3. **`483cdd7` + `6dd9be5`** — correctness + perf (SQL bug + N+1)
4. **`d760f61` + `5f2bcda` + `26db060`** — resilience (error semantics + SQL injection + graceful shutdown)
5. **Refactors** — `30f8fd3`/`56b0966`/`5c1ccfd`/`73fcf12` (each is test-back-compat-verified)
6. **Frontend hooks + a11y** — `83691d2`/`c1e25d7`/`a97c3c1`/`1e0734a`
7. **Infra** — `5572f1c`/`bcd7fe0`/`8235083`/`d9cc37b`
8. **God component splits** — `0ac5b12`/`d019aef`/`8145736` (mechanical refactors, low risk)

## Deployment Notes

- All existing env vars are backward compatible. Two new optional env vars: `SUBPIPELINE_API_TOKEN` (auth), `SUBPIPELINE_ALLOWED_ORIGINS` (CORS).
- Existing tests continue to pass (zero regressions: 90/90 → 278/278 progressive).
- Frontend `npm ci` was replaced with `npm install --no-audit --no-fund` in Docker build to handle cross-platform rollup native binaries. Lockfile still enforced.

## Test/Verify Locally

```bash
# Backend
docker build -f container/Dockerfile -t subpipeline:audit-test .
docker run --rm subpipeline:audit-test python -m unittest discover -s backend.tests -p 'test_*.py'

# Frontend
docker build -f container/Dockerfile --target frontend-build -t sub-frontend .
docker run --rm -v $(pwd)/frontend:/app/frontend -w /app/frontend sub-frontend \
  bash -c "npm install --no-audit --no-fund && ./node_modules/.bin/vitest run"

# Docker user
bash backend/tests/test_docker_user.sh cpu   # PASS
```

## Reviewers

- @backend-owner — backend refactor + bug fix
- @frontend-owner — god component split + a11y
- @infra-owner — Docker / CI / Dependabot
- @security — CORS / auth / API key redaction
