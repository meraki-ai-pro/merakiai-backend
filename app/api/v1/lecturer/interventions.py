"""Intervention Studio: from "eleven students misread this" to doing something.

The analytics panels say who needs help and with what. This turns a focus — a
topic, optionally a misconception, and the students who share it — into an
action the lecturer reviews and approves. Nothing here reaches a student
without that approval:

- options      — Claude proposes interventions for the situation (advice only)
- practice     — a DRAFT paper for just those students: targeted practice, a
                 10-minute diagnostic, or a retrieval check a week out. It
                 lands in the Exams tab, where the lecturer edits and
                 publishes it like any other paper.
- mini-lesson  — Claude drafts a short lesson from the course material; the
                 lecturer edits it, then sends it. Each student gets it as a
                 new Learn session, so the tutor can carry on from it.

Practice papers are ordinary assessments (sql/017 adds only who they are for),
so their marking, release, and mastery updates are the ones exams already use —
which is what closes the loop from intervention back to measurement.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from app.core import audit
from app.core.auth import assert_course_owner, lecturer_guard
from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/courses/{course_id}/interventions", tags=["Lecturer – Interventions"])

Format = Literal["practice", "diagnostic", "retrieval"]

# What each practice format means. Diagnostic: short and timed, to find out
# where the class is. Retrieval: opens a week later, because recalling after a
# gap is what shows whether the fix held.
FORMATS: dict[str, dict] = {
    "practice": {"title": "Practice", "count": 6, "minutes": None, "opens_in_days": None},
    "diagnostic": {"title": "10-minute check", "count": 5, "minutes": 10, "opens_in_days": None},
    "retrieval": {"title": "Retrieval check", "count": 5, "minutes": 15, "opens_in_days": 7},
}


class Focus(BaseModel):
    topic: str = Field(..., min_length=1, max_length=200)
    misconception: str | None = Field(None, max_length=300)
    student_ids: list[str] = Field(default_factory=list, max_length=500)


class PracticeRequest(Focus):
    format: Format = "practice"
    question_count: int | None = Field(None, ge=2, le=15)


class LessonSend(BaseModel):
    topic: str = Field(..., min_length=1, max_length=200)
    lesson: str = Field(..., min_length=20, max_length=20_000)
    # Empty = everyone actively enrolled (a whole-class lesson).
    student_ids: list[str] = Field(default_factory=list, max_length=500)
    sources: list[dict] | None = None


# ── Helpers ─────────────────────────────────────────────────────────────────

def _active_students(course_id: str, student_ids: list[str]) -> list[str]:
    """The requested students who are actively enrolled; the rest are dropped.

    A lecturer can only target their own active students — an id from another
    course, or a withdrawn student, is silently not sent anything.
    """
    if not student_ids:
        return []
    rows = (
        get_supabase().table("enrolments").select("student_id")
        .eq("course_id", course_id).eq("status", "active")
        .in_("student_id", list(dict.fromkeys(student_ids))).execute().data or []
    )
    found = {r["student_id"] for r in rows}
    return [s for s in dict.fromkeys(student_ids) if s in found]


def _focus_text(focus: Focus, n_students: int) -> str:
    lines = [f"TOPIC: {focus.topic}"]
    if focus.misconception:
        lines.append(f"MISCONCEPTION shared by these students: {focus.misconception}")
    lines.append(f"STUDENTS: {n_students}" if n_students else "STUDENTS: the whole class")
    return "\n".join(lines)


_OPTIONS_SYSTEM = """You advise a university lecturer on how to respond to a
learning difficulty in their class. Their platform can do these things for
them, each reviewed by the lecturer before students see it:

- "practice": a short auto-marked practice paper for the chosen students
- "diagnostic": a timed 10-minute check to find out how widespread the problem is
- "retrieval": a short check that opens in a week, to see whether the fix held
- "mini_lesson": a short lesson sent to the chosen students as a tutor session

Everything else is done by the lecturer in person, e.g. a worked example in the
next lecture, a group activity, or revisiting a prerequisite.

Suggest the 3 most useful responses, best first, for the situation given.
Return JSON only:
{"options": [{"action": "practice"|"diagnostic"|"retrieval"|"mini_lesson"|"in_person",
  "title": "<at most 8 words>", "why": "<one or two sentences, specific to this
  topic and misconception>"}]}"""

_PRACTICE_SYSTEM = """You write short practice questions for university students
from their lecturer's course material.

Return JSON only: {"questions": [{"question_type": "mcq" | "fill_blank",
"prompt": "...", "options": ["...", ...], "correct_answer": "..."}]}

Rules:
- Only "mcq" and "fill_blank": both are marked automatically.
- Every question tests the TOPIC and rests on the REFERENCE MATERIAL. Write
  mathematics in LaTeX inside $...$.
- mcq: exactly 4 options, WITHOUT letters; correct_answer is the exact text of
  the one correct option.
- fill_blank: write the blank as "____"; correct_answer is short and exact,
  with acceptable alternatives separated by "|". Prefer a number or an
  algebraic expression — those are marked by value, so any equivalent form
  counts.
- If a MISCONCEPTION is given: at least half the questions must be ones a
  student holding it gets wrong, and in each such mcq one distractor is exactly
  the answer that misconception produces. Never name the misconception in a
  question.
- Vary the questions; no more than two may be the same exercise with different
  numbers."""


async def _ask_json(system: str, prompt: str, label: str) -> dict:
    from app.ai.rag.claude import generate_response
    from app.ai.rag.modes_sessions.service import safe_parse_json_with_retry

    async def ask() -> str:
        return await generate_response(
            prompt=prompt, mode="intervention",
            system_parts=[{"type": "text", "text": system}],
        )

    return await safe_parse_json_with_retry(await ask(), ask, label=label)


def valid_questions(raw: list, topic: str) -> list[dict]:
    """Keep only questions the exam editor would accept, tagged with the topic.

    Tagging is what makes each answer update the student's mastery on the
    topic the intervention was about.
    """
    from app.api.v1.assessments import QuestionCreate

    out = []
    for item in raw or []:
        if not isinstance(item, dict) or item.get("question_type") not in ("mcq", "fill_blank"):
            continue
        try:
            q = QuestionCreate(
                question_type=item["question_type"],
                prompt=str(item.get("prompt") or ""),
                options=[str(o) for o in item.get("options") or []],
                correct_answer=str(item.get("correct_answer") or ""),
                topic=topic, points=1,
            )
        except (ValidationError, KeyError):
            continue
        out.append(q.model_dump())
    return out


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/options")
async def intervention_options(course_id: str, focus: Focus, user=Depends(lecturer_guard)):
    """Claude's suggested responses to this situation. Advice only."""
    assert_course_owner(user, course_id)
    students = _active_students(course_id, focus.student_ids)
    try:
        parsed = await _ask_json(_OPTIONS_SYSTEM, _focus_text(focus, len(students)), "intervention options")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Intervention options failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not get suggestions right now.")
    options = [
        {
            "action": o.get("action") if o.get("action") in ("practice", "diagnostic", "retrieval", "mini_lesson", "in_person") else "in_person",
            "title": str(o.get("title") or "")[:120],
            "why": str(o.get("why") or "")[:600],
        }
        for o in (parsed.get("options") or [])[:3]
        if isinstance(o, dict) and o.get("title")
    ]
    return {"options": options}


@router.post("/practice")
async def create_practice(
    course_id: str, payload: PracticeRequest, request: Request, user=Depends(lecturer_guard)
):
    """Draft a practice paper for the chosen students. Not published."""
    assert_course_owner(user, course_id)
    from app.ai.rag.retriever import retrieve_context

    students = _active_students(course_id, payload.student_ids)
    if payload.student_ids and not students:
        raise HTTPException(status_code=400, detail="None of those students are actively enrolled.")

    fmt = FORMATS[payload.format]
    count = payload.question_count or fmt["count"]
    context = await retrieve_context(
        query=f"{payload.topic} {payload.misconception or ''}".strip(),
        mode="review", course_id=course_id, top_k=8,
    )
    prompt = (
        f"{_focus_text(payload, len(students))}\n"
        f"Write {count} questions.\n\nREFERENCE MATERIAL:\n" + "\n\n".join(context)
    )
    try:
        parsed = await _ask_json(_PRACTICE_SYSTEM, prompt, "practice questions")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Practice generation failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not write the questions right now.")
    questions = valid_questions(parsed.get("questions") or [], payload.topic)[:count]
    if not questions:
        raise HTTPException(status_code=502, detail="No usable questions came back. Try again.")

    now = datetime.now(timezone.utc)
    row = {
        "course_id": course_id, "kind": "quiz", "created_by": user["id"],
        "title": f"{fmt['title']}: {payload.topic}"[:200],
        # Neutral on purpose: students are not told which misconception
        # they were grouped by.
        "instructions": f"A short {fmt['title'].lower()} on {payload.topic}, set by your lecturer.",
        "time_limit_minutes": fmt["minutes"],
        "target_student_ids": students or None,
    }
    if fmt["opens_in_days"]:
        opens = now + timedelta(days=fmt["opens_in_days"])
        row["opens_at"] = opens.isoformat()
        row["closes_at"] = (opens + timedelta(days=7)).isoformat()

    sb = get_supabase()
    try:
        created = sb.table("assessments").insert(row).execute().data
    except Exception as exc:  # noqa: BLE001
        # Without sql/017 there is no target column. Creating the paper
        # anyway would publish a "targeted" paper to the whole class.
        logger.warning("Targeted paper insert failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Targeted papers need database migration 017 (sql/017_targeted_assessments.sql).",
        )
    assessment = created[0]
    sb.table("assessment_questions").insert([
        {**q, "assessment_id": assessment["id"], "order_index": i} for i, q in enumerate(questions)
    ]).execute()

    audit.record(
        actor=user, action="intervention.practice", resource_type="assessment",
        resource_id=assessment["id"], course_id=course_id,
        new_values={"format": payload.format, "topic": payload.topic,
                    "students": len(students), "questions": len(questions)},
        request=request,
    )
    return {"status": "draft", "assessment_id": assessment["id"], "title": row["title"],
            "questions": len(questions), "students": len(students)}


@router.post("/mini-lesson/draft")
async def draft_mini_lesson(course_id: str, focus: Focus, user=Depends(lecturer_guard)):
    """A short lesson from the course material, for the lecturer to edit."""
    assert_course_owner(user, course_id)
    from app.ai.rag.service import query_rag

    course = (
        get_supabase().table("courses").select("persona, domain_topics, academic_level")
        .eq("id", course_id).execute().data or [{}]
    )[0]
    ask = (
        f"Teach a short, self-contained mini-lesson on {focus.topic}: the key idea, "
        "one fully worked example, and the one thing to remember."
    )
    if focus.misconception:
        ask += (
            f" Several students in this class believe that: {focus.misconception}. "
            "Address it head-on without singling anyone out: show why it is "
            "tempting, then why it is wrong, with a worked example that exposes it."
        )
    ask += " End with one short question for the student to try."
    try:
        result = await query_rag(
            user_message=ask, mode="learn", course_id=course_id,
            course_persona=course.get("persona"),
            course_domain_topics=course.get("domain_topics"),
            board=True, academic_level=course.get("academic_level"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Mini-lesson draft failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not draft the lesson right now.")
    return {"lesson": result["response"], "sources": result.get("sources") or []}


@router.post("/mini-lesson/send")
def send_mini_lesson(
    course_id: str, payload: LessonSend, request: Request, user=Depends(lecturer_guard)
):
    """Deliver the approved lesson to each student as a new Learn session.

    The lesson is the session's first tutor message, so it appears in the
    student's sidebar and the tutor has it in context when they reply.
    """
    assert_course_owner(user, course_id)
    if payload.student_ids:
        students = _active_students(course_id, payload.student_ids)
    else:
        students = [
            r["student_id"] for r in get_supabase().table("enrolments").select("student_id")
            .eq("course_id", course_id).eq("status", "active").execute().data or []
        ]
    if not students:
        raise HTTPException(status_code=400, detail="None of those students are actively enrolled.")

    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    title = f"From your lecturer: {payload.topic}"[:120]
    sent = 0
    for sid in students:
        try:
            session = sb.table("sessions").insert({
                "user_id": sid, "course_id": course_id, "current_mode": "learn",
                "prefers_video": False, "started_at": now, "title": title,
            }).execute().data[0]
            row = {
                "session_id": session["id"], "user_id": sid, "mode": "learn",
                # "(system)" inputs are never shown as a student message.
                "user_input": "(system) lecturer mini-lesson",
                "tutor_response": payload.lesson, "response_format": "text",
            }
            if payload.sources:
                row["sources"] = payload.sources
            sb.table("conversations").insert(row).execute()
            sent += 1
        except Exception as exc:  # noqa: BLE001 — one student's failure must not stop the rest
            logger.warning("Mini-lesson to %s failed: %s", sid, exc)

    audit.record(
        actor=user, action="intervention.mini_lesson", resource_type="course",
        resource_id=course_id, course_id=course_id,
        new_values={"topic": payload.topic, "students": len(students), "sent": sent},
        request=request,
    )
    return {"status": "sent", "sent": sent, "skipped": (len(set(payload.student_ids)) - len(students)) if payload.student_ids else 0}
