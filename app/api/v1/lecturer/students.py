"""Student management: invite codes, enrolments, status changes.

Ref: Meraki_AI_Lecturer_Side_Technical_Documentation §4.3
     Meraki_AI_Integration_Roadmap Part D (completion vs departure)
"""

from __future__ import annotations

import csv
import io
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.core import audit
from app.core.auth import assert_course_owner, lecturer_guard
from app.core.enrolment import ALL_STATUSES, generate_invite_code
from app.db.supabase import get_supabase

router = APIRouter(prefix="/courses/{course_id}", tags=["Lecturer – Students"])

EnrolmentStatus = Literal["active", "completed", "withdrawn", "archived"]


class InviteCreate(BaseModel):
    max_uses: int | None = Field(None, gt=0, le=10000)
    expires_at: str | None = None


class AddStudent(BaseModel):
    email: EmailStr


class StatusChange(BaseModel):
    status: EnrolmentStatus


# ── Invite codes ────────────────────────────────────────────────────────────

@router.get("/invite-codes")
def list_invite_codes(course_id: str, user=Depends(lecturer_guard)):
    assert_course_owner(user, course_id)
    rows = (
        get_supabase().table("invite_codes")
        .select("id, code, max_uses, uses_count, expires_at, is_active, created_at")
        .eq("course_id", course_id).order("created_at", desc=True).execute().data or []
    )
    return {"invite_codes": rows}


@router.post("/invite-codes")
def create_invite_code(
    course_id: str, payload: InviteCreate, request: Request, user=Depends(lecturer_guard)
):
    assert_course_owner(user, course_id)
    sb = get_supabase()

    # Retry on collision rather than trusting 29^7 to never repeat. The unique
    # index is the real guarantee; this stops it surfacing as a 500.
    for _ in range(5):
        code = generate_invite_code()
        try:
            created = sb.table("invite_codes").insert({
                "course_id": course_id,
                "code": code,
                "created_by": user["id"],
                "max_uses": payload.max_uses,
                "expires_at": payload.expires_at,
            }).execute().data
        except Exception:  # noqa: BLE001 — unique violation, try another code
            continue
        if created:
            audit.record(
                actor=user, action="invite.create", resource_type="invite_code",
                resource_id=created[0]["id"], course_id=course_id,
                new_values={"max_uses": payload.max_uses, "expires_at": payload.expires_at},
                request=request,
            )
            return {"status": "ok", "invite_code": created[0]}

    raise HTTPException(status_code=500, detail="Could not allocate an invite code")


@router.delete("/invite-codes/{code_id}")
def deactivate_invite_code(
    course_id: str, code_id: str, request: Request, user=Depends(lecturer_guard)
):
    """Deactivate rather than delete — students who already redeemed it keep
    their enrolment, and the record of how they joined survives."""
    assert_course_owner(user, course_id)
    sb = get_supabase()
    updated = (
        sb.table("invite_codes").update({"is_active": False})
        .eq("id", code_id).eq("course_id", course_id).execute().data
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Invite code not found on this course")

    audit.record(
        actor=user, action="invite.deactivate", resource_type="invite_code",
        resource_id=code_id, course_id=course_id, request=request,
    )
    return {"status": "ok", "code_id": code_id, "is_active": False}


# ── Enrolments ──────────────────────────────────────────────────────────────

@router.get("/students")
def list_students(course_id: str, status: str | None = None, user=Depends(lecturer_guard)):
    assert_course_owner(user, course_id)
    sb = get_supabase()

    query = sb.table("enrolments").select(
        "id, student_id, status, enrolled_at, completed_at, withdrawn_at"
    ).eq("course_id", course_id)
    if status:
        if status not in ALL_STATUSES:
            raise HTTPException(status_code=400, detail=f"Unknown status {status!r}")
        query = query.eq("status", status)

    enrolments = query.order("enrolled_at", desc=True).execute().data or []
    if not enrolments:
        return {"students": []}

    # One bulk lookup rather than per row.
    ids = list({e["student_id"] for e in enrolments})
    profiles = (
        sb.table("users").select("id, email, first_name, last_name, university_name")
        .in_("id", ids).execute().data or []
    )
    by_id = {p["id"]: p for p in profiles}

    return {
        "students": [
            {**e, "profile": by_id.get(e["student_id"])} for e in enrolments
        ]
    }


@router.post("/students")
def add_student(
    course_id: str, payload: AddStudent, request: Request, user=Depends(lecturer_guard)
):
    """Enrol an existing account by email.

    Cannot create accounts — a lecturer adding an email that has never signed
    up would otherwise silently do nothing. They get told to send an invite
    code instead, which is the flow that works for a new student.
    """
    assert_course_owner(user, course_id)
    sb = get_supabase()

    found = sb.table("users").select("id, email").eq("email", payload.email).execute().data
    if not found:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No account for {payload.email}. Share an invite code so they "
                "can sign up and enrol themselves."
            ),
        )

    student_id = found[0]["id"]
    existing = (
        sb.table("enrolments").select("id, status")
        .eq("course_id", course_id).eq("student_id", student_id).execute().data
    )
    if existing:
        if existing[0]["status"] == "active":
            raise HTTPException(status_code=409, detail="Already enrolled on this course")
        sb.table("enrolments").update(
            {"status": "active", "withdrawn_at": None}
        ).eq("id", existing[0]["id"]).execute()
        enrolment_id = existing[0]["id"]
    else:
        created = sb.table("enrolments").insert({
            "course_id": course_id, "student_id": student_id, "status": "active",
        }).execute().data
        enrolment_id = created[0]["id"] if created else None

    audit.record(
        actor=user, action="enrolment.add", resource_type="enrolment",
        resource_id=enrolment_id, course_id=course_id,
        new_values={"email": payload.email, "student_id": student_id}, request=request,
    )
    return {"status": "ok", "enrolment_id": enrolment_id, "student_id": student_id}


@router.patch("/students/{enrolment_id}")
def change_status(
    course_id: str,
    enrolment_id: str,
    payload: StatusChange,
    request: Request,
    user=Depends(lecturer_guard),
):
    """Mark completed, withdraw, or archive.

    Completion is not removal: a completed student keeps read and practice
    access to the material (Permission Checks §3.2). Only withdrawal cuts them
    off, and that takes effect on their very next turn.
    """
    assert_course_owner(user, course_id)
    sb = get_supabase()

    before = (
        sb.table("enrolments").select("*")
        .eq("id", enrolment_id).eq("course_id", course_id).execute().data
    )
    if not before:
        raise HTTPException(status_code=404, detail="Enrolment not found on this course")

    updates: dict = {"status": payload.status}
    if payload.status == "completed":
        updates["completed_at"] = "now()"
    elif payload.status == "withdrawn":
        updates["withdrawn_at"] = "now()"

    sb.table("enrolments").update(updates).eq("id", enrolment_id).execute()

    audit.record(
        actor=user, action=f"enrolment.{payload.status}", resource_type="enrolment",
        resource_id=enrolment_id, course_id=course_id,
        old_values={"status": before[0]["status"]}, new_values=updates, request=request,
    )
    return {"status": "ok", "enrolment_id": enrolment_id, "new_status": payload.status}


@router.get("/students/export")
def export_students(course_id: str, user=Depends(lecturer_guard)):
    """CSV of the class list, for the lecturer's own records."""
    assert_course_owner(user, course_id)
    sb = get_supabase()

    enrolments = (
        sb.table("enrolments").select("student_id, status, enrolled_at, completed_at")
        .eq("course_id", course_id).execute().data or []
    )
    ids = list({e["student_id"] for e in enrolments})
    profiles = (
        sb.table("users").select("id, email, first_name, last_name")
        .in_("id", ids).execute().data or []
    ) if ids else []
    by_id = {p["id"]: p for p in profiles}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["email", "first_name", "last_name", "status", "enrolled_at", "completed_at"])
    for e in enrolments:
        p = by_id.get(e["student_id"], {})
        writer.writerow([
            p.get("email", ""), p.get("first_name", ""), p.get("last_name", ""),
            e["status"], e.get("enrolled_at", ""), e.get("completed_at", "") or "",
        ])

    return {"filename": f"{course_id}-students.csv", "csv": buf.getvalue()}
