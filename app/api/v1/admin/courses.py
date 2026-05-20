from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import admin_guard
from app.db.supabase import get_supabase

router = APIRouter(prefix="/courses", tags=["Admin – Courses"])


class CourseCreatePayload(BaseModel):
    id: str
    name: str
    description: str | None = None
    persona: str | None = None
    domain_topics: list[str] = []


class CourseUpdatePayload(BaseModel):
    name: str | None = None
    description: str | None = None
    persona: str | None = None
    domain_topics: list[str] | None = None


@router.get("")
def list_courses(_user=Depends(admin_guard)):
    sb = get_supabase()
    courses = sb.table("courses").select("*").order("created_at", desc=True).execute().data or []

    # M-19: fetch only course_id with a row cap — avoids a full-table scan.
    # For very large deployments this may under-count if > 10 000 docs exist,
    # but is safe for the expected scale and far cheaper than a full scan.
    from collections import Counter
    doc_rows = sb.table("documents").select("course_id").limit(10000).execute().data or []
    doc_counts = Counter(r["course_id"] for r in doc_rows if r.get("course_id"))

    for c in courses:
        c["document_count"] = doc_counts.get(c["id"], 0)

    return {"courses": courses}


@router.post("")
def create_course(payload: CourseCreatePayload, _user=Depends(admin_guard)):
    sb = get_supabase()
    res = sb.table("courses").insert({
        "id": payload.id,
        "name": payload.name,
        "description": payload.description,
        "persona": payload.persona,
        "domain_topics": payload.domain_topics,
    }).execute()
    if not res.data:
        raise HTTPException(status_code=409, detail="Course already exists or insert failed")
    return {"status": "ok", "course": res.data[0]}


@router.get("/{course_id}")
def get_course(course_id: str, _user=Depends(admin_guard)):
    sb = get_supabase()
    course = sb.table("courses").select("*").eq("id", course_id).single().execute().data
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    docs = sb.table("documents").select(
        "id, title, doc_type, status, difficulty, total_chunks, created_at"
    ).eq("course_id", course_id).execute().data or []

    return {"course": course, "documents": docs}


@router.patch("/{course_id}")
def update_course(course_id: str, payload: CourseUpdatePayload, _user=Depends(admin_guard)):
    sb = get_supabase()
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    res = sb.table("courses").update(updates).eq("id", course_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Course not found")
    return {"status": "ok", "course": res.data[0]}


@router.delete("/{course_id}")
def delete_course(course_id: str, user=Depends(admin_guard)):
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="Only super admins can delete courses")

    sb = get_supabase()
    # Check no documents are still attached (must delete documents first)
    docs = sb.table("documents").select("id", count="exact").eq("course_id", course_id).limit(0).execute()
    if (docs.count or 0) > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Course has {docs.count} document(s). Delete all documents first.",
        )

    res = sb.table("courses").delete().eq("id", course_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Course not found")
    return {"status": "ok", "deleted_course_id": course_id}
