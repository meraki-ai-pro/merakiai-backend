"""Courses a lecturer owns.

Ref: Meraki_AI_Lecturer_Side_Technical_Documentation §2, §4.1
"""

from __future__ import annotations

import re
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core import audit
from app.core.academic_levels import AcademicLevel, as_options, normalise
from app.core.auth import ADMIN_ROLES, assert_course_owner, lecturer_guard
from app.db.supabase import get_supabase

router = APIRouter(prefix="/courses", tags=["Lecturer – Courses"])

# Single source of truth — see app/core/academic_levels.py

# courses.id is text and appears in Pinecone namespaces ("{course_id}-learn-v2")
# and storage paths, so it must be a clean slug rather than free text.
_COURSE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


class CourseCreate(BaseModel):
    id: str = Field(..., min_length=3, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    persona: str | None = None
    domain_topics: list[str] = []
    academic_level: AcademicLevel | None = None
    practice_mode_enabled: bool = True
    # Free text. Consulted only as a FALLBACK when a video request names no
    # archetype (app/media/render/routing.py), so "BSc Biology" and "Chemistry
    # II" both work and neither locks the course into one renderer.
    subject: str | None = Field(None, max_length=120)


class CourseUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    persona: str | None = None
    domain_topics: list[str] | None = None
    academic_level: AcademicLevel | None = None
    practice_mode_enabled: bool | None = None
    subject: str | None = Field(None, max_length=120)


def _owned_filter(query, user: dict):
    """Admins see everything; a lecturer sees only what they own."""
    if user["role"] in ADMIN_ROLES:
        return query
    return query.eq("owner_id", user["id"])


@router.get("/academic-levels")
def academic_levels(_user=Depends(lecturer_guard)):
    """The level vocabulary, in progression order.

    Served rather than hard-coded in the UI so the dropdown, the CHECK
    constraint and the teaching prompts can never disagree about what a level
    is called.
    """
    return {"levels": as_options()}


@router.get("")
def list_courses(user=Depends(lecturer_guard)):
    """Multi-course dashboard: every course this lecturer owns, with counts."""
    sb = get_supabase()
    courses = (
        _owned_filter(sb.table("courses").select("*"), user)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    if not courses:
        return {"courses": []}

    ids = [c["id"] for c in courses]

    # Counted in two bulk queries rather than per course — a dashboard with a
    # dozen courses should not fire two dozen round trips.
    docs = (
        sb.table("documents").select("course_id, is_published")
        .in_("course_id", ids).limit(5000).execute().data or []
    )
    enrolments = (
        sb.table("enrolments").select("course_id, status")
        .in_("course_id", ids).limit(20000).execute().data or []
    )

    for course in courses:
        cid = course["id"]
        course_docs = [d for d in docs if d["course_id"] == cid]
        course["document_count"] = len(course_docs)
        course["published_document_count"] = sum(
            1 for d in course_docs if d.get("is_published") is not False
        )
        course["student_count"] = sum(
            1 for e in enrolments if e["course_id"] == cid and e["status"] == "active"
        )

    return {"courses": courses}


@router.post("")
def create_course(payload: CourseCreate, request: Request, user=Depends(lecturer_guard)):
    if not _COURSE_ID_RE.match(payload.id):
        raise HTTPException(
            status_code=400,
            detail=(
                "Course id must be lowercase letters, digits and hyphens "
                "(e.g. 'calculus-101') — it is used in storage paths and "
                "search namespaces."
            ),
        )

    sb = get_supabase()
    if sb.table("courses").select("id").eq("id", payload.id).execute().data:
        raise HTTPException(status_code=409, detail=f"Course '{payload.id}' already exists")

    row = {
        **payload.model_dump(),
        # Accepts "Level 200", "L200" or "200" — a lecturer should not have to
        # learn our slug format to fill in a form.
        "academic_level": normalise(payload.academic_level),
        # Always the caller, never a value from the payload — otherwise a
        # lecturer could create a course owned by someone else.
        "owner_id": user["id"],
    }
    created = sb.table("courses").insert(row).execute().data
    if not created:
        raise HTTPException(status_code=500, detail="Failed to create course")

    audit.record(
        actor=user, action="course.create", resource_type="course",
        resource_id=payload.id, course_id=payload.id,
        new_values=row, request=request,
    )
    return {"status": "ok", "course": created[0]}


@router.get("/{course_id}")
def get_course(course_id: str, user=Depends(lecturer_guard)):
    assert_course_owner(user, course_id)
    sb = get_supabase()
    course = sb.table("courses").select("*").eq("id", course_id).execute().data
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    return {"course": course[0]}


@router.patch("/{course_id}")
def update_course(
    course_id: str, payload: CourseUpdate, request: Request, user=Depends(lecturer_guard)
):
    assert_course_owner(user, course_id)

    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "academic_level" in updates:
        updates["academic_level"] = normalise(updates["academic_level"])

    sb = get_supabase()
    before = sb.table("courses").select("*").eq("id", course_id).execute().data
    updated = sb.table("courses").update(updates).eq("id", course_id).execute().data
    if not updated:
        raise HTTPException(status_code=404, detail="Course not found")

    audit.record(
        actor=user, action="course.update", resource_type="course",
        resource_id=course_id, course_id=course_id,
        old_values=before[0] if before else None, new_values=updates, request=request,
    )
    return {"status": "ok", "course": updated[0]}
