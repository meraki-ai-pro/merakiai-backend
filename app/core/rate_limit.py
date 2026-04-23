"""
Simple Redis-based rate limiter.

Usage as a FastAPI dependency:

    from app.core.rate_limit import rate_limit

    @router.post("/turn")
    async def turn(
        payload: ...,
        user=Depends(auth_guard),
        _=Depends(rate_limit(max_calls=30, window_seconds=60)),
    ):
        ...
"""
from __future__ import annotations

import os
import logging

import redis as redis_sync
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

_security = HTTPBearer(auto_error=False)


def rate_limit(max_calls: int, window_seconds: int):
    """
    Dependency factory.

    Identifies callers by their Supabase user-id (from the JWT) when present,
    falling back to the client IP. Raises HTTP 429 once the caller exceeds
    max_calls within the rolling window_seconds window.
    """
    def _dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    ) -> None:
        # Build a stable identifier (user-id beats raw IP)
        identifier: str
        if credentials and credentials.credentials:
            try:
                from app.core.auth import _validate_token
                user = _validate_token(credentials)
                identifier = str(user.id)
            except Exception:
                identifier = request.client.host if request.client else "unknown"
        else:
            identifier = request.client.host if request.client else "unknown"

        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        key = f"rl:{request.url.path}:{identifier}"

        try:
            r = redis_sync.from_url(redis_url, decode_responses=True)
            count = r.incr(key)
            if count == 1:
                r.expire(key, window_seconds)
            r.close()
        except Exception as exc:
            # Redis unavailable — fail open (don't block the request)
            logger.warning("Rate limit Redis error  key=%s  error=%s", key, exc)
            return

        if count > max_calls:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded. Max {max_calls} requests per {window_seconds}s.",
            )

    return _dependency
