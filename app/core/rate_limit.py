"""
Redis-backed rate limiter with a shared connection pool (M-11).

The previous implementation opened a fresh TCP connection on every request.
A module-level ConnectionPool is now created once and reused, eliminating the
per-request handshake overhead.

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

import logging
import os

import redis as redis_sync
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

_security = HTTPBearer(auto_error=False)

# Module-level connection pool — created once, reused for every request (M-11).
_redis_pool: redis_sync.ConnectionPool | None = None


def _get_redis() -> redis_sync.Redis:
    """Return a Redis client backed by a shared connection pool."""
    global _redis_pool
    if _redis_pool is None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        _redis_pool = redis_sync.ConnectionPool.from_url(
            redis_url, decode_responses=True, max_connections=20
        )
    return redis_sync.Redis(connection_pool=_redis_pool)


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

        key = f"rl:{request.url.path}:{identifier}"

        try:
            r = _get_redis()
            count = r.incr(key)
            if count == 1:
                r.expire(key, window_seconds)
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
