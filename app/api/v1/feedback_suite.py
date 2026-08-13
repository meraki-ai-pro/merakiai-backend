"""The full feedback suite: micro, mode, NPS, lecturer, exit.

Ref: AI_Teaching_System_Technical_Specification_v3 §4

One table and one endpoint for all five, because a study analyses them as one
variable with a type column. The existing /feedback routes are left alone —
they have live data and their own shapes.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.core.auth import assert_course_owner, auth_guard, lecturer_guard
from app.db.supabase import get_supabase, get_user_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback-suite", tags=["Feedback"])

FeedbackType = Literal["micro", "nps", "mode", "lecturer", "exit"]

# Tech Spec §4: NPS every 3-4 weeks. Asking more often trains people to dismiss
# it and makes the trend meaningless.
NPS_COOLDOWN_DAYS = 21


class FeedbackIn(BaseModel):
    feedback_type: FeedbackType
    course_id: str | None = Field(None, max_length=100)
    session_id: str | None = None
    rating: int | None = Field(None, ge=1, le=5)
    nps_score: int | None = Field(None, ge=0, le=10)
    mode: str | None = Field(None, max_length=20)
    free_text: str | None = Field(None, max_length=5000)
    structured_answers: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_shape(self):
        """Each type needs its own measure present.

        Enforced here rather than left to analysis: a table of NPS rows with
        null scores is discovered at the end of a pilot, when it is too late
        to re-collect.
        """
        if self.feedback_type == "nps" and self.nps_score is None:
            raise ValueError("nps_score (0-10) is required for NPS feedback")
        if self.feedback_type in ("micro", "mode") and self.rating is None:
            raise ValueError("rating (1-5) is required for micro and mode feedback")
        if self.feedback_type == "mode" and not self.mode:
            raise ValueError("mode is required for mode feedback")
        if self.feedback_type == "nps" and self.rating is not None:
            # Different scales. Letting both through invites someone to average
            # them later.
            raise ValueError("NPS uses nps_score (0-10), not rating (1-5)")
        return self


@router.post("")
def submit_feedback(body: FeedbackIn, user=Depends(auth_guard)):
    """Record one piece of feedback. RLS pins user_id to the caller."""
    row = {
        "user_id": user["id"],
        "role_at_time": user.get("role") or "user",
        "course_id": body.course_id,
        "session_id": body.session_id,
        "feedback_type": body.feedback_type,
        "rating": body.rating,
        "nps_score": body.nps_score,
        "mode": body.mode,
        "free_text": body.free_text,
        "structured_answers": body.structured_answers,
    }

    try:
        created = get_user_client(user["token"]).table("feedback_responses").insert(
            row
        ).execute().data
    except Exception as exc:  # noqa: BLE001
        logger.warning("Feedback insert failed: %s", exc)
        raise HTTPException(
            status_code=500, detail="Could not record your feedback. Please try again."
        ) from exc

    return {"status": "ok", "id": created[0]["id"] if created else None}


@router.get("/due")
def what_is_due(course_id: str | None = None, user=Depends(auth_guard)):
    """Whether this student should be asked for NPS right now.

    The client asks; the server decides. Leaving the cooldown to the client
    means every device and every reinstall re-prompts.
    """
    try:
        rows = (
            get_supabase()
            .table("feedback_responses")
            .select("created_at")
            .eq("user_id", user["id"])
            .eq("feedback_type", "nps")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 — table may not exist yet
        logger.warning("NPS due-check failed: %s", exc)
        return {"nps_due": False}

    if not rows:
        return {"nps_due": True, "reason": "never_asked"}

    try:
        last = datetime.fromisoformat(rows[0]["created_at"].replace("Z", "+00:00"))
    except (ValueError, KeyError, AttributeError):
        return {"nps_due": False}

    due_at = last + timedelta(days=NPS_COOLDOWN_DAYS)
    now = datetime.now(timezone.utc)
    return {
        "nps_due": now >= due_at,
        "last_asked": rows[0]["created_at"],
        "next_due": due_at.isoformat(),
    }


def _nps(scores: list[int]) -> dict[str, Any]:
    """Net Promoter Score: %promoters − %detractors.

    Passives (7-8) count in the denominator but not the numerator — that is the
    definition, and dropping them inflates the score.
    """
    if not scores:
        return {"measured": False, "reason": "No NPS responses yet."}
    promoters = sum(1 for s in scores if s >= 9)
    detractors = sum(1 for s in scores if s <= 6)
    n = len(scores)
    return {
        "measured": True,
        "n": n,
        "score": round(100 * (promoters - detractors) / n),
        "promoters": promoters,
        "passives": n - promoters - detractors,
        "detractors": detractors,
    }


@router.get("/summary/{course_id}")
def course_summary(course_id: str, user=Depends(lecturer_guard)):
    """Feedback rollup for a course."""
    assert_course_owner(user, course_id)

    try:
        rows = (
            get_supabase()
            .table("feedback_responses")
            .select("feedback_type, rating, nps_score, mode, free_text, created_at")
            .eq("course_id", course_id)
            .limit(20000)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Feedback summary failed: %s", exc)
        return {"measured": False, "reason": "Feedback table is not available."}

    if not rows:
        return {"measured": False, "reason": "No feedback submitted yet."}

    by_type = Counter(r["feedback_type"] for r in rows)
    ratings = [r["rating"] for r in rows if r.get("rating") is not None]
    per_mode: dict[str, list[int]] = {}
    for r in rows:
        if r.get("mode") and r.get("rating") is not None:
            per_mode.setdefault(r["mode"], []).append(r["rating"])

    return {
        "measured": True,
        "responses": len(rows),
        "by_type": dict(by_type),
        "mean_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "nps": _nps([r["nps_score"] for r in rows if r.get("nps_score") is not None]),
        "by_mode": {
            m: {"n": len(v), "mean": round(sum(v) / len(v), 2)} for m, v in per_mode.items()
        },
        # Free text is returned verbatim and unsummarised on purpose — the
        # qualitative themes are the lecturer's to read, not ours to compress.
        "comments": [
            {"type": r["feedback_type"], "text": r["free_text"], "at": r["created_at"]}
            for r in rows
            if r.get("free_text")
        ][:200],
    }
