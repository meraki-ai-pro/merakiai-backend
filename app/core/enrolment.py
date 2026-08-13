"""Course membership: who may be on a course, and in what state.

Reads go through the service-role client deliberately. Enrolment is an
*authorisation input* — it decides whether a request proceeds at all — so it
must be answerable independently of the caller's own RLS visibility. RLS on
`enrolments` still governs what the client can read directly.

Ref: Meraki_AI_Student_Permission_Checks §3.2, §4.1
     Meraki_AI_Integration_Roadmap §B.2
"""

from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException, status

from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

# Statuses that still grant access to course material. 'completed' is included
# because a student who finished the course keeps read/practice access
# (Permission Checks §3.2) — losing your notes the day you pass is not a
# behaviour anyone wants.
ACTIVE_STATUSES = ("active", "completed")

# Statuses permitting *new* work rather than review of old work.
PARTICIPATING_STATUSES = ("active",)

ALL_STATUSES = ("active", "completed", "withdrawn", "archived")

# Roles that skip the enrolment check entirely.
_BYPASS_ROLES = ("admin", "super_admin")

# Ambiguous glyphs removed: these codes get read off a projector and typed by
# hand, so O/0, I/1/L and S/5 collisions turn into support tickets.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRTUVWXYZ2346789"
_CODE_LENGTH = 7


def generate_invite_code(length: int = _CODE_LENGTH) -> str:
    """Cryptographically random, unambiguous, upper-case."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def get_enrolment(course_id: str, student_id: str) -> dict | None:
    rows = (
        get_supabase()
        .table("enrolments")
        .select("id, course_id, student_id, status, enrolled_at, completed_at")
        .eq("course_id", course_id)
        .eq("student_id", student_id)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _resolve_role(user: dict) -> str:
    """Get the caller's role, querying only if the guard did not supply one.

    auth_guard deliberately omits the role — adding a lookup there would put an
    extra round trip on every authenticated request, including the turn hot
    path. Staff are rare and always fail the enrolment check first, so paying
    for the lookup on that branch alone costs the common case nothing.
    """
    role = user.get("role")
    if role:
        return role
    try:
        rows = (
            get_supabase()
            .table("users")
            .select("role")
            .eq("id", user["id"])
            .execute()
            .data
        )
    except Exception as exc:  # noqa: BLE001 — treated as "not staff"
        logger.warning("Role lookup failed for %s: %s", user.get("id"), exc)
        return "user"
    return rows[0]["role"] if rows else "user"


def list_enrolled_course_ids(
    student_id: str,
    statuses: tuple[str, ...] = ACTIVE_STATUSES,
) -> list[str]:
    rows = (
        get_supabase()
        .table("enrolments")
        .select("course_id, status")
        .eq("student_id", student_id)
        .in_("status", list(statuses))
        .execute()
        .data
        or []
    )
    return [r["course_id"] for r in rows]


def require_enrolment(
    user: dict,
    course_id: str,
    allowed_statuses: tuple[str, ...] = ACTIVE_STATUSES,
) -> dict | None:
    """Check 3 of the permission stack. Returns the enrolment, or None for staff.

    Order matters: the enrolment lookup runs first because an enrolled student
    is the overwhelmingly common case and resolves in one indexed query. Staff
    bypass is evaluated only once that has failed.

    Lecturers are admitted to courses they own — they must be able to walk
    through their own material as a student sees it without enrolling in it.
    """
    enrolment = get_enrolment(course_id, user["id"])

    if enrolment and enrolment["status"] in allowed_statuses:
        return enrolment

    role = _resolve_role(user)

    if role in _BYPASS_ROLES:
        return None

    if role == "lecturer":
        from app.core.auth import assert_course_owner

        assert_course_owner({**user, "role": role}, course_id)
        return None

    if not enrolment:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not enrolled on this course.",
        )

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=_status_message(enrolment["status"]),
    )


def _status_message(current: str) -> str:
    return {
        "completed": "This course is complete; that action is no longer available.",
        "withdrawn": "You have been withdrawn from this course.",
        "archived": "This course has been archived.",
    }.get(current, "You do not have access to this course.")


def require_mode_enabled(course_id: str, mode: str) -> None:
    """Check 4 of the permission stack — Permission Checks §3.3.

    Only Application mode is gated. Learn and Review are always available to an
    enrolled student.

    Fails OPEN if the column is missing, so this can be deployed before
    sql/003_add_lecturer_role_and_course_ownership.sql without locking every
    student out of Application mode mid-pilot.
    """
    if (mode or "").lower().strip() != "application":
        return

    try:
        rows = (
            get_supabase()
            .table("courses")
            .select("practice_mode_enabled")
            .eq("id", course_id)
            .execute()
            .data
        )
    except Exception as exc:  # noqa: BLE001 — column may not exist yet
        logger.warning(
            "practice_mode_enabled lookup failed for %s; allowing Application "
            "mode. Apply 003_add_lecturer_role_and_course_ownership.sql: %s",
            course_id, exc,
        )
        return

    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")

    if rows[0].get("practice_mode_enabled") is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Application mode is disabled for this course.",
        )


def redeem_code(code: str, token: str) -> dict:
    """Redeem an invite code as the calling student.

    Runs through the user's own JWT so ``auth.uid()`` inside the SQL function
    resolves to the student. Calling this with the service-role client would
    make auth.uid() NULL and the function would refuse.
    """
    from app.db.supabase import get_user_client

    cleaned = (code or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Invite code is required")

    try:
        result = get_user_client(token).rpc("redeem_invite_code", {"p_code": cleaned}).execute()
    except Exception as exc:  # noqa: BLE001 — surface the SQL function's message
        raise HTTPException(status_code=400, detail=_redemption_error(exc)) from exc

    if not result.data:
        raise HTTPException(status_code=400, detail="Invalid invite code")

    return result.data if isinstance(result.data, dict) else result.data[0]


def _redemption_error(exc: Exception) -> str:
    """Pass the SQL function's own message through when it is one of ours.

    Those messages ("expired", "fully used") are written for students; a
    generic 400 would leave them with no idea which of four conditions failed.
    """
    text = str(exc)
    for known in (
        "Invalid invite code",
        "no longer active",
        "has expired",
        "fully used",
    ):
        if known in text:
            start = text.find(known)
            return text[start:].split('"')[0].split("\\n")[0].strip()
    logger.warning("Invite redemption failed: %s", text)
    return "Could not redeem that invite code."
