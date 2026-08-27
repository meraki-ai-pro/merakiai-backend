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

# (course_id, mode) -> (cached_at, generation, rows)
#
# Rows, not ids. The per-student preference (question format, difficulty) is
# applied on top of this and varies turn by turn, so caching a pre-filtered id
# list would either miss constantly or serve one student's preference to
# another. The tags travel with the rows and the narrowing is done in memory.
_cache: dict[tuple[str, str], tuple[float, str | None, list[dict] | None]] = {}

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


_SELECT = "id, is_published, target_modes, status, deleted_at, difficulty, question_formats"

# The columns as they were before sql/013. Selecting a column Postgres does not
# have is a 400 for the whole query, so an un-migrated database must not lose
# publish filtering as collateral damage.
_SELECT_LEGACY = "id, is_published, target_modes, status, deleted_at"


def _fetch(course_id: str, mode: str) -> list[dict] | None:
    """Visible document rows for this course and mode, or None for "all".

    Returns None to mean "no filter needed" — either every document in the
    course is visible, or the schema predates this feature. None keeps the
    Pinecone query byte-identical to what it was before, which matters because
    that is the overwhelmingly common case.
    """
    sb = get_supabase()

    def _query(columns: str):
        return (
            sb.table("documents").select(columns)
            .eq("course_id", course_id).execute().data or []
        )

    try:
        rows = _query(_SELECT)
    except Exception:  # noqa: BLE001 — sql/013 may not be applied
        try:
            rows = _query(_SELECT_LEGACY)
        except Exception as exc:  # noqa: BLE001 — columns may not exist at all
            logger.warning(
                "Visibility lookup failed for course=%s; retrieving unfiltered. "
                "Apply 006_add_mode_aware_publishable_documents.sql: %s",
                course_id, exc,
            )
            return None

    if not rows:
        return None

    visible: list[dict] = []
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

        visible.append(row)

    if not filtered:
        return None

    return visible


def _lookup(course_id: str, mode: str) -> list[dict] | None:
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

    rows = _fetch(course_id, mode)

    if _TTL_SECONDS > 0:
        _cache[key] = (now, generation, rows)

    return rows


def prefer(
    rows: list[dict] | None,
    *,
    question_format: str | None = None,
    difficulty: str | None = None,
) -> list[dict] | None:
    """Narrow to the documents the lecturer tagged for this exact ask.

    A **preference**, not a filter, and the distinction is the whole design.
    The tags are optional and most files will not carry them, so treating a
    non-match as "not visible" would empty a course's Review material the first
    time one lecturer ticked "Multiple Choice" on one file. Instead: if
    anything matches, use only those; if nothing does, fall back to everything
    visible for the mode, which is exactly the behaviour before tagging existed.

    Untagged documents count as matching. A file with no formats listed is not
    "unsuitable for MCQ", it is "unspecified", and the lecturer who uploaded it
    before the field existed did not opt out of anything.
    """
    if not rows:
        return rows

    wanted_format = (question_format or "").strip().lower() or None
    wanted_difficulty = (difficulty or "").strip().lower() or None
    if not wanted_format and not wanted_difficulty:
        return rows

    def matches(row: dict) -> bool:
        if wanted_format:
            formats = row.get("question_formats")
            if formats and wanted_format not in formats:
                return False
        if wanted_difficulty:
            level = (row.get("difficulty") or "").strip().lower()
            if level and level != wanted_difficulty:
                return False
        return True

    preferred = [r for r in rows if matches(r)]
    if not preferred:
        logger.info(
            "No documents tagged format=%s difficulty=%s — using all %d visible",
            wanted_format, wanted_difficulty, len(rows),
        )
        return rows
    return preferred


async def visible_document_ids(
    course_id: str,
    mode: str,
    *,
    question_format: str | None = None,
    difficulty: str | None = None,
) -> list[str] | None:
    """Cached, non-blocking wrapper around :func:`_fetch`.

    ``question_format`` and ``difficulty`` come from what the student chose in
    the mode picker and are matched against the lecturer's upload tags. Both
    are preferences — see :func:`prefer`.
    """
    rows = await asyncio.to_thread(_lookup, course_id, mode)
    if rows is None:
        return None
    return [
        r["id"]
        for r in prefer(rows, question_format=question_format, difficulty=difficulty)
    ]


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
