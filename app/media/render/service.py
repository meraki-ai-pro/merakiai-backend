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

# Read here as well as in app/media/narration.py so the render worker can mark
# an asset 'skipped' immediately instead of queueing work that will be dropped.
# Not imported from that module: it pulls the TTS stack, which is deliberately
# absent from this container.
NARRATION_ENABLED = os.getenv("RENDER_NARRATION", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

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
    parent_asset_id: str | None = None,
    revision_note: str | None = None,
    revision: int = 1,
) -> dict:
    """Create or reuse a render job. Returns the media_assets row.

    Re-requesting an unchanged concept is a cache hit, not a second job — a
    multi-minute render must not be repeated because someone clicked twice.

    ``parent_asset_id`` marks this as a lecturer's revision of an earlier
    video. The cache still applies: reverting a prompt to exactly what it was
    two revisions ago returns the render that was already made from it.
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

    new_row = {
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
    }
    revision_fields = {
        "parent_asset_id": parent_asset_id,
        "revision": revision,
        "revision_note": revision_note,
    }

    sb = get_supabase()
    try:
        row = sb.table("media_assets").insert({**new_row, **revision_fields}).execute().data
    except Exception as exc:  # noqa: BLE001 — sql/013 may not be applied yet
        logger.warning(
            "media_assets rejected the revision columns; queueing without them. "
            "Apply sql/013_roster_import_narration_and_upload_tags.sql: %s", exc,
        )
        row = sb.table("media_assets").insert(new_row).execute().data

    if not row:
        raise RuntimeError("Failed to create render job")

    logger.info(
        "Render queued  course=%s concept=%s renderer=%s", course_id, concept_key, renderer
    )
    return row[0]


def mark(asset_id: str, **fields: Any) -> None:
    get_supabase().table("media_assets").update(fields).eq("id", asset_id).execute()


def _mark_optional(asset_id: str, **fields: Any) -> None:
    """mark(), for columns a database that predates sql/013 may not have.

    Narration bookkeeping must never fail a render that has already produced a
    watchable video.
    """
    try:
        mark(asset_id, **fields)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not write %s on asset %s; apply sql/013: %s",
            ", ".join(fields), asset_id, exc,
        )


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

    narration_queued = _queue_narration(asset_id)

    # Deliberately not "available to students" — it is awaiting review.
    # Proposal §10 makes lecturer sign-off the mitigation for notation errors.
    _publish(
        course_id,
        {"type": "render_status", "asset_id": asset_id, "status": "ready",
         "awaiting_review": True, "narrating": narration_queued},
    )
    logger.info("Render ready  asset=%s  path=%s", asset_id, path)
    return {
        "status": "ready",
        "asset_id": asset_id,
        "storage_path": path,
        "narration_queued": narration_queued,
    }


def _queue_narration(asset_id: str) -> bool:
    """Hand the silent render to the media worker to have a voice added.

    Dispatched BY NAME, like the render task itself and for the same reason in
    reverse: this code runs inside the render container, which has no
    ElevenLabs client and no ffmpeg-backed media stack. Importing the narration
    task here would fail at import time; publishing a message does not.

    Best-effort. A broker hiccup costs the narration, not the render — the
    asset is already `ready` and the lecturer can review a silent video.
    """
    if not NARRATION_ENABLED:
        _mark_optional(asset_id, narration_status="skipped")
        return False

    try:
        from app.core.celery_app import celery_app

        celery_app.send_task(
            "app.ai.tasks.process_narration_task",
            args=[asset_id],
            queue="video_tasks",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not queue narration for asset %s: %s", asset_id, exc)
        _mark_optional(asset_id, narration_status="failed")
        return False

    _mark_optional(asset_id, narration_status="pending")
    return True


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


_PLAYABLE_COLUMNS = "id, concept_key, storage_path, duration_seconds, archetype, renderer"


def playable_asset(course_id: str, concept_key: str) -> dict | None:
    """The approved video for a concept, with a playback URL. None if there
    is not one — the caller falls back to the Lesson Board."""

    def _query(columns: str):
        return (
            get_supabase()
            .table("media_assets")
            .select(columns)
            .eq("course_id", course_id)
            .eq("concept_key", concept_key)
            .eq("status", "ready")
            .not_.is_("approved_at", "null")
            # Newest approval wins. Regeneration means one concept can have
            # several approved renders, and without an explicit order the
            # student would get whichever row Postgres happened to return —
            # quite possibly the one the lecturer replaced.
            .order("approved_at", desc=True)
            .limit(1)
            .execute()
            .data
        )

    try:
        rows = _query(f"{_PLAYABLE_COLUMNS}, has_audio")
    except Exception:  # noqa: BLE001 — sql/013 may not be applied yet
        rows = _query(_PLAYABLE_COLUMNS)

    if not rows:
        return None

    asset = rows[0]
    url = signed_url(RENDERED_MEDIA_BUCKET, asset.get("storage_path"), _PLAYBACK_URL_TTL)
    if not url:
        return None
    return {**asset, "url": url}
