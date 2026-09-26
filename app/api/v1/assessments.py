"""Exams, and the pre/post instrument they grew out of.

Lecturers set tests, mid-sems, finals and quizzes here: typed or imported from
a PDF/Word paper, timed, opened for a window, marked, and released. The pre/post
research instrument shares the tables (a paper, its questions, one attempt per
student) but is retired from the student view at the client's request — its
existing results and the learning-gain report stay readable, and no new pre/post
papers can be created.

The answer key never leaves the server for a student before results are
released. `assessment_questions` has no student RLS policy at all, and the
student-facing endpoints never SELECT ``correct_answer`` — belt and braces,
because a leaked key invalidates every score collected with it.

Marking: multiple choice and fill-in-the-blank are marked here, exactly.
Short answers get an AI-SUGGESTED mark and are flagged for review; the lecturer
confirms or overrides each one before releasing results. A student never sees a
mark a person has not had the chance to check.

UDL: extended time is a per-student accommodation on the course, applied to
every timed paper; the deadline is enforced from a server-side start time, never
a clock the browser reports.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, model_validator

from app.core import audit, events, mastery, math_answers
from app.core.auth import assert_course_owner, auth_guard, lecturer_guard
from app.core.enrolment import require_enrolment
from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assessments", tags=["Assessments"])

ExamKind = Literal["quiz", "test", "midsem", "final"]
QuestionType = Literal["mcq", "fill_blank", "short_answer"]

EXAM_KINDS = frozenset({"quiz", "test", "midsem", "final"})

# A dropped connection at the buzzer must not lose a paper. Past this, it has.
SUBMIT_GRACE = timedelta(minutes=2)

MAX_IMPORT_BYTES = 15 * 1024 * 1024
_IMPORT_EXTENSIONS = frozenset({"pdf", "docx"})
# Long papers are extracted in slices so the JSON for 60 questions is never one
# response long enough to be cut off mid-array.
_IMPORT_SLICE_CHARS = 18_000

_MIGRATION_HINT = "Exams need database migration 016 (sql/016_udl_round_exams_soft_delete_titles.sql)."


# ── Schemas ─────────────────────────────────────────────────────────────────

class AssessmentCreate(BaseModel):
    course_id: str = Field(..., max_length=100)
    # Pre/post are retired: only exam kinds can be created.
    kind: ExamKind
    title: str = Field(..., min_length=1, max_length=200)
    instructions: str | None = None
    time_limit_minutes: int | None = Field(None, ge=1, le=600)
    opens_at: datetime | None = None
    closes_at: datetime | None = None

    @model_validator(mode="after")
    def _window_is_ordered(self):
        if self.opens_at and self.closes_at and self.closes_at <= self.opens_at:
            raise ValueError("closes_at must be after opens_at")
        return self


class AssessmentUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    instructions: str | None = None
    time_limit_minutes: int | None = Field(None, ge=1, le=600)
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    # Explicit clears, because None already means "leave it alone".
    untimed: bool = False
    clear_window: bool = False


class QuestionCreate(BaseModel):
    question_type: QuestionType = "mcq"
    prompt: str = Field(..., min_length=1)
    options: list[str] = Field(default_factory=list)
    # mcq: the correct option. fill_blank: accepted answers separated by "|".
    # short_answer: the model answer / marking guide the marker compares with.
    correct_answer: str = Field(..., min_length=1)
    topic: str | None = Field(None, max_length=200)
    points: float = Field(1, gt=0, le=100)
    order_index: int = 0

    @model_validator(mode="after")
    def _shape_matches_type(self):
        self.options = [o.strip() for o in self.options if o and o.strip()]
        if self.question_type == "mcq":
            if len(self.options) < 2:
                raise ValueError("A multiple-choice question needs at least two options.")
            # Caught here rather than at marking time, where it would silently
            # mark every student wrong and look like a cohort-wide failure.
            if self.correct_answer.strip() not in self.options:
                raise ValueError("correct_answer must be one of the options.")
        else:
            self.options = []
        return self


class BulkQuestions(BaseModel):
    questions: list[QuestionCreate] = Field(..., min_length=1, max_length=300)


class SubmissionItem(BaseModel):
    question_id: str
    answer: str = Field("", max_length=10_000)
    time_spent_seconds: int | None = None


class Submission(BaseModel):
    answers: list[SubmissionItem] = Field(..., min_length=1)


class MarkUpdate(BaseModel):
    score: float = Field(..., ge=0)
    feedback: str | None = Field(None, max_length=2000)


class ExtraTimeUpdate(BaseModel):
    extra_time_percent: int = Field(..., ge=0, le=200)


# ── Pure helpers (unit-tested) ──────────────────────────────────────────────

_SPACE_RE = re.compile(r"\s+")
_EDGE_PUNCT = " \t\n.,;:!?\"'`()[]{}"


def normalise_answer(text: str) -> str:
    """Case, spacing, surrounding punctuation and maths delimiters never decide
    a mark. Imported keys arrive as LaTeX ("$2x$", found live) while students
    type "2x"; without dropping the "$" every such blank marks them wrong."""
    return _SPACE_RE.sub(" ", (text or "").replace("$", "").strip().casefold()).strip(_EDGE_PUNCT)


def _as_number(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def grade_objective(question_type: str, correct_answer: str, answer: str) -> bool:
    """Exact marking for multiple choice and fill-in-the-blank.

    Fill-in-the-blank accepts any of the lecturer's "|"-separated answers, and
    compares numbers as numbers — "0.5" and ".5" are the same answer — and
    algebra as algebra (app/core/math_answers.py).
    """
    given = normalise_answer(answer)
    if not given:
        return False
    accepted = [correct_answer] if question_type == "mcq" else correct_answer.split("|")
    for option in accepted:
        expected = normalise_answer(option)
        if given == expected:
            return True
        # From the raw text: edge-punctuation stripping would turn ".5" into "5".
        a = _as_number(answer.replace("$", "").strip().rstrip("."))
        b = _as_number(option.replace("$", "").strip().rstrip("."))
        if a is not None and b is not None and abs(a - b) <= 1e-9 * max(1.0, abs(b)):
            return True
        # "1 + 2x" for a key of "2x+1". Blanks only: an MCQ answer is the
        # option text itself, so text equality is already exact there.
        if question_type != "mcq" and math_answers.equivalent(option, answer):
            return True
    return False


def effective_minutes(time_limit_minutes: int | None, extra_time_percent: int) -> float | None:
    if not time_limit_minutes:
        return None
    return time_limit_minutes * (1 + max(0, extra_time_percent) / 100)


def deadline_for(started_at: datetime, time_limit_minutes: int | None, extra_time_percent: int) -> datetime | None:
    minutes = effective_minutes(time_limit_minutes, extra_time_percent)
    return started_at + timedelta(minutes=minutes) if minutes else None


def coerce_imported_question(raw: Any) -> dict | None:
    """One extracted question, made safe to show as an editable draft.

    Drafts are allowed to be incomplete (a paper without an answer key yields
    questions with no answer) — the lecturer fills the gap before saving, and
    QuestionCreate refuses anything still incomplete. What is dropped outright
    is what no editing could rescue: no question text.
    """
    if not isinstance(raw, dict):
        return None
    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        return None
    qtype = raw.get("question_type")
    options = [str(o).strip() for o in (raw.get("options") or []) if str(o).strip()]
    if qtype not in ("mcq", "fill_blank", "short_answer"):
        qtype = "mcq" if len(options) >= 2 else "short_answer"
    if qtype != "mcq":
        options = []
    answer = str(raw.get("correct_answer") or "").strip()
    if qtype == "mcq" and answer and answer not in options:
        # Papers often key by letter ("B") rather than by text.
        letter = answer.strip("() .").upper()
        if len(letter) == 1 and "A" <= letter <= "Z" and ord(letter) - 65 < len(options):
            answer = options[ord(letter) - 65]
        else:
            answer = ""
    try:
        points = float(raw.get("points") or 1)
    except (TypeError, ValueError):
        points = 1.0
    return {
        "question_type": qtype,
        "prompt": prompt,
        "options": options,
        "correct_answer": answer,
        "topic": (str(raw.get("topic")).strip()[:200] or None) if raw.get("topic") else None,
        "points": min(max(points, 0.5), 100.0),
        "answer_source": raw.get("answer_source") if raw.get("answer_source") in ("paper", "inferred") else ("paper" if answer else "missing"),
    }


def slice_for_import(text: str, limit: int = _IMPORT_SLICE_CHARS) -> list[str]:
    """Split at blank lines so no question is cut in half between slices."""
    slices, current = [], ""
    for block in re.split(r"\n\s*\n", text):
        if current and len(current) + len(block) + 2 > limit:
            slices.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current.strip():
        slices.append(current)
    return slices


# ── Data helpers ────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _extra_time(course_id: str, student_id: str) -> int:
    try:
        rows = (
            get_supabase().table("enrolments").select("extra_time_percent")
            .eq("course_id", course_id).eq("student_id", student_id).limit(1).execute().data
        )
    except Exception:  # noqa: BLE001 — sql/016 not applied: nobody has an accommodation yet
        return 0
    return int((rows[0].get("extra_time_percent") if rows else 0) or 0)


def _load(assessment_id: str) -> dict:
    rows = get_supabase().table("assessments").select("*").eq("id", assessment_id).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return rows[0]


def _load_owned(assessment_id: str, user: dict) -> dict:
    assessment = _load(assessment_id)
    assert_course_owner(user, assessment["course_id"])
    return assessment


def visible_to(assessment: dict, student_id: str) -> bool:
    """A targeted paper (sql/017, Intervention Studio) is for its students only.

    NULL or empty targets = the whole course, which is every paper made
    outside the studio.
    """
    targets = assessment.get("target_student_ids")
    return not targets or student_id in targets


def _load_published_for_student(assessment_id: str, user: dict) -> dict:
    rows = get_supabase().table("assessments").select("*").eq("id", assessment_id).execute().data
    # 404 for someone else's targeted paper, as for one that does not exist:
    # it is not theirs to know about.
    if not rows or not rows[0]["is_published"] or not visible_to(rows[0], user["id"]):
        raise HTTPException(status_code=404, detail="Assessment not found")
    require_enrolment(user, rows[0]["course_id"])
    return rows[0]


def _check_window(assessment: dict, *, grace: timedelta = timedelta(0)) -> None:
    now = _now()
    opens, closes = _ts(assessment.get("opens_at")), _ts(assessment.get("closes_at"))
    if opens and now < opens:
        raise HTTPException(status_code=403, detail=f"This paper opens at {opens.isoformat()}.")
    if closes and now > closes + grace:
        raise HTTPException(status_code=403, detail="This paper has closed.")


def _names(user_ids: list[str]) -> dict[str, dict]:
    if not user_ids:
        return {}
    rows = get_supabase().table("users").select("id, first_name, last_name, email").in_(
        "id", user_ids
    ).execute().data or []
    return {
        r["id"]: {
            "name": " ".join(p for p in (r.get("first_name"), r.get("last_name")) if p) or r.get("email"),
            "email": r.get("email"),
        }
        for r in rows
    }


def _insert(table: str, rows: Any) -> list[dict]:
    try:
        return get_supabase().table(table).insert(rows).execute().data or []
    except Exception as exc:  # noqa: BLE001
        if "question_type" in str(exc) or "column" in str(exc):
            raise HTTPException(status_code=503, detail=_MIGRATION_HINT) from exc
        raise


# ── AI: import and marking ──────────────────────────────────────────────────

_IMPORT_SYSTEM = """You convert an exam paper into structured questions.

Return JSON only: {"questions": [ ... ]}, one entry per question, in paper order:
{"question_type": "mcq" | "fill_blank" | "short_answer",
 "prompt": "...", "options": ["...", ...], "correct_answer": "...",
 "topic": "...", "points": 1, "answer_source": "paper" | "inferred"}

Rules:
- mcq: options are the choices WITHOUT their letters; correct_answer is the
  exact text of the right option.
- fill_blank: write the blank as "____" in the prompt; correct_answer lists
  every acceptable answer separated by "|".
- short_answer: correct_answer is a model answer a marker can compare against.
- If the paper gives the answer (an answer key, a marking scheme), use it and
  set answer_source "paper". If it does not, work the answer out and set
  "inferred" — the lecturer checks every inferred answer before it is used.
- Keep mathematics as LaTeX inside $...$. Copy the question text faithfully;
  do not reword, simplify or merge questions.
- Instructions, headers, and rubric text are not questions. Skip them.
- points: the marks shown on the paper, else 1."""

_MARKING_SYSTEM = """You suggest a mark for one short-answer exam response.

Compare the student's answer with the model answer. Award marks for correct
content and reasoning, not for wording or spelling. Partial credit is allowed
in steps of 0.5. Be consistent: the same answer must always earn the same mark.

Return JSON only: {"score": <number from 0 to max>, "feedback": "<one or two
sentences for the student: what was right, what was missing>"}.
A lecturer reviews every suggestion before the student sees it."""


async def _extract_questions(text: str) -> list[dict]:
    from app.ai.rag.claude import generate_response
    from app.ai.rag.modes_sessions.service import safe_parse_json_with_retry

    async def one(slice_text: str) -> list[dict]:
        async def ask() -> str:
            return await generate_response(
                prompt=f"EXAM PAPER (or part of one):\n\n{slice_text}",
                mode="exam_import",
                system_parts=[{"type": "text", "text": _IMPORT_SYSTEM}],
            )

        parsed = await safe_parse_json_with_retry(await ask(), ask, label="exam questions")
        return [q for q in (coerce_imported_question(r) for r in parsed.get("questions") or []) if q]

    batches = await asyncio.gather(*(one(s) for s in slice_for_import(text)))
    return [q for batch in batches for q in batch]


async def suggest_mark(question: dict, answer: str) -> tuple[float, str, bool]:
    """``(score, feedback, ok)``. On any failure: 0, a note, ok=False."""
    from app.ai.rag.claude import generate_response
    from app.ai.rag.modes_sessions.service import safe_parse_json_with_retry

    points = float(question["points"])
    if not normalise_answer(answer):
        return 0.0, "No answer given.", True

    prompt = (
        f"QUESTION ({points:g} marks):\n{question['prompt']}\n\n"
        f"MODEL ANSWER:\n{question['correct_answer']}\n\n"
        f"STUDENT ANSWER:\n{answer[:4000]}"
    )

    async def ask() -> str:
        return await generate_response(
            prompt=prompt, mode="exam_marking",
            system_parts=[{"type": "text", "text": _MARKING_SYSTEM}],
        )

    try:
        parsed = await safe_parse_json_with_retry(await ask(), ask, label="mark")
        score = min(max(float(parsed.get("score", 0)), 0.0), points)
        return round(score * 2) / 2, str(parsed.get("feedback") or "")[:2000], True
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI marking failed for question %s: %s", question.get("id"), exc)
        return 0.0, "Automatic marking was unavailable — please mark by hand.", False


# ── Lecturer: papers ────────────────────────────────────────────────────────

@router.post("")
def create_assessment(payload: AssessmentCreate, request: Request, user=Depends(lecturer_guard)):
    assert_course_owner(user, payload.course_id)
    row = payload.model_dump(mode="json", exclude_none=True)
    created = _insert("assessments", {**row, "created_by": user["id"]})
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
    sb = get_supabase()
    rows = (
        sb.table("assessments").select("*")
        .eq("course_id", course_id).order("created_at").execute().data or []
    )
    ids = [r["id"] for r in rows]
    counts: dict[str, int] = {}
    if ids:
        for q in sb.table("assessment_questions").select("assessment_id").in_("assessment_id", ids).execute().data or []:
            counts[q["assessment_id"]] = counts.get(q["assessment_id"], 0) + 1
    return {"assessments": [{**r, "question_count": counts.get(r["id"], 0)} for r in rows]}


@router.patch("/{assessment_id}")
def update_assessment(
    assessment_id: str, payload: AssessmentUpdate, request: Request, user=Depends(lecturer_guard)
):
    """Title, instructions, timing and window. Allowed after publishing — a
    lecturer extending a deadline mid-exam is the common case."""
    assessment = _load_owned(assessment_id, user)
    update = payload.model_dump(
        mode="json", exclude_none=True, exclude={"untimed", "clear_window"}
    )
    if payload.untimed:
        update["time_limit_minutes"] = None
    if payload.clear_window:
        update["opens_at"] = None
        update["closes_at"] = None
    if not update:
        return {"status": "ok", "assessment": assessment}

    merged = {**assessment, **update}
    opens, closes = _ts(merged.get("opens_at")), _ts(merged.get("closes_at"))
    if opens and closes and closes <= opens:
        raise HTTPException(status_code=400, detail="The closing time must be after the opening time.")

    try:
        rows = get_supabase().table("assessments").update(update).eq("id", assessment_id).execute().data
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=_MIGRATION_HINT) from exc
    audit.record(
        actor=user, action="assessment.update", resource_type="assessment",
        resource_id=assessment_id, course_id=assessment["course_id"],
        new_values=update, request=request,
    )
    return {"status": "ok", "assessment": rows[0] if rows else merged}


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


# ── Lecturer: questions ─────────────────────────────────────────────────────

@router.get("/{assessment_id}/questions")
def list_questions(assessment_id: str, user=Depends(lecturer_guard)):
    """The whole paper WITH its answer key — lecturer only."""
    _load_owned(assessment_id, user)
    rows = (
        get_supabase().table("assessment_questions").select("*")
        .eq("assessment_id", assessment_id).order("order_index").execute().data or []
    )
    return {"questions": rows}


def _require_draft(assessment: dict) -> None:
    if assessment.get("is_published"):
        # Changing a live paper would mark students who sat it against a
        # different question from the one they answered.
        raise HTTPException(status_code=409, detail="This paper is published; its questions can no longer change.")


@router.post("/{assessment_id}/questions")
def add_question(
    assessment_id: str, payload: QuestionCreate, request: Request, user=Depends(lecturer_guard)
):
    assessment = _load_owned(assessment_id, user)
    _require_draft(assessment)

    created = _insert("assessment_questions", {**payload.model_dump(), "assessment_id": assessment_id})
    audit.record(
        actor=user, action="assessment.question_add", resource_type="assessment",
        resource_id=assessment_id, course_id=assessment["course_id"], request=request,
    )
    return {"status": "ok", "question": created[0] if created else None}


@router.post("/{assessment_id}/questions/bulk")
def add_questions(
    assessment_id: str, payload: BulkQuestions, request: Request, user=Depends(lecturer_guard)
):
    """Save a reviewed import in one go, appended after existing questions."""
    assessment = _load_owned(assessment_id, user)
    _require_draft(assessment)

    existing = get_supabase().table("assessment_questions").select("order_index").eq(
        "assessment_id", assessment_id
    ).execute().data or []
    start = max((r["order_index"] for r in existing), default=-1) + 1

    rows = [
        {**q.model_dump(), "assessment_id": assessment_id, "order_index": start + i}
        for i, q in enumerate(payload.questions)
    ]
    created = _insert("assessment_questions", rows)
    audit.record(
        actor=user, action="assessment.question_import", resource_type="assessment",
        resource_id=assessment_id, course_id=assessment["course_id"],
        new_values={"questions": len(created)}, request=request,
    )
    return {"status": "ok", "added": len(created)}


@router.delete("/{assessment_id}/questions/{question_id}")
def delete_question(assessment_id: str, question_id: str, user=Depends(lecturer_guard)):
    assessment = _load_owned(assessment_id, user)
    _require_draft(assessment)
    get_supabase().table("assessment_questions").delete().eq("id", question_id).eq(
        "assessment_id", assessment_id
    ).execute()
    return {"status": "ok", "question_id": question_id}


@router.post("/{assessment_id}/import")
async def import_questions(
    assessment_id: str, file: UploadFile = File(...), user=Depends(lecturer_guard)
):
    """Read a PDF or Word paper into DRAFT questions. Nothing is saved here.

    The lecturer reviews the drafts — especially any answer marked "inferred"
    because the paper carried no key — and saves them with /questions/bulk.
    """
    from app.ai.ingestion.math_parser import parse_blocks

    assessment = await asyncio.to_thread(_load_owned, assessment_id, user)
    _require_draft(assessment)

    filename = file.filename or "paper"
    extension = os.path.splitext(filename)[1].lstrip(".").lower()
    if extension not in _IMPORT_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Upload the paper as a PDF or a Word (.docx) file.")
    content = await file.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="That file is larger than 15 MB.")

    try:
        blocks = await asyncio.to_thread(parse_blocks, content, filename)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    text = "\n\n".join((b.get("text") or "").strip() for b in blocks if (b.get("text") or "").strip())
    if len(text) < 40:
        raise HTTPException(
            status_code=422,
            detail="No text could be read from that file. If it is a scan, export it as a text PDF or Word document.",
        )

    try:
        questions = await _extract_questions(text)
    except Exception as exc:  # noqa: BLE001 — model outage or unusable output
        logger.warning("Exam import failed for %s: %s", assessment_id, exc)
        raise HTTPException(status_code=502, detail="The paper could not be read into questions. Try again.") from exc

    if not questions:
        raise HTTPException(status_code=422, detail="No questions were found in that file.")
    return {
        "questions": questions,
        "needs_attention": sum(1 for q in questions if q["answer_source"] != "paper"),
    }


# ── Lecturer: marking, results, release ─────────────────────────────────────

@router.get("/{assessment_id}/results")
def results(assessment_id: str, user=Depends(lecturer_guard)):
    """Per-student totals, with who still has answers awaiting review."""
    assessment = _load_owned(assessment_id, user)
    sb = get_supabase()

    questions = sb.table("assessment_questions").select("id, points").eq(
        "assessment_id", assessment_id
    ).execute().data or []
    total_points = sum(float(q["points"]) for q in questions) or 1.0

    attempts = sb.table("assessment_attempts").select("*").eq(
        "assessment_id", assessment_id
    ).execute().data or []

    by_student: dict[str, dict] = {}
    for a in attempts:
        row = by_student.setdefault(a["student_id"], {"score": 0.0, "pending": 0})
        row["score"] += float(a["score"] or 0)
        row["pending"] += 1 if a.get("needs_review") else 0

    names = _names(list(by_student))
    return {
        "assessment_id": assessment_id,
        "kind": assessment["kind"],
        "results_released": bool(assessment.get("results_released")),
        "total_points": total_points,
        "responses": len(by_student),
        "pending_review": sum(r["pending"] for r in by_student.values()),
        "students": [
            {
                "student_id": sid,
                **names.get(sid, {"name": None, "email": None}),
                "score": row["score"],
                "percent": round(100 * row["score"] / total_points, 1),
                "pending_review": row["pending"],
            }
            for sid, row in sorted(by_student.items(), key=lambda x: -x[1]["score"])
        ],
    }


@router.get("/{assessment_id}/marking")
def marking_queue(assessment_id: str, user=Depends(lecturer_guard)):
    """Every short answer with the AI's suggestion, awaiting-review first."""
    _load_owned(assessment_id, user)
    sb = get_supabase()
    questions = {
        q["id"]: q
        for q in sb.table("assessment_questions").select("*").eq("assessment_id", assessment_id)
        .eq("question_type", "short_answer").execute().data or []
    }
    if not questions:
        return {"items": []}
    attempts = sb.table("assessment_attempts").select("*").eq(
        "assessment_id", assessment_id
    ).in_("question_id", list(questions)).execute().data or []
    names = _names(list({a["student_id"] for a in attempts}))

    items = [
        {
            "attempt_id": a["id"],
            "student": names.get(a["student_id"], {}).get("name"),
            "question_id": a["question_id"],
            "question": questions[a["question_id"]]["prompt"],
            "model_answer": questions[a["question_id"]]["correct_answer"],
            "points": float(questions[a["question_id"]]["points"]),
            "answer": a.get("student_answer"),
            "ai_score": a.get("ai_score"),
            "ai_feedback": a.get("ai_feedback"),
            "score": float(a.get("score") or 0),
            "needs_review": bool(a.get("needs_review")),
        }
        for a in attempts
    ]
    items.sort(key=lambda i: (not i["needs_review"], i["question_id"], i["student"] or ""))
    return {"items": items}


@router.patch("/attempts/{attempt_id}")
def confirm_mark(attempt_id: str, payload: MarkUpdate, request: Request, user=Depends(lecturer_guard)):
    """Confirm or override one mark. The AI's suggestion is kept beside it."""
    sb = get_supabase()
    rows = sb.table("assessment_attempts").select("*").eq("id", attempt_id).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail="Answer not found")
    attempt = rows[0]
    assessment = _load_owned(attempt["assessment_id"], user)

    question = sb.table("assessment_questions").select("points").eq(
        "id", attempt["question_id"]
    ).execute().data
    points = float(question[0]["points"]) if question else 0.0
    if payload.score > points:
        raise HTTPException(status_code=400, detail=f"This question is worth {points:g} marks.")

    update = {
        "score": payload.score,
        "is_correct": payload.score >= points / 2,
        "needs_review": False,
        "reviewed_by": user["id"],
        "reviewed_at": _now().isoformat(),
    }
    if payload.feedback is not None:
        update["ai_feedback"] = payload.feedback
    sb.table("assessment_attempts").update(update).eq("id", attempt_id).execute()
    audit.record(
        actor=user, action="assessment.mark", resource_type="assessment",
        resource_id=attempt["assessment_id"], course_id=assessment["course_id"],
        old_values={"score": attempt.get("score"), "ai_score": attempt.get("ai_score")},
        new_values={"score": payload.score}, request=request,
    )
    return {"status": "ok", "attempt_id": attempt_id, "score": payload.score}


@router.post("/{assessment_id}/release")
def release_results(assessment_id: str, request: Request, user=Depends(lecturer_guard)):
    assessment = _load_owned(assessment_id, user)
    sb = get_supabase()
    pending = sb.table("assessment_attempts").select("id").eq(
        "assessment_id", assessment_id
    ).eq("needs_review", True).execute().data or []
    if pending:
        raise HTTPException(
            status_code=409,
            detail=f"{len(pending)} short answer(s) still need your review before results can be released.",
        )
    sb.table("assessments").update({"results_released": True}).eq("id", assessment_id).execute()
    audit.record(
        actor=user, action="assessment.release", resource_type="assessment",
        resource_id=assessment_id, course_id=assessment["course_id"], request=request,
    )
    return {"status": "ok", "assessment_id": assessment_id}


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


# ── Lecturer: UDL accommodations ────────────────────────────────────────────

@router.get("/course/{course_id}/accommodations")
def list_accommodations(course_id: str, user=Depends(lecturer_guard)):
    assert_course_owner(user, course_id)
    try:
        rows = get_supabase().table("enrolments").select("student_id, extra_time_percent, status").eq(
            "course_id", course_id
        ).execute().data or []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=_MIGRATION_HINT) from exc
    rows = [r for r in rows if r.get("status") in ("active", "completed")]
    names = _names([r["student_id"] for r in rows])
    return {
        "students": sorted(
            (
                {
                    "student_id": r["student_id"],
                    **names.get(r["student_id"], {"name": None, "email": None}),
                    "extra_time_percent": int(r.get("extra_time_percent") or 0),
                }
                for r in rows
            ),
            key=lambda s: (s["name"] or s["email"] or "").lower(),
        )
    }


@router.put("/course/{course_id}/accommodations/{student_id}")
def set_extra_time(
    course_id: str, student_id: str, payload: ExtraTimeUpdate, request: Request,
    user=Depends(lecturer_guard),
):
    assert_course_owner(user, course_id)
    try:
        rows = get_supabase().table("enrolments").update(
            {"extra_time_percent": payload.extra_time_percent}
        ).eq("course_id", course_id).eq("student_id", student_id).execute().data
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=_MIGRATION_HINT) from exc
    if not rows:
        raise HTTPException(status_code=404, detail="That student is not enrolled on this course.")
    audit.record(
        actor=user, action="enrolment.extra_time", resource_type="enrolment",
        resource_id=student_id, course_id=course_id,
        new_values={"extra_time_percent": payload.extra_time_percent}, request=request,
    )
    return {"status": "ok", "student_id": student_id, "extra_time_percent": payload.extra_time_percent}


# ── Student ─────────────────────────────────────────────────────────────────

@router.get("/available/{course_id}")
def available(course_id: str, user=Depends(auth_guard)):
    """Published exams on this course, with this student's status on each.

    Pre/post papers are no longer listed: the client removed them from the
    student view. Their rows and results are untouched.
    """
    require_enrolment(user, course_id)
    sb = get_supabase()

    rows = [
        r for r in sb.table("assessments").select("*").eq("course_id", course_id)
        .eq("is_published", True).order("created_at").execute().data or []
        if r["kind"] in EXAM_KINDS and visible_to(r, user["id"])
    ]
    if not rows:
        return {"assessments": [], "extra_time_percent": 0}

    ids = [r["id"] for r in rows]
    done = {
        d["assessment_id"]
        for d in sb.table("assessment_attempts").select("assessment_id").eq("student_id", user["id"])
        .in_("assessment_id", ids).execute().data or []
    }
    counts: dict[str, int] = {}
    for q in sb.table("assessment_questions").select("assessment_id").in_("assessment_id", ids).execute().data or []:
        counts[q["assessment_id"]] = counts.get(q["assessment_id"], 0) + 1
    extra = _extra_time(course_id, user["id"])

    return {
        "extra_time_percent": extra,
        "assessments": [
            {
                "id": r["id"], "kind": r["kind"], "title": r["title"],
                "instructions": r.get("instructions"),
                "question_count": counts.get(r["id"], 0),
                "time_limit_minutes": r.get("time_limit_minutes"),
                "your_time_minutes": effective_minutes(r.get("time_limit_minutes"), extra),
                "opens_at": r.get("opens_at"),
                "closes_at": r.get("closes_at"),
                "completed": r["id"] in done,
                "results_released": bool(r.get("results_released")),
            }
            for r in rows
        ],
    }


@router.get("/{assessment_id}/take")
def take(assessment_id: str, user=Depends(auth_guard)):
    """Questions WITHOUT the answer key, and — for a timed paper — the deadline.

    Opening a timed paper starts its clock, once: reopening it (a refresh, a
    second tab) resumes the same deadline rather than granting a fresh one.
    """
    assessment = _load_published_for_student(assessment_id, user)
    _check_window(assessment)
    sb = get_supabase()

    already = sb.table("assessment_attempts").select("id").eq(
        "assessment_id", assessment_id
    ).eq("student_id", user["id"]).limit(1).execute().data
    if already:
        raise HTTPException(status_code=409, detail="You have already submitted this paper.")

    deadline = None
    limit = assessment.get("time_limit_minutes")
    if limit:
        sb.table("assessment_starts").upsert(
            {"assessment_id": assessment_id, "student_id": user["id"]},
            on_conflict="assessment_id,student_id", ignore_duplicates=True,
        ).execute()
        start = sb.table("assessment_starts").select("started_at").eq(
            "assessment_id", assessment_id
        ).eq("student_id", user["id"]).execute().data
        started_at = _ts(start[0]["started_at"]) if start else _now()
        deadline = deadline_for(started_at, limit, _extra_time(assessment["course_id"], user["id"]))
        closes = _ts(assessment.get("closes_at"))
        if closes and deadline > closes:
            deadline = closes  # the window closing ends the paper for everyone

    questions = sb.table("assessment_questions").select(
        # correct_answer deliberately not selected. Not filtered afterwards —
        # never fetched, so it cannot leak through a logging or error path.
        "id, order_index, prompt, options, topic, points, question_type"
    ).eq("assessment_id", assessment_id).order("order_index").execute().data or []

    return {
        "assessment": {
            "id": assessment["id"],
            "kind": assessment["kind"],
            "title": assessment["title"],
            "instructions": assessment.get("instructions"),
            "time_limit_minutes": limit,
        },
        "questions": questions,
        # The client counts down from server_now, not from its own clock.
        "deadline": deadline.isoformat() if deadline else None,
        "server_now": _now().isoformat(),
    }


def _prepare_submission(assessment_id: str, user: dict) -> tuple[dict, list[dict]]:
    assessment = _load_published_for_student(assessment_id, user)
    _check_window(assessment, grace=SUBMIT_GRACE)
    sb = get_supabase()

    already = sb.table("assessment_attempts").select("id").eq(
        "assessment_id", assessment_id
    ).eq("student_id", user["id"]).limit(1).execute().data
    if already:
        # One sitting per student; a retake would also break pre/post pairing.
        raise HTTPException(status_code=409, detail="You have already submitted this paper.")

    if assessment.get("time_limit_minutes"):
        start = sb.table("assessment_starts").select("started_at").eq(
            "assessment_id", assessment_id
        ).eq("student_id", user["id"]).execute().data
        if not start:
            raise HTTPException(status_code=400, detail="Open the paper before submitting it.")
        deadline = deadline_for(
            _ts(start[0]["started_at"]), assessment["time_limit_minutes"],
            _extra_time(assessment["course_id"], user["id"]),
        )
        if deadline and _now() > deadline + SUBMIT_GRACE:
            raise HTTPException(status_code=403, detail="The time for this paper has ended.")

    questions = sb.table("assessment_questions").select("*").eq(
        "assessment_id", assessment_id
    ).execute().data or []
    return assessment, questions


@router.post("/{assessment_id}/submit")
async def submit(assessment_id: str, payload: Submission, user=Depends(auth_guard)):
    """Mark a submission server-side and fold it into mastery."""
    assessment, questions = await asyncio.to_thread(_prepare_submission, assessment_id, user)
    course_id = assessment["course_id"]
    key = {q["id"]: q for q in questions}

    items = [(item, key[item.question_id]) for item in payload.answers if item.question_id in key]
    if not items:
        raise HTTPException(status_code=400, detail="No valid answers submitted.")

    short = [(item, q) for item, q in items if q.get("question_type") == "short_answer"]
    suggestions = await asyncio.gather(*(suggest_mark(q, item.answer) for item, q in short))
    suggested = {q["id"]: s for (_, q), s in zip(short, suggestions)}

    graded: list[dict[str, Any]] = []
    for item, question in items:
        points = float(question["points"])
        row = {
            "assessment_id": assessment_id,
            "question_id": item.question_id,
            "student_id": user["id"],
            "course_id": course_id,
            "topic": question.get("topic"),
            "student_answer": item.answer[:10_000],
            "time_spent_seconds": item.time_spent_seconds,
            # Every row carries every key. A PostgREST bulk insert takes the
            # UNION of the rows' keys and fills a missing one with NULL — not
            # the column default — so an objective row without needs_review
            # broke every paper that mixed question types (NOT NULL).
            "needs_review": False,
            "ai_score": None,
            "ai_feedback": None,
        }
        if question.get("question_type") == "short_answer":
            score, feedback, ok = suggested[question["id"]]
            row.update(
                score=score, is_correct=score >= points / 2, needs_review=True,
                # ai_feedback is shown to the STUDENT once released. A failed
                # suggestion stores nothing — found live, "please mark by hand"
                # was reaching students — and the marking queue says so instead.
                ai_score=score if ok else None,
                ai_feedback=feedback if ok else None,
            )
        else:
            correct = grade_objective(
                question.get("question_type") or "mcq", str(question["correct_answer"]), item.answer
            )
            row.update(score=points if correct else 0.0, is_correct=correct)
        graded.append(row)

    await asyncio.to_thread(lambda: get_supabase().table("assessment_attempts").insert(graded).execute())

    for row in graded:
        mastery.record_attempt(
            student_id=user["id"], course_id=course_id,
            topic=row.get("topic") or "", correct=bool(row["is_correct"]),
        )

    earned = sum(float(r["score"]) for r in graded)
    total = sum(float(q["points"]) for q in questions) or 1.0
    events.emit(
        events.ASSESSMENT_SUBMITTED,
        user_id=user["id"], course_id=course_id,
        payload={
            "assessment_id": assessment_id, "kind": assessment["kind"],
            "score": earned, "total": total, "answered": len(graded),
        },
    )

    if assessment["kind"] in EXAM_KINDS:
        # No score yet: the lecturer releases results once short answers are
        # checked. Showing a provisional AI mark and then changing it is worse
        # than a short wait.
        return {"status": "submitted", "answered": len(graded), "released": False}

    return {
        "status": "ok",
        "score": earned,
        "total": total,
        "percent": round(100 * earned / total, 1),
        "answered": len(graded),
        # Deliberately no per-question breakdown: returning which items were
        # wrong on a pre-test hands the answer key back before the post-test.
    }


@router.get("/{assessment_id}/my-result")
def my_result(assessment_id: str, user=Depends(auth_guard)):
    """This student's marked paper — only once the lecturer has released it."""
    assessment = _load_published_for_student(assessment_id, user)
    if not assessment.get("results_released"):
        return {"released": False}

    sb = get_supabase()
    questions = sb.table("assessment_questions").select("*").eq(
        "assessment_id", assessment_id
    ).order("order_index").execute().data or []
    attempts = {
        a["question_id"]: a
        for a in sb.table("assessment_attempts").select("*").eq("assessment_id", assessment_id)
        .eq("student_id", user["id"]).execute().data or []
    }
    total = sum(float(q["points"]) for q in questions) or 1.0
    earned = sum(float(a.get("score") or 0) for a in attempts.values())

    return {
        "released": True,
        "title": assessment["title"],
        "score": earned,
        "total": total,
        "percent": round(100 * earned / total, 1),
        "items": [
            {
                "prompt": q["prompt"],
                "question_type": q.get("question_type") or "mcq",
                "options": q.get("options") or [],
                "points": float(q["points"]),
                "your_answer": (attempts.get(q["id"]) or {}).get("student_answer"),
                "score": float((attempts.get(q["id"]) or {}).get("score") or 0),
                # Safe now: results are released and the paper cannot be retaken.
                "correct_answer": q["correct_answer"],
                "feedback": (attempts.get(q["id"]) or {}).get("ai_feedback"),
            }
            for q in questions
        ],
    }


@router.get("/mastery/{course_id}")
def my_mastery(course_id: str, user=Depends(auth_guard)):
    require_enrolment(user, course_id)
    return {"topics": mastery.for_student(user["id"], course_id)}
