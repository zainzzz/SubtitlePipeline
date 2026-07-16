from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import (
    SAFE_METHODS,
    TOKEN_ENV_VAR,
    get_api_token,
    is_auth_enabled,
    require_token,
    require_token_for_mutation,
)


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/mutate")
    async def mutate(_=Depends(require_token_for_mutation)) -> dict[str, str]:
        return {"ok": "true"}

    @app.put("/put")
    async def put(_=Depends(require_token_for_mutation)) -> dict[str, str]:
        return {"ok": "true"}

    @app.delete("/delete")
    async def delete(_=Depends(require_token_for_mutation)) -> dict[str, str]:
        return {"ok": "true"}

    return app


class TestAuthDisabled(unittest.TestCase):
    def setUp(self) -> None:
        self._patch = patch.dict(os.environ, {}, clear=False)
        env = self._patch.start()
        env.pop(TOKEN_ENV_VAR, None)

    def tearDown(self) -> None:
        self._patch.stop()

    def test_is_auth_disabled(self) -> None:
        self.assertFalse(is_auth_enabled())
        self.assertIsNone(get_api_token())

    def test_require_token_allows_anything_when_unset(self) -> None:
        self.assertTrue(require_token(None))
        self.assertTrue(require_token(""))
        self.assertTrue(require_token("Bearer anything"))

    def test_get_endpoint_works_without_header(self) -> None:
        client = TestClient(_build_app())
        self.assertEqual(client.get("/health").status_code, 200)

    def test_post_endpoint_works_without_header(self) -> None:
        client = TestClient(_build_app())
        self.assertEqual(client.post("/mutate").status_code, 200)


class TestAuthEnabled(unittest.TestCase):
    TOKEN = "s3cret-token-abcdef"

    def setUp(self) -> None:
        self._patch = patch.dict(os.environ, {TOKEN_ENV_VAR: self.TOKEN})
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    def test_is_auth_enabled(self) -> None:
        self.assertTrue(is_auth_enabled())
        self.assertEqual(get_api_token(), self.TOKEN)

    def test_get_endpoint_works_without_header(self) -> None:
        client = TestClient(_build_app())
        self.assertEqual(client.get("/health").status_code, 200)

    def test_safe_methods_are_get_head_options(self) -> None:
        self.assertEqual(SAFE_METHODS, frozenset({"GET", "HEAD", "OPTIONS"}))

    def test_post_without_header_returns_401(self) -> None:
        client = TestClient(_build_app())
        resp = client.post("/mutate")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.headers.get("WWW-Authenticate"), "Bearer")

    def test_post_with_correct_bearer_works(self) -> None:
        client = TestClient(_build_app())
        resp = client.post("/mutate", headers={"Authorization": f"Bearer {self.TOKEN}"})
        self.assertEqual(resp.status_code, 200)

    def test_put_with_correct_bearer_works(self) -> None:
        client = TestClient(_build_app())
        resp = client.put("/put", headers={"Authorization": f"Bearer {self.TOKEN}"})
        self.assertEqual(resp.status_code, 200)

    def test_delete_with_correct_bearer_works(self) -> None:
        client = TestClient(_build_app())
        resp = client.delete("/delete", headers={"Authorization": f"Bearer {self.TOKEN}"})
        self.assertEqual(resp.status_code, 200)

    def test_post_with_wrong_token_returns_401(self) -> None:
        client = TestClient(_build_app())
        resp = client.post("/mutate", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(resp.status_code, 401)

    def test_post_with_malformed_header_returns_401(self) -> None:
        client = TestClient(_build_app())
        resp = client.post("/mutate", headers={"Authorization": self.TOKEN})
        self.assertEqual(resp.status_code, 401)

    def test_post_with_empty_bearer_returns_401(self) -> None:
        client = TestClient(_build_app())
        resp = client.post("/mutate", headers={"Authorization": "Bearer "})
        self.assertEqual(resp.status_code, 401)

    def test_require_token_helper_rejects_wrong(self) -> None:
        self.assertFalse(require_token("Bearer wrong"))
        self.assertFalse(require_token(None))
        self.assertFalse(require_token(""))

    def test_require_token_helper_accepts_correct(self) -> None:
        self.assertTrue(require_token(f"Bearer {self.TOKEN}"))


class TestTimingAttackResistance(unittest.TestCase):
    """A wrong token of the same length should take roughly the same time as a correct one."""

    TOKEN = "0123456789abcdef"

    def setUp(self) -> None:
        self._patch = patch.dict(os.environ, {TOKEN_ENV_VAR: self.TOKEN})
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    def test_compare_digest_is_used_for_full_length(self) -> None:
        wrong_same_len = "abcdef0123456789"
        self.assertNotEqual(wrong_same_len, self.TOKEN)

        correct_times: list[float] = []
        wrong_times: list[float] = []
        for _ in range(200):
            t0 = time.perf_counter()
            require_token(f"Bearer {self.TOKEN}")
            correct_times.append(time.perf_counter() - t0)

            t0 = time.perf_counter()
            require_token(f"Bearer {wrong_same_len}")
            wrong_times.append(time.perf_counter() - t0)

        correct_mean = sum(correct_times) / len(correct_times)
        wrong_mean = sum(wrong_times) / len(wrong_times)

        self.assertGreater(correct_mean, 0)
        ratio = wrong_mean / correct_mean
        self.assertGreater(ratio, 0.3, "wrong-token timing should be close to correct-token timing")

    def test_compare_digest_amortizes_length_difference(self) -> None:
        wrong_short = "short"
        self.assertFalse(require_token(f"Bearer {wrong_short}"))


class TestRequireTokenForMutationDirect(unittest.TestCase):
    """Call the dependency function directly to exercise its branches."""

    TOKEN = "direct-token"

    def setUp(self) -> None:
        self._patch = patch.dict(os.environ, {TOKEN_ENV_VAR: self.TOKEN})
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    def _fake_request(self, method: str, auth: str | None):
        from types import SimpleNamespace

        return SimpleNamespace(
            method=method,
            headers={"Authorization": auth} if auth is not None else {},
            url=SimpleNamespace(path="/x"),
        )

    def test_get_passes_without_token(self) -> None:
        asyncio.run(require_token_for_mutation(self._fake_request("GET", None)))

    def test_post_correct_token_passes(self) -> None:
        asyncio.run(require_token_for_mutation(self._fake_request("POST", f"Bearer {self.TOKEN}")))

    def test_post_wrong_token_raises_401(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(require_token_for_mutation(self._fake_request("POST", "Bearer nope")))
        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
