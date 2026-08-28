"""Student management: invite codes, enrolments, status changes.

Ref: Meraki_AI_Lecturer_Side_Technical_Documentation §4.3
     Meraki_AI_Integration_Roadmap Part D (completion vs departure)
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, EmailStr, Field

from app.core import audit
from app.core.auth import assert_course_owner, lecturer_guard
from app.core.enrolment import ALL_STATUSES, generate_invite_code
from app.core.roster import RosterFormatError, parse_roster
from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

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


_MAX_ROSTER_BYTES = 5 * 1024 * 1024


@router.post("/students/import")
async def import_students(
    course_id: str,
    file: UploadFile,
    request: Request,
    user=Depends(lecturer_guard),
):
    """Enrol a whole class from a spreadsheet of names and email addresses.

    Two outcomes per row, and both are enrolments:

      * the address already has an account -> enrolled immediately;
      * it does not -> held as a pending invitation, which converts to a live
        enrolment the moment that person signs up.

    The second half is the point. At the start of a semester almost nobody on
    the list has registered yet, so an import that could only enrol existing
    accounts would report "0 enrolled" on a correct file and leave the lecturer
    with nothing to do but wait and re-upload.
    """
    assert_course_owner(user, course_id)

    contents = await file.read(_MAX_ROSTER_BYTES + 1)
    if len(contents) > _MAX_ROSTER_BYTES:
        raise HTTPException(
            status_code=413, detail="Roster files are limited to 5 MB."
        )
    if not contents:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    try:
        parsed = parse_roster(contents, file.filename or "")
    except RosterFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not parsed.rows:
        raise HTTPException(
            status_code=400,
            detail=(
                "No usable rows found. Every row needs an email address in a "
                "column headed 'Email'."
            ),
        )

    sb = get_supabase()
    # Lowercased for the lookup. Postgres `IN` is case-sensitive, Supabase Auth
    # stores addresses lowercased, and spreadsheets are full of "Ama@ug.edu.gh"
    # — matching on the raw value would report a registered student as
    # "no account yet" and quietly leave them off the course.
    emails = sorted({r.email.lower() for r in parsed.rows})

    # Two bulk lookups rather than two round trips per student — a 200-row
    # class list would otherwise be 400 sequential queries.
    accounts = (
        sb.table("users").select("id, email").in_("email", emails).execute().data or []
    )
    by_email = {a["email"].lower(): a["id"] for a in accounts}

    existing_enrolments = (
        sb.table("enrolments").select("id, student_id, status")
        .eq("course_id", course_id).execute().data or []
    )
    enrolment_by_student = {e["student_id"]: e for e in existing_enrolments}

    enrolled: list[str] = []
    reactivated: list[str] = []
    already: list[str] = []
    invited: list[str] = []
    failed: list[dict] = [
        {"row": row_no, "reason": reason} for row_no, reason in parsed.skipped
    ]

    to_insert: list[dict] = []
    invitations: list[dict] = []
    # student_id -> email, so a per-row retry can report the address the
    # lecturer typed rather than a uuid they have never seen.
    email_for: dict[str, str] = {}

    for row in parsed.rows:
        student_id = by_email.get(row.email.lower())

        if student_id is None:
            invitations.append({
                "course_id": course_id,
                # Lowercased to match the unique key the upsert conflicts on.
                "email": row.email.lower(),
                "first_name": row.first_name,
                "last_name": row.last_name,
                "status": "pending",
                "invited_by": user["id"],
            })
            invited.append(row.email)
            continue

        email_for[student_id] = row.email
        existing = enrolment_by_student.get(student_id)
        if existing is None:
            to_insert.append({
                "course_id": course_id, "student_id": student_id, "status": "active",
            })
            enrolled.append(row.email)
        elif existing["status"] == "active":
            already.append(row.email)
        else:
            # Re-importing a class list is how a lecturer readmits someone who
            # withdrew, so this reactivates rather than reporting a conflict.
            try:
                sb.table("enrolments").update(
                    {"status": "active", "withdrawn_at": None}
                ).eq("id", existing["id"]).execute()
                reactivated.append(row.email)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Roster reactivation failed for %s: %s", row.email, exc)
                failed.append({"email": row.email, "reason": "Could not reactivate"})

    if to_insert:
        try:
            sb.table("enrolments").insert(to_insert).execute()
        except Exception as exc:  # noqa: BLE001 — one bad row must not lose the batch
            # One bad row must not lose the other 199 — retry individually so
            # the failure is reported against the address that caused it.
            logger.warning("Bulk enrolment insert failed, retrying per row: %s", exc)
            enrolled = []
            for row_data in to_insert:
                email = email_for.get(row_data["student_id"], row_data["student_id"])
                try:
                    sb.table("enrolments").insert(row_data).execute()
                    enrolled.append(email)
                except Exception as row_exc:  # noqa: BLE001
                    failed.append({"email": email, "reason": str(row_exc)[:200]})

    if invitations:
        try:
            # on_conflict so a corrected re-upload updates the held names
            # instead of failing the whole import on the unique index.
            sb.table("enrolment_invitations").upsert(
                invitations, on_conflict="course_id,email"
            ).execute()
        except Exception as exc:  # noqa: BLE001 — 013 may not be applied yet
            logger.warning("Could not record pending invitations: %s", exc)
            for invitation in invitations:
                failed.append({
                    "email": invitation["email"],
                    "reason": (
                        "No account yet, and the pending-invitation table is "
                        "unavailable. Apply sql/013."
                    ),
                })
            invited = []

    audit.record(
        actor=user, action="enrolment.import", resource_type="enrolment",
        resource_id=None, course_id=course_id,
        new_values={
            "filename": file.filename,
            "rows": len(parsed.rows),
            "enrolled": len(enrolled),
            "invited": len(invited),
        },
        request=request,
    )

    return {
        "status": "ok",
        "filename": file.filename,
        "rows_read": len(parsed.rows) + len(parsed.skipped),
        "enrolled": len(enrolled),
        "reactivated": len(reactivated),
        "already_enrolled": len(already),
        # Named "invited" rather than folded into "enrolled": the lecturer must
        # know these students are not on the course until they sign up.
        "invited": len(invited),
        "invited_emails": invited[:200],
        "failed": failed[:200],
    }


@router.get("/students/invitations")
def list_invitations(course_id: str, user=Depends(lecturer_guard)):
    """Imported students who have not signed up yet."""
    assert_course_owner(user, course_id)
    try:
        rows = (
            get_supabase().table("enrolment_invitations")
            .select("id, email, first_name, last_name, status, created_at, accepted_at")
            .eq("course_id", course_id)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 — 013 may not be applied yet
        logger.warning("Invitation list unavailable for %s: %s", course_id, exc)
        return {"invitations": [], "available": False}

    return {"invitations": rows, "available": True}


@router.delete("/students/invitations/{invitation_id}")
def cancel_invitation(
    course_id: str, invitation_id: str, request: Request, user=Depends(lecturer_guard)
):
    """Withdraw a pending invitation so signing up no longer auto-enrols."""
    assert_course_owner(user, course_id)
    sb = get_supabase()
    updated = (
        sb.table("enrolment_invitations").update({"status": "cancelled"})
        .eq("id", invitation_id).eq("course_id", course_id)
        .eq("status", "pending").execute().data
    )
    if not updated:
        raise HTTPException(status_code=404, detail="No pending invitation with that id")

    audit.record(
        actor=user, action="enrolment.invitation_cancel", resource_type="enrolment",
        resource_id=invitation_id, course_id=course_id, request=request,
    )
    return {"status": "ok", "invitation_id": invitation_id}


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
