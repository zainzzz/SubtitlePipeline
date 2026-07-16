# Backend Tests

## Python unit tests

Run from the repo root or inside the container:

```bash
# Host (requires backend deps installed in the active venv)
cd backend && python3 -m unittest discover -s tests -v

# Inside the running container
docker compose exec subpipeline python3 -m unittest backend.tests.test_auth
docker compose exec subpipeline python3 -m unittest backend.tests.test_cors_config
```

### Test modules

| Module | Verifies |
|--------|----------|
| `test_auth.py` | `SUBPIPELINE_API_TOKEN` Bearer middleware — disabled mode, enabled mode (401 on missing/wrong token, 200 on correct), timing-safe comparison, method gating |
| `test_cors_config.py` | `DEFAULT_ALLOWED_ORIGINS` constant and `SUBPIPELINE_ALLOWED_ORIGINS` env parsing in `app/defaults.py` |
| `test_mvp.py` | Core pipeline end-to-end (existing) |
| `test_pipe_cleanup.py` | Intermediate-file cleanup (existing) |
| `test_scan_enhancements.py` | Scanner behaviour (existing) |
| `test_segment_cleaner.py` | Segment post-processing (existing) |
| `test_quota_saving.py` | Translation quota saving (existing) |

## Docker integration tests

Shell scripts that build the image and assert runtime invariants. Requires the
Docker daemon running.

```bash
# Verify the container runs as the non-root `app` user and data dirs are
# owned by app:appgroup. Accepts `cpu` (default) or `gpu`.
bash backend/tests/test_docker_user.sh cpu
bash backend/tests/test_docker_user.sh gpu

# Verify the docker-compose healthcheck transitions to "healthy" within 120s.
bash backend/tests/test_docker_healthcheck.sh cpu
bash backend/tests/test_docker_healthcheck.sh gpu
```

### What the Docker tests check

| Script | Assertions |
|--------|------------|
| `test_docker_user.sh` | `id -u` returns non-zero; `/app/backend`, `/data`, `/output`, `/models`, `/config` owned by `app` |
| `test_docker_healthcheck.sh` | `docker inspect .State.Health.Status` returns `healthy` within 120s of `docker compose up -d` |
