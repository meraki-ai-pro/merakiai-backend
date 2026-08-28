"""Query-embedding cache.

Measured on this deployment, warm:

    Supabase cache lookup      687 ms
    OpenAI embeddings.create   ~500 ms
    Supabase cache write       891 ms   (previously awaited, blocking the turn)

The lookup costs more than the call it avoids, so the old Supabase-backed cache
was a pessimisation on every hit and a 1.6s tax on every miss. Nothing about
that is obvious from the code — it reads like an optimisation — which is why
the numbers are recorded here.

The replacement is two tiers in front of OpenAI:

    1. in-process LRU   ~0 ms    same question, same worker
    2. Redis            ~1-2 ms  same question, any worker
    3. OpenAI           ~500 ms  genuinely new question

Redis is already in the stack for the Celery broker and the WebSocket relay, so
this adds no new dependency. Writes are fire-and-forget: the embedding is
already in hand by then, and making a student wait on a cache write is exactly
the mistake being corrected.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections import OrderedDict

from app.ai.embedding_config import cache_namespace

logger = logging.getLogger(__name__)

# Bounded so a long-lived worker cannot grow without limit. 512 x 3072 floats
# is roughly 12 MB, which is affordable and covers a lecture's worth of
# repeated questions.
_LRU_MAX = int(os.getenv("EMBED_LRU_SIZE", "512"))
_lru: OrderedDict[str, list[float]] = OrderedDict()

# A week. Query embeddings never change for the same text — the only reason to
# expire at all is to stop Redis growing forever.
_REDIS_TTL = int(os.getenv("EMBED_CACHE_TTL", str(7 * 24 * 3600)))

_KEY_PREFIX = "emb:v2:"

_redis = None
_redis_unavailable = False


def _key(text: str) -> str:
    payload = f"{cache_namespace()}\n{text}"
    return _KEY_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_redis():
    """Lazy async Redis client. Returns None once we know it is unavailable.

    The sticky flag matters: without it every single turn pays a connection
    timeout when Redis is down, which is worse than not caching at all.
    """
    global _redis, _redis_unavailable
    if _redis_unavailable:
        return None
    if _redis is None:
        try:
            import redis.asyncio as aioredis

            _redis = aioredis.from_url(
                os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding cache: Redis unavailable (%s); using OpenAI directly", exc)
            _redis_unavailable = True
            return None
    return _redis


def _lru_get(key: str) -> list[float] | None:
    value = _lru.get(key)
    if value is not None:
        _lru.move_to_end(key)
    return value


def _lru_put(key: str, embedding: list[float]) -> None:
    _lru[key] = embedding
    _lru.move_to_end(key)
    while len(_lru) > _LRU_MAX:
        _lru.popitem(last=False)


async def get(text: str) -> list[float] | None:
    """Look up an embedding. Never raises; a cache failure is a miss."""
    key = _key(text)

    hit = _lru_get(key)
    if hit is not None:
        return hit

    client = _get_redis()
    if client is None:
        return None

    try:
        raw = await client.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Embedding cache read failed: %s", exc)
        return None

    if not raw:
        return None

    try:
        embedding = json.loads(raw)
    except (TypeError, ValueError):
        return None

    _lru_put(key, embedding)
    return embedding


async def put(text: str, embedding: list[float]) -> None:
    """Store an embedding. Fire-and-forget — never awaited on the hot path."""
    key = _key(text)
    _lru_put(key, embedding)

    client = _get_redis()
    if client is None:
        return
    try:
        await client.set(key, json.dumps(embedding), ex=_REDIS_TTL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Embedding cache write failed: %s", exc)


def put_background(text: str, embedding: list[float]) -> None:
    """Schedule the write without blocking the caller.

    The LRU is populated synchronously so an immediate repeat still hits, and
    only the Redis round trip is deferred.
    """
    _lru_put(_key(text), embedding)
    try:
        asyncio.get_running_loop().create_task(put(text, embedding))
    except RuntimeError:
        # No running loop (sync context) — the LRU write above still stands.
        pass


def stats() -> dict:
    return {"lru_entries": len(_lru), "lru_max": _LRU_MAX,
            "redis": "unavailable" if _redis_unavailable else "enabled"}
