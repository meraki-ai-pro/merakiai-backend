"""Student-facing enrolment: join a course, see what you are on.

Lecturer-side enrolment management (add/remove students, bulk CSV, status
changes) lives with the rest of the lecturer surface — see task #19.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core import events
from app.core.auth import auth_guard
from app.core.enrolment import redeem_code
from app.db.supabase import get_user_client

router = APIRouter(prefix="/enrolments", tags=["Enrolments"])


class JoinPayload(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)


@router.post("/join")
def join_course(payload: JoinPayload, user=Depends(auth_guard)):
    """Redeem an invite code.

    All validation, the uses_count increment and the enrolment insert happen
    inside one SQL transaction (redeem_invite_code) so two students racing for
    the last seat cannot both win.
    """
    enrolment = redeem_code(payload.code, user["token"])
    events.emit(
        events.ENROLMENT_CREATED,
        user_id=user["id"],
        course_id=enrolment.get("course_id"),
        payload={"via": "invite_code"},
    )
    return {"status": "ok", "enrolment": enrolment}


@router.get("")
def list_my_enrolments(user=Depends(auth_guard)):
    """Every course this student is on. RLS restricts this to their own rows."""
    rows = (
        get_user_client(user["token"])
        .table("enrolments")
        .select("id, course_id, status, enrolled_at, completed_at")
        .order("enrolled_at", desc=True)
        .execute()
        .data
        or []
    )
    return {"enrolments": rows}
