"""Course-level analytics for the lecturer dashboard.

Deliberately built from tables that exist today (enrolments, sessions,
conversations, documents, media_assets, mastery_states, events, feedback).
Anything that cannot be measured from them is named in ``unavailable`` rather
than approximated — a plausible-looking number derived from the wrong table is
worse than an honest gap in a study.

Two endpoints, and the split is the point. ``GET ""`` is the cohort rollup a
lecturer reads at a glance; ``GET /mastery`` is the per-student breakdown they
act on in a tutorial. Folding the second into the first would put a row per
student behind every dashboard load.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

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
        "time_on_task": _time_on_task(sessions),
        # Named explicitly so the dashboard shows "not yet measured" rather
        # than a zero the lecturer would read as "no learning happened".
        # Empty now that time-on-task is derived from session timestamps —
        # kept as a field so the UI does not need a release to show the next
        # metric that is not yet instrumented.
        "unavailable": [],
    }


def _time_on_task(sessions: list[dict]) -> dict:
    """Minutes actually spent studying, from session start/end timestamps.

    Only CLOSED sessions count. An open session has no end time, and treating
    "now" as the end would score a student who left a tab open overnight as the
    most engaged in the cohort — which is precisely the number a lecturer would
    act on and be wrong about.

    The median is reported alongside the mean because the distribution is
    heavily skewed: a handful of long sessions drag the mean well above what a
    typical student does.
    """
    durations: list[float] = []
    open_sessions = 0

    for session in sessions:
        started, ended = session.get("started_at"), session.get("ended_at")
        if not ended:
            open_sessions += 1
            continue
        try:
            start = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        minutes = (end - start).total_seconds() / 60
        # Clamped: clock skew produces negatives, and a session longer than
        # four hours is an abandoned tab that was eventually closed, not study.
        if 0 < minutes <= 240:
            durations.append(minutes)

    if not durations:
        return {
            "measured": False,
            "reason": "No completed sessions yet.",
            "open_sessions": open_sessions,
        }

    ordered = sorted(durations)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )

    return {
        "measured": True,
        "completed_sessions": len(durations),
        "open_sessions": open_sessions,
        "total_minutes": round(sum(durations)),
        "mean_minutes": round(sum(durations) / len(durations), 1),
        "median_minutes": round(median, 1),
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

    ranked = sorted(
        ({"topic": t, "mean": round(sum(v) / len(v), 3), "students": len(v)}
         for t, v in per_topic.items()),
        key=lambda x: x["mean"],
    )

    # Split by BAND, not by position in the ranking.
    #
    # Taking the bottom five and the top five puts the same topic in both lists
    # whenever a course has five or fewer topics — which is every course early
    # in a pilot. A lecturer then reads "chain rule" under both "needs
    # reteaching" and "secure" and reasonably concludes the dashboard is
    # broken. Splitting on the mastery band makes the two lists disjoint at any
    # cohort size, and means what it says: these are weak, those are not.
    weakest = [t for t in ranked if band(t["mean"]) != "secure"][:5]
    strongest = [t for t in reversed(ranked) if band(t["mean"]) == "secure"][:5]

    return {
        "measured": True,
        "students_tracked": len({r["student_id"] for r in rows}),
        "topics_tracked": len(per_topic),
        "bands": buckets,
        "weakest_topics": weakest,
        "strongest_topics": strongest,
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


@router.get("/mastery")
def mastery_breakdown(course_id: str, user=Depends(lecturer_guard)):
    """Per-topic and per-student mastery, for the Overview mastery table.

    The rollup in the main analytics answers "how is the cohort doing"; this
    answers "which student needs help with what", which is the only version a
    lecturer can act on in a tutorial.
    """
    assert_course_owner(user, course_id)
    sb = get_supabase()

    rows = _safe(
        lambda: sb.table("mastery_states")
        .select("student_id, topic, mastery_score, attempts_count, correct_count, "
                "last_practised_at")
        .eq("course_id", course_id).limit(20000).execute().data or [],
        [],
    )
    if not rows:
        return {"measured": False, "reason": "No graded attempts yet.", "topics": [], "students": []}

    from app.core.mastery import band

    profiles = {}
    student_ids = list({r["student_id"] for r in rows})
    if student_ids:
        found = _safe(
            lambda: sb.table("users").select("id, first_name, last_name, email")
            .in_("id", student_ids).execute().data or [],
            [],
        )
        profiles = {p["id"]: p for p in found}

    per_topic: dict[str, list[dict]] = {}
    per_student: dict[str, list[dict]] = {}
    for row in rows:
        score = float(row["mastery_score"])
        entry = {**row, "band": band(score)}
        per_topic.setdefault(row["topic"], []).append(entry)
        per_student.setdefault(row["student_id"], []).append(entry)

    topics = sorted(
        (
            {
                "topic": topic,
                "students": len(entries),
                "mean": round(sum(float(e["mastery_score"]) for e in entries) / len(entries), 3),
                "attempts": sum(int(e.get("attempts_count") or 0) for e in entries),
                "bands": {
                    b: sum(1 for e in entries if e["band"] == b)
                    for b in ("secure", "developing", "struggling")
                },
            }
            for topic, entries in per_topic.items()
        ),
        key=lambda t: t["mean"],
    )

    students = []
    for student_id, entries in per_student.items():
        profile = profiles.get(student_id, {})
        name = " ".join(
            p for p in (profile.get("first_name"), profile.get("last_name")) if p
        ).strip()
        mean = round(sum(float(e["mastery_score"]) for e in entries) / len(entries), 3)
        students.append({
            "student_id": student_id,
            "name": name or None,
            "email": profile.get("email"),
            "topics_tracked": len(entries),
            "mean": mean,
            "band": band(mean),
            "struggling_topics": sorted(
                e["topic"] for e in entries if e["band"] == "struggling"
            ),
            "last_practised_at": max(
                (e.get("last_practised_at") or "" for e in entries), default=None
            ) or None,
        })

    # Weakest first. A lecturer opening this page is looking for who to help,
    # not for a class ranking.
    students.sort(key=lambda s: s["mean"])

    return {"measured": True, "topics": topics, "students": students}


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
