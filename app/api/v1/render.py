"""Lecturer-facing render endpoints: request, review, approve.

The approval gate is the point of this router. Proposal §10 names "video
quality or notation errors" as a named risk and lecturer review of every video
before release as the mitigation. An LLM-generated animation with a flipped
sign reaching a Ghanaian lecture hall unreviewed is the failure mode that would
actually damage the pilot, so students only ever see rows with approved_at set
— enforced in the RLS policy as well as here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import assert_course_owner, auth_guard, lecturer_guard
from app.core.enrolment import require_enrolment
from app.db.supabase import get_supabase
from app.media.render.routing import (
    UnsupportedArchetypeError,
    known_archetypes,
    render_queue,
)
from app.media.render.service import playable_asset, request_render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/render", tags=["Render"])


class RenderRequestPayload(BaseModel):
    course_id: str = Field(..., max_length=100)
    concept_key: str = Field(..., min_length=1, max_length=120)
    source_script: str = Field(..., min_length=1)
    archetype: str | None = None
    topic: str | None = None
    subject: str | None = None


class ReviewPayload(BaseModel):
    approved: bool
    note: str | None = Field(None, max_length=2000)


class RegeneratePayload(BaseModel):
    """A revised prompt for an existing concept.

    Only ``source_script`` is required: the common case is a lecturer changing
    the wording of what the video should show and leaving everything else
    alone. Omitted fields are inherited from the asset being revised, so a
    regenerate never silently reroutes a Biology video to Manim because the
    form did not resend the archetype.
    """

    source_script: str = Field(..., min_length=1)
    archetype: str | None = None
    topic: str | None = None
    subject: str | None = None
    note: str | None = Field(None, max_length=2000)


@router.get("/archetypes")
def list_archetypes(_user=Depends(lecturer_guard)):
    """Archetypes a lesson script may name, and which renderer each selects."""
    from app.media.render.routing import MANIM_ARCHETYPES, REMOTION_ARCHETYPES

    return {
        "archetypes": [
            {"name": a, "renderer": "manim" if a in MANIM_ARCHETYPES else "remotion"}
            for a in known_archetypes()
        ],
        "unsupported": sorted(
            {"molecular_structure", "anatomical_model"}
        ),
    }


def _course_subject(course_id: str) -> str | None:
    """The course's own subject, for renderer routing.

    Read here rather than trusted from the client. The lecturer UI used to send
    a hard-coded ``subject="mathematics"`` on every request, which meant a
    Biology or Chemistry course with no archetype named was routed to Manim —
    an animation engine for continuous mathematics — and produced a plausible
    but useless equation-shaped video. Falls back to the course name, which for
    "BSc Chemistry" or "Organic Chemistry II" is a better hint than nothing.
    """
    try:
        rows = (
            get_supabase().table("courses").select("subject, name")
            .eq("id", course_id).limit(1).execute().data or []
        )
    except Exception as exc:  # noqa: BLE001 — sql/013 may not be applied yet
        logger.warning("Could not read subject for course %s: %s", course_id, exc)
        return None

    if not rows:
        return None
    return (rows[0].get("subject") or rows[0].get("name") or "").strip() or None


@router.post("")
def create_render(payload: RenderRequestPayload, user=Depends(lecturer_guard)):
    """Queue a concept video. Returns immediately — renders take minutes.

    A repeat request for an unchanged script is a cache hit, not a second job.
    """
    assert_course_owner(user, payload.course_id)

    try:
        asset = request_render(
            course_id=payload.course_id,
            concept_key=payload.concept_key,
            source_script=payload.source_script,
            user_id=user["id"],
            archetype=payload.archetype,
            subject=payload.subject or _course_subject(payload.course_id),
            topic=payload.topic,
        )
    except UnsupportedArchetypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if asset["status"] == "queued":
        # Dispatched by NAME, not by importing the task. The API process has no
        # manim and no LaTeX; importing app.media.render.tasks here would fail.
        from app.core.celery_app import celery_app

        celery_app.send_task(
            "app.media.render.tasks.process_render_task",
            args=[asset["id"]],
            # The asset's renderer decides the queue — a Manim job must not
            # reach the Remotion worker, which cannot serve it.
            queue=render_queue(asset["renderer"]),
        )

    return {"status": asset["status"], "asset": asset}


_ASSET_COLUMNS = (
    "id, concept_key, topic, renderer, archetype, status, error, "
    "duration_seconds, approved_at, rejected_at, review_note, created_at"
)
_ASSET_COLUMNS_V13 = (
    f"{_ASSET_COLUMNS}, has_audio, narration_status, revision, "
    "parent_asset_id, revision_note"
)


@router.get("/course/{course_id}")
def list_assets(course_id: str, status: str | None = None, user=Depends(lecturer_guard)):
    """Every asset for a course — the lecturer's render and review queue."""
    assert_course_owner(user, course_id)

    def _query(columns: str):
        query = (
            get_supabase()
            .table("media_assets")
            .select(columns)
            .eq("course_id", course_id)
            .order("created_at", desc=True)
        )
        if status:
            query = query.eq("status", status)
        return query.execute().data or []

    try:
        return {"assets": _query(_ASSET_COLUMNS_V13)}
    except Exception:  # noqa: BLE001 — sql/013 may not be applied yet
        return {"assets": _query(_ASSET_COLUMNS)}


@router.post("/{asset_id}/regenerate")
def regenerate_asset(
    asset_id: str, payload: RegeneratePayload, user=Depends(lecturer_guard)
):
    """Re-render a concept from an edited prompt.

    A new asset rather than an overwrite. The old video keeps serving students
    until the new one is approved, the lecturer can compare the two, and the
    revision they rejected stays on record with the note explaining why —
    overwriting in place would mean a bad regeneration leaves the course with
    no video at all while it re-renders.
    """
    sb = get_supabase()
    rows = sb.table("media_assets").select("*").eq("id", asset_id).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail="Asset not found")

    original = rows[0]
    assert_course_owner(user, original["course_id"])

    if payload.source_script.strip() == (original.get("source_script") or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "The prompt is unchanged, so this would return the same video. "
                "Edit what the animation should show, then regenerate."
            ),
        )

    try:
        asset = request_render(
            course_id=original["course_id"],
            concept_key=original["concept_key"],
            source_script=payload.source_script,
            user_id=user["id"],
            # Inherited unless explicitly overridden — see RegeneratePayload.
            archetype=payload.archetype or original.get("archetype"),
            subject=payload.subject or _course_subject(original["course_id"]),
            topic=payload.topic or original.get("topic"),
            parent_asset_id=asset_id,
            revision_note=payload.note,
            revision=int(original.get("revision") or 1) + 1,
        )
    except UnsupportedArchetypeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if asset["status"] == "queued":
        from app.core.celery_app import celery_app

        celery_app.send_task(
            "app.media.render.tasks.process_render_task",
            args=[asset["id"]],
            # The asset's renderer decides the queue — a Manim job must not
            # reach the Remotion worker, which cannot serve it.
            queue=render_queue(asset["renderer"]),
        )

    return {"status": asset["status"], "asset": asset, "replaces": asset_id}


@router.get("/{asset_id}")
def get_asset(asset_id: str, user=Depends(lecturer_guard)):
    """One asset with its generated scene code and a preview URL.

    scene_code is included so the lecturer (or we) can see exactly what was
    executed — a render that produced wrong mathematics is much easier to fix
    when the source is in front of you.
    """
    from app.media.render.service import RENDERED_MEDIA_BUCKET
    from app.media.storage_service import signed_url

    rows = get_supabase().table("media_assets").select("*").eq("id", asset_id).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset = rows[0]
    assert_course_owner(user, asset["course_id"])

    preview = signed_url(RENDERED_MEDIA_BUCKET, asset.get("storage_path"))
    return {"asset": asset, "preview_url": preview}


@router.post("/{asset_id}/review")
def review_asset(asset_id: str, payload: ReviewPayload, user=Depends(lecturer_guard)):
    """Approve or reject. Nothing reaches a student until this says approved."""
    sb = get_supabase()
    rows = (
        sb.table("media_assets")
        .select("id, course_id, status, concept_key")
        .eq("id", asset_id)
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset = rows[0]
    assert_course_owner(user, asset["course_id"])

    if payload.approved and asset["status"] != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot approve an asset with status {asset['status']!r}.",
        )

    if payload.approved:
        update = {
            "approved_by": user["id"],
            "approved_at": "now()",
            "rejected_at": None,
            "review_note": payload.note,
        }
    else:
        # Approval is cleared as well as rejection set, so revoking a previously
        # approved video actually removes it from students rather than leaving
        # both timestamps populated and the RLS policy still satisfied.
        update = {
            "approved_by": None,
            "approved_at": None,
            "rejected_at": "now()",
            "review_note": payload.note,
        }

    sb.table("media_assets").update(update).eq("id", asset_id).execute()

    superseded = 0
    if payload.approved:
        # Approving a revision retires the video it replaces. Without this a
        # concept accumulates approved renders, and while playable_asset picks
        # the newest, the lecturer's list would show three "Live" videos for
        # one concept and no way to tell which students actually get.
        try:
            others = (
                sb.table("media_assets")
                .update({"approved_by": None, "approved_at": None,
                         "review_note": "Superseded by a newer approved version."})
                .eq("course_id", asset["course_id"])
                .eq("concept_key", asset["concept_key"])
                .neq("id", asset_id)
                .not_.is_("approved_at", "null")
                .execute()
                .data
                or []
            )
            superseded = len(others)
        except Exception as exc:  # noqa: BLE001 — the approval itself already landed
            logger.warning("Could not retire older approvals for %s: %s", asset_id, exc)

    return {
        "status": "ok",
        "asset_id": asset_id,
        "approved": payload.approved,
        "superseded": superseded,
    }


@router.get("/concept/{course_id}/{concept_key}")
def get_playable(course_id: str, concept_key: str, user=Depends(auth_guard)):
    """Student-facing: the approved video for a concept, if there is one.

    A null asset is the normal case, not an error — the client falls back to
    the Lesson Board, which is instant and covers every concept.
    """
    require_enrolment(user, course_id)
    return {"asset": playable_asset(course_id, concept_key)}
