"""Activities the Intervention Studio assigns, and the study lock they can impose.

An assignment (sql/025) is a mini-lesson session or a practice paper given to
one student. Completion is derived, never stored, so it cannot drift from what
the student actually did:

- a lesson is done once the student has replied in its session (the lesson
  ends with a question to try);
- a paper is done once it has been submitted.

A ``required`` assignment that is still to do locks the rest of that course:
no new session, no reopening an old one, no new turn anywhere but the
assignment itself. A practice paper only locks while it is actually takeable
(published and inside its window) — otherwise a paper that closed unanswered,
or one the lecturer never published, would lock the student out for good.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

LOCK_MESSAGE = "Finish the activity your lecturer assigned before starting or reopening other sessions."


def _ts(value) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def for_student(student_id: str, course_id: str) -> list[dict]:
    """Every assignment this student can see on the course, with its status.

    status: "todo" | "done" | "closed" (a paper whose window ended unanswered).
    Papers the lecturer has not published yet are left out entirely.
    """
    sb = get_supabase()
    try:
        rows = (
            sb.table("student_assignments").select("*")
            .eq("student_id", student_id).eq("course_id", course_id)
            .order("created_at", desc=True).execute().data or []
        )
    except Exception as exc:  # noqa: BLE001 — sql/025 not applied: nothing is assigned
        logger.debug("student_assignments unavailable: %s", exc)
        return []
    if not rows:
        return []

    lesson_ids = [r["session_id"] for r in rows if r["kind"] == "lesson"]
    replied: set[str] = set()
    if lesson_ids:
        replied = {
            c["session_id"]
            for c in sb.table("conversations").select("session_id, user_input")
            .in_("session_id", lesson_ids).execute().data or []
            if not str(c.get("user_input") or "").startswith("(system)")
        }

    paper_ids = [r["assessment_id"] for r in rows if r["kind"] == "paper"]
    papers: dict[str, dict] = {}
    submitted: set[str] = set()
    if paper_ids:
        papers = {
            a["id"]: a
            for a in sb.table("assessments").select("id, is_published, opens_at, closes_at")
            .in_("id", paper_ids).execute().data or []
        }
        submitted = {
            a["assessment_id"]
            for a in sb.table("assessment_attempts").select("assessment_id")
            .eq("student_id", student_id).in_("assessment_id", paper_ids).execute().data or []
        }

    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        item = {k: r[k] for k in ("id", "kind", "title", "required", "session_id", "assessment_id", "created_at")}
        if r["kind"] == "lesson":
            item["status"] = "done" if r["session_id"] in replied else "todo"
        else:
            paper = papers.get(r["assessment_id"])
            if not paper or not paper.get("is_published"):
                continue
            opens, closes = _ts(paper.get("opens_at")), _ts(paper.get("closes_at"))
            item["opens_at"], item["closes_at"] = paper.get("opens_at"), paper.get("closes_at")
            if r["assessment_id"] in submitted:
                item["status"] = "done"
            elif closes and now > closes:
                item["status"] = "closed"
            else:
                item["status"] = "todo"
            item["available"] = not opens or now >= opens
        out.append(item)
    return out


def blocking(student_id: str, course_id: str) -> list[dict]:
    """Required, unfinished assignments that can be done right now."""
    return [
        a for a in for_student(student_id, course_id)
        if a["required"] and a["status"] == "todo" and a.get("available", True)
    ]


def require_unlocked(student_id: str, course_id: str, session_id: str | None = None) -> None:
    """423 unless the student is free to study — or is working in the very
    lesson session they were assigned."""
    blockers = blocking(student_id, course_id)
    if blockers and (session_id is None or session_id not in {b.get("session_id") for b in blockers}):
        raise HTTPException(status_code=423, detail=LOCK_MESSAGE)


def lock_message_for_session(student_id: str, session_id: str) -> str | None:
    """For the WebSocket, which has a session but no course: the lock message, or None."""
    rows = get_supabase().table("sessions").select("course_id").eq("id", session_id).execute().data
    if not rows:
        return None
    try:
        require_unlocked(student_id, rows[0]["course_id"], session_id)
    except HTTPException as exc:
        return exc.detail
    return None
