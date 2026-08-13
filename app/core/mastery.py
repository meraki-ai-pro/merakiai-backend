"""Per-topic mastery.

Mastery is an exponential moving average of correctness, not a raw percentage.

The difference matters for teaching. A student who got their first five
attempts at limits wrong and their last five right has 50% correct and clearly
understands it now; a plain ratio would say the opposite, and would take
another five correct answers to admit it. The EMA lets recent evidence move the
number while older evidence decays, which is what "has this student got it yet"
actually means.

Ref: AI_Teaching_System_Technical_Specification_v3 §5.1, §6.4
"""

from __future__ import annotations

import logging

from app.core import events
from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

# How fast recent attempts dominate. 0.3 means roughly the last handful of
# attempts carry most of the weight — responsive enough to reflect a student
# who has just understood something, damped enough that one lucky guess does
# not read as mastery.
ALPHA = 0.3

# Below this a topic is "struggling"; above it, "secure". Two bands rather than
# a percentage, because the underlying signal does not support more precision.
SECURE_THRESHOLD = 0.7
STRUGGLING_THRESHOLD = 0.4


def _next_score(previous: float | None, correct: bool, attempts: int) -> float:
    """EMA update, with the first few attempts weighted more heavily.

    With a cold start at 0, a first correct answer would otherwise move the
    score only to 0.3, which reads as failure. Seeding from the first attempt
    avoids that without letting one answer stand as final.
    """
    observed = 1.0 if correct else 0.0
    if previous is None or attempts == 0:
        return observed
    return round((1 - ALPHA) * previous + ALPHA * observed, 4)


def band(score: float) -> str:
    if score >= SECURE_THRESHOLD:
        return "secure"
    if score >= STRUGGLING_THRESHOLD:
        return "developing"
    return "struggling"


def record_attempt(
    *,
    student_id: str,
    course_id: str,
    topic: str,
    correct: bool,
) -> dict | None:
    """Fold one graded attempt into the student's mastery for a topic.

    Never raises: this runs after the answer has already been recorded, and a
    failure to update a derived statistic must not lose the attempt itself.
    """
    if not topic:
        # Untagged questions cannot contribute to a per-topic measure. Silently
        # skipping is correct; guessing a topic would corrupt the dataset.
        return None

    # Inside the try, not before it: get_supabase() itself can fail (missing
    # config, unreachable host), and this function promises never to raise.
    try:
        sb = get_supabase()
        existing = (
            sb.table("mastery_states")
            .select("id, mastery_score, attempts_count, correct_count")
            .eq("student_id", student_id)
            .eq("course_id", course_id)
            .eq("topic", topic)
            .execute()
            .data
        )
    except Exception as exc:  # noqa: BLE001 — table may not exist yet
        logger.warning("Mastery lookup failed for %s/%s: %s", student_id, topic, exc)
        return None

    if existing:
        row = existing[0]
        attempts = int(row.get("attempts_count") or 0)
        score = _next_score(float(row.get("mastery_score") or 0), correct, attempts)
        updates = {
            "mastery_score": score,
            "attempts_count": attempts + 1,
            "correct_count": int(row.get("correct_count") or 0) + (1 if correct else 0),
            "last_practised_at": "now()",
        }
        try:
            sb.table("mastery_states").update(updates).eq("id", row["id"]).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mastery update failed: %s", exc)
            return None
        result = {**updates, "topic": topic, "band": band(score)}
    else:
        score = _next_score(None, correct, 0)
        try:
            sb.table("mastery_states").insert({
                "student_id": student_id,
                "course_id": course_id,
                "topic": topic,
                "mastery_score": score,
                "attempts_count": 1,
                "correct_count": 1 if correct else 0,
                "last_practised_at": "now()",
            }).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mastery insert failed: %s", exc)
            return None
        result = {"mastery_score": score, "attempts_count": 1, "topic": topic, "band": band(score)}

    events.emit(
        events.MASTERY_UPDATED,
        user_id=student_id,
        course_id=course_id,
        topic=topic,
        payload={"score": result["mastery_score"], "correct": correct},
    )
    return result


def for_student(student_id: str, course_id: str) -> list[dict]:
    try:
        rows = (
            get_supabase()
            .table("mastery_states")
            .select("topic, mastery_score, attempts_count, correct_count, last_practised_at")
            .eq("student_id", student_id)
            .eq("course_id", course_id)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Mastery read failed: %s", exc)
        return []

    return sorted(
        ({**r, "band": band(float(r["mastery_score"]))} for r in rows),
        key=lambda r: r["mastery_score"],
    )
