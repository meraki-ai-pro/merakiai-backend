"""Pre/post assessments — the pilot's primary outcome measure.

Without a labelled pre/post pair the study can report engagement and nothing
else. This is the instrument that turns "students used it a lot" into "students
scored N points higher after using it".

The answer key never leaves the server for a student. `assessment_questions`
has no student RLS policy at all, and the student-facing endpoint strips
``correct_answer`` explicitly — belt and braces, because a leaked key
invalidates every score collected with it.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core import audit, events, mastery
from app.core.auth import assert_course_owner, auth_guard, lecturer_guard
from app.core.enrolment import require_enrolment
from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assessments", tags=["Assessments"])

AssessmentKind = Literal["pre", "post", "retention"]


class AssessmentCreate(BaseModel):
    course_id: str = Field(..., max_length=100)
    kind: AssessmentKind
    title: str = Field(..., min_length=1, max_length=200)
    instructions: str | None = None


class QuestionCreate(BaseModel):
    prompt: str = Field(..., min_length=1)
    options: list[str] = Field(default_factory=list)
    correct_answer: str = Field(..., min_length=1)
    topic: str | None = Field(None, max_length=200)
    points: float = Field(1, gt=0)
    order_index: int = 0


class SubmissionItem(BaseModel):
    question_id: str
    answer: str
    time_spent_seconds: int | None = None


class Submission(BaseModel):
    answers: list[SubmissionItem] = Field(..., min_length=1)


# ── Lecturer ────────────────────────────────────────────────────────────────

@router.post("")
def create_assessment(payload: AssessmentCreate, request: Request, user=Depends(lecturer_guard)):
    assert_course_owner(user, payload.course_id)
    sb = get_supabase()
    created = sb.table("assessments").insert({
        **payload.model_dump(), "created_by": user["id"],
    }).execute().data
    if not created:
        raise HTTPException(status_code=500, detail="Failed to create assessment")

    audit.record(
        actor=user, action="assessment.create", resource_type="assessment",
        resource_id=created[0]["id"], course_id=payload.course_id,
        new_values={"kind": payload.kind, "title": payload.title}, request=request,
    )
    return {"status": "ok", "assessment": created[0]}


@router.get("/course/{course_id}")
def list_assessments(course_id: str, user=Depends(lecturer_guard)):
    assert_course_owner(user, course_id)
    rows = (
        get_supabase().table("assessments")
        .select("id, kind, title, instructions, is_published, created_at")
        .eq("course_id", course_id).order("created_at").execute().data or []
    )
    return {"assessments": rows}


def _load_owned(assessment_id: str, user: dict) -> dict:
    rows = get_supabase().table("assessments").select("*").eq("id", assessment_id).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail="Assessment not found")
    assert_course_owner(user, rows[0]["course_id"])
    return rows[0]


@router.post("/{assessment_id}/questions")
def add_question(
    assessment_id: str, payload: QuestionCreate, request: Request, user=Depends(lecturer_guard)
):
    assessment = _load_owned(assessment_id, user)

    if payload.options and payload.correct_answer not in payload.options:
        # Caught here rather than at scoring time, where it would silently mark
        # every student wrong and look like a catastrophic cohort failure.
        raise HTTPException(
            status_code=400,
            detail="correct_answer must be one of the options.",
        )

    created = get_supabase().table("assessment_questions").insert({
        **payload.model_dump(), "assessment_id": assessment_id,
    }).execute().data

    audit.record(
        actor=user, action="assessment.question_add", resource_type="assessment",
        resource_id=assessment_id, course_id=assessment["course_id"], request=request,
    )
    return {"status": "ok", "question": created[0] if created else None}


@router.patch("/{assessment_id}/publish")
def publish(assessment_id: str, request: Request, user=Depends(lecturer_guard)):
    assessment = _load_owned(assessment_id, user)
    sb = get_supabase()

    count = sb.table("assessment_questions").select("id").eq(
        "assessment_id", assessment_id
    ).execute().data or []
    if not count:
        raise HTTPException(
            status_code=400, detail="Add at least one question before publishing."
        )

    sb.table("assessments").update({"is_published": True}).eq("id", assessment_id).execute()
    audit.record(
        actor=user, action="assessment.publish", resource_type="assessment",
        resource_id=assessment_id, course_id=assessment["course_id"],
        new_values={"questions": len(count)}, request=request,
    )
    return {"status": "ok", "assessment_id": assessment_id, "questions": len(count)}


@router.get("/{assessment_id}/results")
def results(assessment_id: str, user=Depends(lecturer_guard)):
    """Per-student totals for one assessment."""
    assessment = _load_owned(assessment_id, user)
    sb = get_supabase()

    questions = sb.table("assessment_questions").select("id, points, topic").eq(
        "assessment_id", assessment_id
    ).execute().data or []
    total_points = sum(float(q["points"]) for q in questions) or 1.0

    attempts = sb.table("assessment_attempts").select(
        "student_id, question_id, is_correct, score"
    ).eq("assessment_id", assessment_id).execute().data or []

    by_student: dict[str, float] = {}
    for a in attempts:
        by_student[a["student_id"]] = by_student.get(a["student_id"], 0) + float(a["score"] or 0)

    return {
        "assessment_id": assessment_id,
        "kind": assessment["kind"],
        "total_points": total_points,
        "responses": len(by_student),
        "students": [
            {"student_id": sid, "score": score, "percent": round(100 * score / total_points, 1)}
            for sid, score in sorted(by_student.items(), key=lambda x: -x[1])
        ],
    }


@router.get("/course/{course_id}/learning-gain")
def learning_gain(course_id: str, user=Depends(lecturer_guard)):
    """Pre vs post, per student and overall — the headline pilot number.

    Only students who sat BOTH are included. A cohort mean over different
    populations at the two time points is the classic way to manufacture a
    gain that is really just attrition.
    """
    assert_course_owner(user, course_id)
    sb = get_supabase()

    assessments = sb.table("assessments").select("id, kind").eq(
        "course_id", course_id
    ).execute().data or []
    pre_ids = [a["id"] for a in assessments if a["kind"] == "pre"]
    post_ids = [a["id"] for a in assessments if a["kind"] == "post"]

    if not pre_ids or not post_ids:
        return {
            "available": False,
            "reason": "Needs at least one published pre-test and one post-test.",
        }

    def _percent_by_student(ids: list[str]) -> dict[str, float]:
        questions = sb.table("assessment_questions").select("id, points, assessment_id").in_(
            "assessment_id", ids
        ).execute().data or []
        total = sum(float(q["points"]) for q in questions) or 1.0
        attempts = sb.table("assessment_attempts").select("student_id, score").in_(
            "assessment_id", ids
        ).execute().data or []
        totals: dict[str, float] = {}
        for a in attempts:
            totals[a["student_id"]] = totals.get(a["student_id"], 0) + float(a["score"] or 0)
        return {sid: round(100 * v / total, 1) for sid, v in totals.items()}

    pre = _percent_by_student(pre_ids)
    post = _percent_by_student(post_ids)
    paired = sorted(set(pre) & set(post))

    if not paired:
        return {
            "available": False,
            "reason": "No student has completed both the pre-test and the post-test yet.",
            "sat_pre": len(pre),
            "sat_post": len(post),
        }

    gains = [round(post[s] - pre[s], 1) for s in paired]
    mean_gain = round(sum(gains) / len(gains), 2)

    return {
        "available": True,
        "n": len(paired),
        "mean_pre": round(sum(pre[s] for s in paired) / len(paired), 2),
        "mean_post": round(sum(post[s] for s in paired) / len(paired), 2),
        "mean_gain": mean_gain,
        "improved": sum(1 for g in gains if g > 0),
        "unchanged": sum(1 for g in gains if g == 0),
        "declined": sum(1 for g in gains if g < 0),
        # Reported so the lecturer can see who is missing rather than assuming
        # the paired n is the whole cohort.
        "sat_pre_only": len(set(pre) - set(post)),
        "sat_post_only": len(set(post) - set(pre)),
        "students": [
            {"student_id": s, "pre": pre[s], "post": post[s], "gain": round(post[s] - pre[s], 1)}
            for s in paired
        ],
    }


# ── Student ─────────────────────────────────────────────────────────────────

@router.get("/available/{course_id}")
def available(course_id: str, user=Depends(auth_guard)):
    """Published assessments on this course, with whether it is already done."""
    require_enrolment(user, course_id)
    sb = get_supabase()

    rows = sb.table("assessments").select("id, kind, title, instructions").eq(
        "course_id", course_id
    ).eq("is_published", True).execute().data or []
    if not rows:
        return {"assessments": []}

    done = sb.table("assessment_attempts").select("assessment_id").eq(
        "student_id", user["id"]
    ).in_("assessment_id", [r["id"] for r in rows]).execute().data or []
    completed = {d["assessment_id"] for d in done}

    return {
        "assessments": [{**r, "completed": r["id"] in completed} for r in rows]
    }


@router.get("/{assessment_id}/take")
def take(assessment_id: str, user=Depends(auth_guard)):
    """Questions WITHOUT the answer key."""
    sb = get_supabase()
    rows = sb.table("assessments").select("*").eq("id", assessment_id).execute().data
    if not rows or not rows[0]["is_published"]:
        raise HTTPException(status_code=404, detail="Assessment not found")
    assessment = rows[0]

    require_enrolment(user, assessment["course_id"])

    questions = sb.table("assessment_questions").select(
        # correct_answer deliberately not selected. Not filtered afterwards —
        # never fetched, so it cannot leak through a logging or error path.
        "id, order_index, prompt, options, topic, points"
    ).eq("assessment_id", assessment_id).order("order_index").execute().data or []

    return {
        "assessment": {
            "id": assessment["id"],
            "kind": assessment["kind"],
            "title": assessment["title"],
            "instructions": assessment["instructions"],
        },
        "questions": questions,
    }


@router.post("/{assessment_id}/submit")
def submit(
    assessment_id: str, payload: Submission, request: Request, user=Depends(auth_guard)
):
    """Score a submission server-side and fold it into mastery."""
    sb = get_supabase()
    rows = sb.table("assessments").select("*").eq("id", assessment_id).execute().data
    if not rows or not rows[0]["is_published"]:
        raise HTTPException(status_code=404, detail="Assessment not found")
    assessment = rows[0]
    course_id = assessment["course_id"]

    require_enrolment(user, course_id)

    already = sb.table("assessment_attempts").select("id").eq(
        "assessment_id", assessment_id
    ).eq("student_id", user["id"]).limit(1).execute().data
    if already:
        # A retake would break the pre/post pairing the study depends on.
        raise HTTPException(status_code=409, detail="You have already completed this assessment.")

    questions = sb.table("assessment_questions").select(
        "id, correct_answer, points, topic"
    ).eq("assessment_id", assessment_id).execute().data or []
    key = {q["id"]: q for q in questions}

    graded: list[dict[str, Any]] = []
    earned = 0.0
    for item in payload.answers:
        question = key.get(item.question_id)
        if not question:
            continue  # a question id not on this assessment is simply ignored
        correct = item.answer.strip().lower() == str(question["correct_answer"]).strip().lower()
        score = float(question["points"]) if correct else 0.0
        earned += score
        graded.append({
            "assessment_id": assessment_id,
            "question_id": item.question_id,
            "student_id": user["id"],
            "course_id": course_id,
            "topic": question.get("topic"),
            "student_answer": item.answer[:2000],
            "is_correct": correct,
            "score": score,
            "time_spent_seconds": item.time_spent_seconds,
        })

    if not graded:
        raise HTTPException(status_code=400, detail="No valid answers submitted.")

    sb.table("assessment_attempts").insert(graded).execute()

    total = sum(float(q["points"]) for q in questions) or 1.0

    for row in graded:
        mastery.record_attempt(
            student_id=user["id"], course_id=course_id,
            topic=row.get("topic") or "", correct=bool(row["is_correct"]),
        )

    events.emit(
        events.ASSESSMENT_SUBMITTED,
        user_id=user["id"], course_id=course_id,
        payload={
            "assessment_id": assessment_id, "kind": assessment["kind"],
            "score": earned, "total": total, "answered": len(graded),
        },
    )

    return {
        "status": "ok",
        "score": earned,
        "total": total,
        "percent": round(100 * earned / total, 1),
        "answered": len(graded),
        # Deliberately no per-question breakdown: returning which items were
        # wrong on a pre-test hands the answer key back before the post-test.
    }


@router.get("/mastery/{course_id}")
def my_mastery(course_id: str, user=Depends(auth_guard)):
    require_enrolment(user, course_id)
    return {"topics": mastery.for_student(user["id"], course_id)}
