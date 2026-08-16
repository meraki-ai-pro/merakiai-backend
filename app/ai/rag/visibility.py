"""Which documents a student is allowed to retrieve from.

Step 6 of the permission stack (Student Permission Checks §3.4): a chunk must
belong to a published, non-deleted document that is tagged for the mode being
used. Enforced here — inside the retrieval path — rather than in the UI, so an
unpublished draft cannot reach an answer by any route.

Publish state lives in Postgres, not in Pinecone metadata, deliberately. Vector
metadata would mean re-upserting every chunk of a document to toggle one
boolean; a lecturer who unpublishes a file with a wrong formula in it expects it
gone from the next answer, not after a re-embed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

# Ten minutes, not fifteen seconds.
#
# The TTL is a backstop, not the correctness mechanism: a lecturer's publish or
# unpublish must take effect on the very next answer. The only thing the TTL
# covers is a change nothing told us about, and that does not warrant a 578ms
# Supabase round trip inside the latency budget of every fourth turn.
#
# Set RAG_VISIBILITY_TTL=0 to disable caching entirely.
_TTL_SECONDS = float(os.getenv("RAG_VISIBILITY_TTL", "600"))

# (course_id, mode) -> (cached_at, generation, ids)
_cache: dict[tuple[str, str], tuple[float, str | None, list[str] | None]] = {}

# Cross-process invalidation.
#
# `_cache` is per-process, and the process that CHANGES visibility is not the
# process that READS it: publishing is an API route, retrieval runs in the
# Celery text worker. An in-process invalidate() therefore clears the API's
# copy and leaves the worker serving a stale set for the rest of the TTL — a
# lecturer unpublishes a file with a wrong formula, their own test-query panel
# (served by the API) shows it gone, and students keep being taught from it for
# ten more minutes.
#
# So the cache is validated against a counter in Redis that every writer bumps.
# A GET is sub-millisecond and shared by every process, which keeps the point
# of the cache — no Supabase round trip per turn — while making invalidation
# immediate everywhere. If Redis is unreachable the generation reads as None
# and behaviour degrades to the plain TTL, which is what it was before.
_GENERATION_PREFIX = "rag:visibility:gen:"

_redis_pool = None


def _get_redis():
    global _redis_pool
    import redis as redis_sync

    if _redis_pool is None:
        _redis_pool = redis_sync.ConnectionPool.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            max_connections=10,
        )
    return redis_sync.Redis(connection_pool=_redis_pool)


def _current_generation(course_id: str) -> str | None:
    """The course's visibility generation, or None if Redis is unavailable."""
    try:
        return _get_redis().get(f"{_GENERATION_PREFIX}{course_id}") or "0"
    except Exception as exc:  # noqa: BLE001 — caching must never break retrieval
        logger.debug("Visibility generation unavailable for course=%s: %s", course_id, exc)
        return None


def _fetch(course_id: str, mode: str) -> list[str] | None:
    """Document ids visible for this course and mode.

    Returns None to mean "no filter needed" — either every document in the
    course is visible, or the schema predates this feature. None keeps the
    Pinecone query byte-identical to what it was before, which matters because
    that is the overwhelmingly common case.
    """
    sb = get_supabase()

    try:
        rows = (
            sb.table("documents")
            .select("id, is_published, target_modes, status, deleted_at")
            .eq("course_id", course_id)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 — columns may not exist yet
        logger.warning(
            "Visibility lookup failed for course=%s; retrieving unfiltered. "
            "Apply 006_add_mode_aware_publishable_documents.sql: %s",
            course_id, exc,
        )
        return None

    if not rows:
        return None

    visible: list[str] = []
    filtered = False

    for row in rows:
        if row.get("status") not in (None, "ready"):
            # Still processing or failed. Its vectors are absent or partial;
            # excluding it costs nothing and avoids half-ingested answers.
            filtered = True
            continue
        if row.get("deleted_at"):
            filtered = True
            continue
        if row.get("is_published") is False:
            filtered = True
            continue

        modes = row.get("target_modes")
        if modes and mode not in modes:
            filtered = True
            continue

        visible.append(row["id"])

    if not filtered:
        return None

    return visible


def _lookup(course_id: str, mode: str) -> list[str] | None:
    """Cache check, generation check and fetch — one thread hop, not three."""
    key = (course_id, mode)
    now = time.monotonic()
    generation = _current_generation(course_id)

    if _TTL_SECONDS > 0:
        hit = _cache.get(key)
        # A generation mismatch beats a live TTL: somebody published or
        # unpublished since this entry was stored, possibly in another process.
        if hit and now - hit[0] < _TTL_SECONDS and hit[1] == generation:
            return hit[2]

    ids = _fetch(course_id, mode)

    if _TTL_SECONDS > 0:
        _cache[key] = (now, generation, ids)

    return ids


async def visible_document_ids(course_id: str, mode: str) -> list[str] | None:
    """Cached, non-blocking wrapper around :func:`_fetch`."""
    return await asyncio.to_thread(_lookup, course_id, mode)


def invalidate(course_id: str | None = None) -> None:
    """Drop cached visibility so a publish toggle takes effect at once.

    Bumps the shared generation as well as clearing this process's copy —
    otherwise only the process that handled the publish would notice, and the
    worker that actually answers students would not.
    """
    if course_id is None:
        _cache.clear()
        return

    for key in [k for k in _cache if k[0] == course_id]:
        _cache.pop(key, None)

    try:
        _get_redis().incr(f"{_GENERATION_PREFIX}{course_id}")
    except Exception as exc:  # noqa: BLE001
        # The local clear already happened; other processes fall back to the
        # TTL. Worth a warning, because it means a publish is slow to land.
        logger.warning(
            "Could not bump visibility generation for course=%s — other "
            "processes will not see this change until the %.0fs TTL expires: %s",
            course_id, _TTL_SECONDS, exc,
        )
