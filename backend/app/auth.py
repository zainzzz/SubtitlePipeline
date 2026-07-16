from __future__ import annotations

import logging
import os
import secrets
from typing import Final, Optional

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "SUBPIPELINE_API_TOKEN"
_BEARER_PREFIX: Final[str] = "Bearer "
SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})


def get_api_token() -> Optional[str]:
    """Return the configured API token, or ``None`` when auth is disabled."""
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    return token or None


def is_auth_enabled() -> bool:
    return get_api_token() is not None


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.startswith(_BEARER_PREFIX):
        return None
    candidate = authorization[len(_BEARER_PREFIX):].strip()
    return candidate or None


def require_token(authorization: Optional[str]) -> bool:
    """Pure check: True when no token is configured OR the bearer matches.

    Constant-time comparison via :func:`secrets.compare_digest` so successful
    and failed comparisons take the same wall-clock path.
    """
    expected = get_api_token()
    if expected is None:
        return True
    provided = _extract_bearer(authorization)
    if provided is None:
        return False
    return secrets.compare_digest(provided, expected)


async def require_token_for_mutation(request: Request) -> None:
    """FastAPI dependency gating only mutating methods.

    Apply via ``Depends(require_token_for_mutation)`` on routers that mix
    read and write endpoints. Safe methods (GET/HEAD/OPTIONS) always pass.
    """
    if request.method in SAFE_METHODS:
        return
    if require_token(request.headers.get("Authorization")):
        return
    logger.warning(
        "rejected %s %s: %s",
        request.method,
        request.url.path,
        "missing or invalid bearer token",
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


__all__ = [
    "SAFE_METHODS",
    "TOKEN_ENV_VAR",
    "get_api_token",
    "is_auth_enabled",
    "require_token",
    "require_token_for_mutation",
]

