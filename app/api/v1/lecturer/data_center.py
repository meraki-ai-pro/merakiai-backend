"""Data Center: every course dataset an instructor may need, as CSV, by date range.

One endpoint, one dataset per call. Each dataset filters on the timestamp that
says when the thing happened (a session's start, an answer's submission, an
enrolment's date), so "1–30 September" means what the instructor expects.

Dates are whole days in UTC: ``start`` from 00:00, ``end`` up to 23:59:59.
Either may be omitted for an open range.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import assert_course_owner, lecturer_guard
from app.db.supabase import get_supabase

router = APIRouter(prefix="/courses/{course_id}/data", tags=["Instructor – Data Center"])

Dataset = Literal["students", "sessions", "conversations", "exam_results", "mastery", "activities", "engagement", "feedback"]

# Hard ceiling per download. A pilot course is far below it; past it the CSV
# says it was cut rather than silently being incomplete.
MAX_ROWS = 50_000
_PAGE = 1000


def day_bounds(start: date | None, end: date | None) -> tuple[str | None, str | None]:
    """ISO bounds for whole UTC days, ``end`` inclusive."""
    if start and end and end < start:
        raise HTTPException(status_code=400, detail="The end date is before the start date.")
    lo = datetime.combine(start, time.min, tzinfo=timezone.utc).isoformat() if start else None
    hi = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc).isoformat() if end else None
    return lo, hi


def _fetch(build: Callable[[], object], ts_column: str, lo: str | None, hi: str | None) -> list[dict]:
    """All rows of a query, paged, filtered to [lo, hi) on ts_column."""
    rows: list[dict] = []
    while len(rows) < MAX_ROWS:
        q = build()
        if lo:
            q = q.gte(ts_column, lo)
        if hi:
            q = q.lt(ts_column, hi)
        page = q.order(ts_column).range(len(rows), len(rows) + _PAGE - 1).execute().data or []
        rows.extend(page)
        if len(page) < _PAGE:
            break
    return rows[:MAX_ROWS]


def _in_batches(ids: list[str], fn: Callable[[list[str]], list[dict]], size: int = 150) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(ids), size):
        out.extend(fn(ids[i:i + size]))
    return out


def _people(sb, ids: set[str]) -> dict[str, dict]:
    if not ids:
        return {}
    rows = _in_batches(sorted(ids), lambda b: sb.table("users").select(
        "id, email, first_name, last_name").in_("id", b).execute().data or [])
    return {r["id"]: r for r in rows}


def _name(p: dict | None) -> str:
    p = p or {}
    return " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)


def _course_sessions(sb, course_id: str) -> dict[str, dict]:
    rows = []
    while True:
        page = sb.table("sessions").select("id, user_id, title, current_mode").eq(
            "course_id", course_id).range(len(rows), len(rows) + _PAGE - 1).execute().data or []
        rows.extend(page)
        if len(page) < _PAGE:
            return {r["id"]: r for r in rows}


# ── Datasets ────────────────────────────────────────────────────────────────
# Each returns (header, rows). Student identity is email + name, never a uuid
# alone: the file is read by a person, often in a spreadsheet.

def _students(sb, course_id, lo, hi):
    rows = _fetch(lambda: sb.table("enrolments").select("student_id, status, enrolled_at, completed_at, withdrawn_at, extra_time_minutes")
                  .eq("course_id", course_id), "enrolled_at", lo, hi)
    people = _people(sb, {r["student_id"] for r in rows})
    return (["email", "name", "status", "enrolled_at", "completed_at", "withdrawn_at", "extra_time_minutes"],
            [[people.get(r["student_id"], {}).get("email"), _name(people.get(r["student_id"])), r["status"],
              r.get("enrolled_at"), r.get("completed_at"), r.get("withdrawn_at"), r.get("extra_time_minutes")] for r in rows])


def _sessions(sb, course_id, lo, hi):
    rows = _fetch(lambda: sb.table("sessions").select("id, user_id, title, current_mode, prefers_video, started_at, archived_at")
                  .eq("course_id", course_id), "started_at", lo, hi)
    people = _people(sb, {r["user_id"] for r in rows})
    return (["session_id", "email", "name", "title", "mode", "format", "started_at", "archived"],
            [[r["id"], people.get(r["user_id"], {}).get("email"), _name(people.get(r["user_id"])), r.get("title"),
              r.get("current_mode"), "video" if r.get("prefers_video") else "text", r.get("started_at"),
              "yes" if r.get("archived_at") else "no"] for r in rows])


def _conversations(sb, course_id, lo, hi):
    sessions = _course_sessions(sb, course_id)
    rows = _in_batches(list(sessions), lambda b: _fetch(
        lambda: sb.table("conversations").select("session_id, user_id, mode, user_input, tutor_response, response_format, created_at")
        .in_("session_id", b), "created_at", lo, hi))
    rows.sort(key=lambda r: r.get("created_at") or "")
    people = _people(sb, {r["user_id"] for r in rows if r.get("user_id")})
    return (["session_id", "session_title", "email", "name", "mode", "format", "asked_at", "student_message", "tutor_answer"],
            [[r["session_id"], sessions.get(r["session_id"], {}).get("title"), people.get(r.get("user_id"), {}).get("email"),
              _name(people.get(r.get("user_id"))), r.get("mode"), r.get("response_format"), r.get("created_at"),
              "" if str(r.get("user_input") or "").startswith("(system)") else r.get("user_input"),
              r.get("tutor_response")] for r in rows])


def _exam_results(sb, course_id, lo, hi):
    papers = {p["id"]: p for p in sb.table("assessments").select("id, title, kind").eq("course_id", course_id).execute().data or []}
    rows = _in_batches(list(papers), lambda b: _fetch(
        lambda: sb.table("assessment_attempts").select("assessment_id, question_id, student_id, student_answer, score, is_correct, needs_review, time_spent_seconds, created_at")
        .in_("assessment_id", b), "created_at", lo, hi))
    questions = {q["id"]: q for q in _in_batches(list(papers), lambda b: sb.table("assessment_questions").select(
        "id, prompt, points, topic, order_index").in_("assessment_id", b).execute().data or [])}
    people = _people(sb, {r["student_id"] for r in rows})
    rows.sort(key=lambda r: (r.get("created_at") or "", questions.get(r["question_id"], {}).get("order_index") or 0))
    return (["exam", "kind", "email", "name", "question_no", "question", "topic", "answer", "score", "out_of",
             "correct", "awaiting_review", "seconds_on_question", "submitted_at"],
            [[papers[r["assessment_id"]]["title"], papers[r["assessment_id"]]["kind"],
              people.get(r["student_id"], {}).get("email"), _name(people.get(r["student_id"])),
              (questions.get(r["question_id"], {}).get("order_index") or 0) + 1,
              questions.get(r["question_id"], {}).get("prompt"), questions.get(r["question_id"], {}).get("topic"),
              r.get("student_answer"), r.get("score"), questions.get(r["question_id"], {}).get("points"),
              {True: "yes", False: "no"}.get(r.get("is_correct"), ""), "yes" if r.get("needs_review") else "no",
              r.get("time_spent_seconds"), r.get("created_at")] for r in rows])


def _mastery(sb, course_id, lo, hi):
    rows = _fetch(lambda: sb.table("mastery_states").select("student_id, topic, mastery_score, attempts_count, correct_count, last_practised_at, updated_at")
                  .eq("course_id", course_id), "updated_at", lo, hi)
    people = _people(sb, {r["student_id"] for r in rows})
    return (["email", "name", "topic", "mastery_percent", "attempts", "correct", "last_practised_at", "updated_at"],
            [[people.get(r["student_id"], {}).get("email"), _name(people.get(r["student_id"])), r.get("topic"),
              round(float(r.get("mastery_score") or 0) * 100, 1), r.get("attempts_count"), r.get("correct_count"),
              r.get("last_practised_at"), r.get("updated_at")] for r in rows])


def _activities(sb, course_id, lo, hi):
    from app.core import assignments

    rows = _fetch(lambda: sb.table("student_assignments").select("id, student_id, kind, title, required, created_at")
                  .eq("course_id", course_id), "created_at", lo, hi)
    status: dict[str, str] = {}
    for sid in {r["student_id"] for r in rows}:
        status.update({a["id"]: a["status"] for a in assignments.for_student(sid, course_id)})
    people = _people(sb, {r["student_id"] for r in rows})
    return (["email", "name", "activity", "kind", "required", "status", "assigned_at"],
            [[people.get(r["student_id"], {}).get("email"), _name(people.get(r["student_id"])), r["title"], r["kind"],
              "yes" if r["required"] else "no", status.get(r["id"], "not published"), r["created_at"]] for r in rows])


def _engagement(sb, course_id, lo, hi):
    rows = _fetch(lambda: sb.table("events").select("user_id, event_type, topic, session_id, created_at")
                  .eq("course_id", course_id), "created_at", lo, hi)
    people = _people(sb, {r["user_id"] for r in rows if r.get("user_id")})
    return (["event", "email", "name", "topic", "session_id", "at"],
            [[r["event_type"], people.get(r.get("user_id"), {}).get("email"), _name(people.get(r.get("user_id"))),
              r.get("topic"), r.get("session_id"), r["created_at"]] for r in rows])


def _feedback(sb, course_id, lo, hi):
    rows = _fetch(lambda: sb.table("feedback_responses").select("user_id, role_at_time, feedback_type, rating, nps_score, mode, free_text, created_at")
                  .eq("course_id", course_id), "created_at", lo, hi)
    people = _people(sb, {r["user_id"] for r in rows if r.get("user_id")})
    return (["email", "name", "role", "type", "rating", "nps", "mode", "comment", "at"],
            [[people.get(r.get("user_id"), {}).get("email"), _name(people.get(r.get("user_id"))), r.get("role_at_time"),
              r.get("feedback_type"), r.get("rating"), r.get("nps_score"), r.get("mode"), r.get("free_text"),
              r["created_at"]] for r in rows])


DATASETS: dict[str, Callable] = {
    "students": _students, "sessions": _sessions, "conversations": _conversations,
    "exam_results": _exam_results, "mastery": _mastery, "activities": _activities,
    "engagement": _engagement, "feedback": _feedback,
}


def to_csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(["" if v is None else v for v in row] for row in rows)
    if len(rows) >= MAX_ROWS:
        writer.writerow([f"(cut at {MAX_ROWS} rows — narrow the date range for the rest)"])
    return buf.getvalue()


@router.get("/{dataset}")
def download(
    course_id: str, dataset: Dataset, start: date | None = None, end: date | None = None,
    user=Depends(lecturer_guard),
):
    """One dataset as CSV text: ``{filename, rows, csv}``."""
    assert_course_owner(user, course_id)
    lo, hi = day_bounds(start, end)
    header, rows = DATASETS[dataset](get_supabase(), course_id, lo, hi)
    span = f"_{start or 'start'}_to_{end or 'now'}" if (start or end) else ""
    return {"filename": f"{course_id}_{dataset}{span}.csv", "rows": len(rows), "csv": to_csv(header, rows)}
