"""Render job lifecycle: request, cache, execute, publish status.

Renders live outside the student turn. A Manim render takes minutes; putting it
where D-ID sits would rebuild the exact latency problem the Lesson Board exists
to avoid. The flow is instead:

    lecturer requests a concept video
        -> cache lookup  (an unchanged script is already rendered)
        -> queued on render_tasks
        -> rendered in an isolated worker
        -> lecturer reviews and approves
        -> replayed instantly by every student, forever

Students never wait on any of this. When a concept has no approved video they
get the Lesson Board, which is instant, and lose nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

import redis as redis_sync

from app.db.supabase import get_supabase
from app.media.render.registry import RenderRequest, resolve
from app.media.render.routing import route
from app.media.storage_service import signed_url

logger = logging.getLogger(__name__)

RENDERED_MEDIA_BUCKET = "rendered-media"

# Longer than the 15 minutes used for documents: a student may leave a lesson
# open and come back to the video, and a mid-playback expiry is a bad surprise.
_PLAYBACK_URL_TTL = 60 * 60 * 4


def content_hash(source_script: str) -> str:
    """Cache key over the script the artefact is generated from."""
    return hashlib.sha256((source_script or "").strip().encode("utf-8")).hexdigest()


def _publish(course_id: str, payload: dict) -> None:
    """Push render status over the same Redis -> WebSocket path as D-ID.

    Best-effort: a status push that fails must not fail the render itself.
    """
    try:
        client = redis_sync.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        client.publish(f"render:{course_id}", json.dumps(payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not publish render status for %s: %s", course_id, exc)


def find_cached(course_id: str, renderer: str, concept_key: str, digest: str) -> dict | None:
    """An existing asset for exactly this script, if there is one."""
    rows = (
        get_supabase()
        .table("media_assets")
        .select("*")
        .eq("course_id", course_id)
        .eq("renderer", renderer)
        .eq("concept_key", concept_key)
        .eq("content_hash", digest)
        .execute()
        .data
    )
    return rows[0] if rows else None


def request_render(
    *,
    course_id: str,
    concept_key: str,
    source_script: str,
    user_id: str,
    archetype: str | None = None,
    subject: str | None = None,
    topic: str | None = None,
) -> dict:
    """Create or reuse a render job. Returns the media_assets row.

    Re-requesting an unchanged concept is a cache hit, not a second job — a
    multi-minute render must not be repeated because someone clicked twice.
    """
    renderer = route(archetype, subject)
    digest = content_hash(source_script)

    existing = find_cached(course_id, renderer, concept_key, digest)
    if existing:
        # A previous failure is worth retrying; a queued, rendering or ready
        # asset is not.
        if existing["status"] != "failed":
            logger.info(
                "Render cache hit  course=%s concept=%s status=%s",
                course_id, concept_key, existing["status"],
            )
            return existing

        get_supabase().table("media_assets").update(
            {"status": "queued", "error": None}
        ).eq("id", existing["id"]).execute()
        return {**existing, "status": "queued", "error": None}

    row = (
        get_supabase()
        .table("media_assets")
        .insert({
            "course_id": course_id,
            "concept_key": concept_key,
            "topic": topic,
            "type": "video",
            "renderer": renderer,
            "archetype": archetype,
            "source_script": source_script,
            "content_hash": digest,
            "status": "queued",
            "created_by": user_id,
        })
        .execute()
        .data
    )

    if not row:
        raise RuntimeError("Failed to create render job")

    logger.info(
        "Render queued  course=%s concept=%s renderer=%s", course_id, concept_key, renderer
    )
    return row[0]


def mark(asset_id: str, **fields: Any) -> None:
    get_supabase().table("media_assets").update(fields).eq("id", asset_id).execute()


async def execute_render(asset_id: str) -> dict:
    """Run one queued job to completion. Called by the Celery render worker."""
    from app.media.storage_service import upload_rendered_media

    rows = get_supabase().table("media_assets").select("*").eq("id", asset_id).execute().data
    if not rows:
        raise LookupError(f"No media asset {asset_id}")
    asset = rows[0]

    course_id = asset["course_id"]
    mark(asset_id, status="rendering", error=None)
    _publish(course_id, {"type": "render_status", "asset_id": asset_id, "status": "rendering"})

    request = RenderRequest(
        asset_id=asset_id,
        course_id=course_id,
        concept_key=asset["concept_key"],
        source_script=asset.get("source_script") or "",
        archetype=asset.get("archetype"),
        topic=asset.get("topic"),
    )

    try:
        renderer = resolve(asset["renderer"])
        result = await renderer.render(request)
    except Exception as exc:  # noqa: BLE001 — every failure mode ends here
        logger.error("Render failed for %s: %s", asset_id, exc, exc_info=True)
        # Truncated: a Manim traceback can run to thousands of lines, and the
        # lecturer needs the reason, not the stack.
        mark(asset_id, status="failed", error=str(exc)[:2000])
        _publish(
            course_id,
            {"type": "render_status", "asset_id": asset_id, "status": "failed",
             "error": str(exc)[:300]},
        )
        return {"status": "failed", "asset_id": asset_id, "error": str(exc)[:300]}

    path = upload_rendered_media(
        course_id, asset_id, result.content, result.media_type, result.extension
    )

    if not path:
        mark(asset_id, status="failed", error="Render succeeded but the upload failed.")
        _publish(
            course_id,
            {"type": "render_status", "asset_id": asset_id, "status": "failed",
             "error": "upload failed"},
        )
        return {"status": "failed", "asset_id": asset_id}

    mark(
        asset_id,
        status="ready",
        storage_path=path,
        duration_seconds=result.duration_seconds,
        scene_code=result.scene_code,
        completed_at="now()",
    )

    # Deliberately not "available to students" — it is awaiting review.
    # Proposal §10 makes lecturer sign-off the mitigation for notation errors.
    _publish(
        course_id,
        {"type": "render_status", "asset_id": asset_id, "status": "ready",
         "awaiting_review": True},
    )
    logger.info("Render ready  asset=%s  path=%s", asset_id, path)
    return {"status": "ready", "asset_id": asset_id, "storage_path": path}


def approved_concept_keys(course_id: str) -> list[str]:
    """Concepts with an approved, rendered video on this course.

    Fed into the board prompt so the model can only reference videos that
    exist. Failures return an empty list rather than raising: a missing video
    list must degrade to a normal Lesson Board answer, never fail the turn.
    """
    try:
        rows = (
            get_supabase()
            .table("media_assets")
            .select("concept_key")
            .eq("course_id", course_id)
            .eq("status", "ready")
            .not_.is_("approved_at", "null")
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 — table may not exist yet
        logger.warning("Could not list approved videos for %s: %s", course_id, exc)
        return []

    return sorted({r["concept_key"] for r in rows if r.get("concept_key")})


def playable_asset(course_id: str, concept_key: str) -> dict | None:
    """The approved video for a concept, with a playback URL. None if there
    is not one — the caller falls back to the Lesson Board."""
    rows = (
        get_supabase()
        .table("media_assets")
        .select("id, concept_key, storage_path, duration_seconds, archetype, renderer")
        .eq("course_id", course_id)
        .eq("concept_key", concept_key)
        .eq("status", "ready")
        .not_.is_("approved_at", "null")
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        return None

    asset = rows[0]
    url = signed_url(RENDERED_MEDIA_BUCKET, asset.get("storage_path"), _PLAYBACK_URL_TTL)
    if not url:
        return None
    return {**asset, "url": url}
