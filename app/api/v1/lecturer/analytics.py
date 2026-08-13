"""Course-level analytics for the lecturer dashboard.

Deliberately built from tables that exist today (enrolments, sessions,
conversations, documents, media_assets, feedback). The richer research metrics
— mastery progression, pre/post gains, video watch percentage — need the events
stream and mastery_states from tasks #20 and #21, and are reported as
unavailable rather than approximated. A plausible-looking number derived from
the wrong table is worse than an honest gap in a study.
"""

from __future__ import annotations

import logging
from collections import Counter

from fastapi import APIRouter, Depends

from app.core.auth import assert_course_owner, lecturer_guard
from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/courses/{course_id}/analytics", tags=["Lecturer – Analytics"])


def _safe(fn, default):
    """Run one metric query; a missing table degrades that metric only."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics metric unavailable: %s", exc)
        return default


@router.get("")
def course_analytics(course_id: str, user=Depends(lecturer_guard)):
    assert_course_owner(user, course_id)
    sb = get_supabase()

    enrolments = _safe(
        lambda: sb.table("enrolments").select("status, student_id")
        .eq("course_id", course_id).execute().data or [],
        [],
    )
    status_counts = Counter(e["status"] for e in enrolments)
    student_ids = [e["student_id"] for e in enrolments]

    sessions = _safe(
        lambda: sb.table("sessions").select("id, user_id, current_mode, started_at, ended_at")
        .eq("course_id", course_id).limit(10000).execute().data or [],
        [],
    )
    mode_counts = Counter((s.get("current_mode") or "learn") for s in sessions)

    documents = _safe(
        lambda: sb.table("documents").select("id, is_published, status, target_modes, deleted_at")
        .eq("course_id", course_id).execute().data or [],
        [],
    )
    live_docs = [d for d in documents if not d.get("deleted_at")]

    videos = _safe(
        lambda: sb.table("media_assets").select("id, status, approved_at")
        .eq("course_id", course_id).execute().data or [],
        [],
    )

    # Engagement is measured as distinct ENROLLED students who have actually
    # opened a session — an enrolled student who never logged in is the number
    # a pilot most needs to see separated out.
    #
    # Intersected with the enrolment list deliberately. Counting every session
    # author includes admins and lecturers testing the course, which produced
    # "7 of 5 students have started" on a real dashboard and, worse, drove
    # enrolled_but_never_started to zero through the max() below.
    enrolled_ids = set(student_ids)
    started_ids = {s["user_id"] for s in sessions} & enrolled_ids
    active_students = len(started_ids)
    staff_sessions = len({s["user_id"] for s in sessions} - enrolled_ids)

    return {
        "course_id": course_id,
        "students": {
            "total": len(enrolments),
            "active": status_counts.get("active", 0),
            "completed": status_counts.get("completed", 0),
            "withdrawn": status_counts.get("withdrawn", 0),
            "ever_opened_a_session": active_students,
            "enrolled_but_never_started": len(enrolled_ids) - active_students,
            # Surfaced separately rather than folded in, so a lecturer's own
            # testing is visible but never inflates the cohort figures.
            "non_student_session_users": staff_sessions,
        },
        "sessions": {
            "total": len(sessions),
            "by_mode": {
                "learn": mode_counts.get("learn", 0),
                "review": mode_counts.get("review", 0),
                "application": mode_counts.get("application", 0),
            },
        },
        "knowledge": {
            "total": len(live_docs),
            "published": sum(1 for d in live_docs if d.get("is_published") is not False),
            "draft": sum(1 for d in live_docs if d.get("is_published") is False),
            "failed": sum(1 for d in live_docs if d.get("status") == "failed"),
        },
        "videos": {
            "total": len(videos),
            "awaiting_review": sum(
                1 for v in videos if v.get("status") == "ready" and not v.get("approved_at")
            ),
            "approved": sum(1 for v in videos if v.get("approved_at")),
            "failed": sum(1 for v in videos if v.get("status") == "failed"),
        },
        "mastery": _mastery_summary(sb, course_id),
        "engagement": _engagement_summary(sb, course_id),
        # Named explicitly so the dashboard shows "not yet measured" rather
        # than a zero the lecturer would read as "no learning happened".
        # Shrinks as instrumentation lands: mastery and pre/post gains moved
        # out of this list once #20/#21 shipped.
        "unavailable": ["time_on_task"],
    }


def _mastery_summary(sb, course_id: str) -> dict:
    """Cohort mastery, bucketed. Reported as counts, not a cohort mean —
    an average over topics with wildly different attempt counts is not a
    number anyone should act on."""
    rows = _safe(
        lambda: sb.table("mastery_states").select("topic, mastery_score, student_id")
        .eq("course_id", course_id).limit(20000).execute().data or [],
        [],
    )
    if not rows:
        return {"measured": False, "reason": "No graded attempts yet."}

    from app.core.mastery import band

    buckets = {"secure": 0, "developing": 0, "struggling": 0}
    per_topic: dict[str, list[float]] = {}
    for r in rows:
        score = float(r["mastery_score"])
        buckets[band(score)] += 1
        per_topic.setdefault(r["topic"], []).append(score)

    weakest = sorted(
        ({"topic": t, "mean": round(sum(v) / len(v), 3), "students": len(v)}
         for t, v in per_topic.items()),
        key=lambda x: x["mean"],
    )[:5]

    return {
        "measured": True,
        "students_tracked": len({r["student_id"] for r in rows}),
        "topics_tracked": len(per_topic),
        "bands": buckets,
        "weakest_topics": weakest,
    }


def _engagement_summary(sb, course_id: str) -> dict:
    """Counts straight off the events stream."""
    rows = _safe(
        lambda: sb.table("events").select("event_type, user_id")
        .eq("course_id", course_id).limit(50000).execute().data or [],
        [],
    )
    if not rows:
        return {"measured": False, "reason": "No events recorded yet."}

    counts = Counter(r["event_type"] for r in rows)
    return {
        "measured": True,
        "turns": counts.get("turn.completed", 0),
        "citations_clicked": counts.get("citation.clicked", 0),
        "sources_opened": counts.get("source.drawer_opened", 0),
        "videos_completed": counts.get("video.completed", 0),
        "narration_played": counts.get("board.narration_played", 0),
        # A direct measure of how often the knowledge base cannot answer —
        # the number that should drive what the lecturer uploads next.
        "empty_retrievals": counts.get("retrieval.empty", 0),
    }


@router.get("/knowledge-usage")
def knowledge_usage(course_id: str, user=Depends(lecturer_guard)):
    """Which files retrieval actually draws on.

    Requires the events stream (#20) to be meaningful — retrieval hits are not
    recorded anywhere today. Returns the document list with a null usage count
    so the UI can render the table and label the column honestly.
    """
    assert_course_owner(user, course_id)
    docs = _safe(
        lambda: get_supabase().table("documents")
        .select("id, title, target_modes, is_published")
        .eq("course_id", course_id).execute().data or [],
        [],
    )
    return {
        "documents": [{**d, "retrieval_count": None} for d in docs],
        "note": "Retrieval counts require the events stream (task #20).",
    }
