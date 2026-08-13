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
# The TTL is a backstop, not the correctness mechanism: every route that
# changes visibility (publish, unpublish, delete) calls invalidate() directly,
# so a lecturer's change still takes effect on the very next answer. The only
# thing the TTL covers is a change made outside the API — a direct SQL edit —
# and that does not warrant a 578ms Supabase round trip inside the latency
# budget of every fourth turn.
#
# Set RAG_VISIBILITY_TTL=0 to disable caching entirely.
_TTL_SECONDS = float(os.getenv("RAG_VISIBILITY_TTL", "600"))

_cache: dict[tuple[str, str], tuple[float, list[str] | None]] = {}


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


async def visible_document_ids(course_id: str, mode: str) -> list[str] | None:
    """Cached, non-blocking wrapper around :func:`_fetch`."""
    key = (course_id, mode)
    now = time.monotonic()

    if _TTL_SECONDS > 0:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL_SECONDS:
            return hit[1]

    ids = await asyncio.to_thread(_fetch, course_id, mode)

    if _TTL_SECONDS > 0:
        _cache[key] = (now, ids)

    return ids


def invalidate(course_id: str | None = None) -> None:
    """Drop cached visibility so a publish toggle takes effect at once."""
    if course_id is None:
        _cache.clear()
        return
    for key in [k for k in _cache if k[0] == course_id]:
        _cache.pop(key, None)
