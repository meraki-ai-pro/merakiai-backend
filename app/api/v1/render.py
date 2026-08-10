"""Lecturer-facing render endpoints: request, review, approve.

The approval gate is the point of this router. Proposal §10 names "video
quality or notation errors" as a named risk and lecturer review of every video
before release as the mitigation. An LLM-generated animation with a flipped
sign reaching a Ghanaian lecture hall unreviewed is the failure mode that would
actually damage the pilot, so students only ever see rows with approved_at set
— enforced in the RLS policy as well as here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import assert_course_owner, auth_guard, lecturer_guard
from app.core.enrolment import require_enrolment
from app.db.supabase import get_supabase
from app.media.render.routing import UnsupportedArchetypeError, known_archetypes
from app.media.render.service import playable_asset, request_render

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
            subject=payload.subject,
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
            queue="render_tasks",
        )

    return {"status": asset["status"], "asset": asset}


@router.get("/course/{course_id}")
def list_assets(course_id: str, status: str | None = None, user=Depends(lecturer_guard)):
    """Every asset for a course — the lecturer's render and review queue."""
    assert_course_owner(user, course_id)

    query = (
        get_supabase()
        .table("media_assets")
        .select(
            "id, concept_key, topic, renderer, archetype, status, error, "
            "duration_seconds, approved_at, rejected_at, review_note, created_at"
        )
        .eq("course_id", course_id)
        .order("created_at", desc=True)
    )
    if status:
        query = query.eq("status", status)

    return {"assets": query.execute().data or []}


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
    rows = (
        get_supabase()
        .table("media_assets")
        .select("id, course_id, status")
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

    get_supabase().table("media_assets").update(update).eq("id", asset_id).execute()
    return {"status": "ok", "asset_id": asset_id, "approved": payload.approved}


@router.get("/concept/{course_id}/{concept_key}")
def get_playable(course_id: str, concept_key: str, user=Depends(auth_guard)):
    """Student-facing: the approved video for a concept, if there is one.

    A null asset is the normal case, not an error — the client falls back to
    the Lesson Board, which is instant and covers every concept.
    """
    require_enrolment(user, course_id)
    return {"asset": playable_asset(course_id, concept_key)}
