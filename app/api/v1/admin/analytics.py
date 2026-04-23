from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from app.core.auth import admin_guard
from app.db.supabase import get_supabase

router = APIRouter(prefix="/analytics", tags=["Admin – Analytics"])


def _avg(values: list) -> float | None:
    nums = [v for v in values if v is not None]
    return round(sum(nums) / len(nums), 2) if nums else None


def _date_bins(records: list, date_field: str, days: int) -> List[Dict[str, Any]]:
    """Bucket records into daily counts for the last `days` days."""
    now = datetime.now(timezone.utc)
    bins: Dict[str, int] = {}
    for i in range(days):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        bins[day] = 0
    for r in records:
        raw = r.get(date_field, "")
        if raw:
            day = raw[:10]
            if day in bins:
                bins[day] += 1
    return [{"date": d, "count": c} for d, c in sorted(bins.items())]


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@router.get("/overview")
def analytics_overview(_user=Depends(admin_guard)):
    """High-level platform snapshot for the admin dashboard."""
    sb = get_supabase()

    total_users = (sb.table("users").select("id", count="exact").limit(0).execute()).count or 0
    total_sessions = (sb.table("sessions").select("id", count="exact").limit(0).execute()).count or 0
    total_conversations = (sb.table("conversations").select("id", count="exact").limit(0).execute()).count or 0
    total_reviews = (sb.table("review_summaries").select("id", count="exact").limit(0).execute()).count or 0
    survey_count = (sb.table("session_surveys").select("id", count="exact").limit(0).execute()).count or 0
    user_feedback_count = (sb.table("user_feedback").select("id", count="exact").limit(0).execute()).count or 0
    mode_feedback_count = (sb.table("mode_feedback").select("id", count="exact").limit(0).execute()).count or 0

    # Active users in last 30 days (unique)
    cutoff = _cutoff_iso(30)
    logins_30d = sb.table("platform_sessions").select("user_id").gte("login_at", cutoff).execute().data or []
    active_users_30d = len({r["user_id"] for r in logins_30d})

    # Avg overall rating from session surveys
    surveys = sb.table("session_surveys").select("overall_rating").execute().data or []
    avg_overall_rating = _avg([r["overall_rating"] for r in surveys])

    # Avg review score
    summaries = sb.table("review_summaries").select("overall_score").execute().data or []
    avg_review_score = _avg([r["overall_score"] for r in summaries])

    # Completed sessions (ended_at is set)
    completed = sb.table("sessions").select("id", count="exact").not_.is_("ended_at", "null").limit(0).execute()
    sessions_completed = completed.count or 0

    # Total platform time (sum of ws_session durations)
    ws = sb.table("ws_sessions").select("duration_sec").not_.is_("duration_sec", "null").execute().data or []
    total_platform_minutes = round(sum(r["duration_sec"] for r in ws) / 60, 1)

    return {
        "total_users": total_users,
        "active_users_30d": active_users_30d,
        "total_sessions": total_sessions,
        "sessions_completed": sessions_completed,
        "total_conversations": total_conversations,
        "total_reviews_completed": total_reviews,
        "avg_overall_rating": avg_overall_rating,
        "avg_review_score": avg_review_score,
        "total_platform_minutes": total_platform_minutes,
        "survey_count": survey_count,
        "user_feedback_count": user_feedback_count,
        "mode_feedback_count": mode_feedback_count,
    }


# ---------------------------------------------------------------------------
# User growth & activity
# ---------------------------------------------------------------------------

@router.get("/users")
def analytics_users(days: int = Query(30, ge=1, le=365), _user=Depends(admin_guard)):
    sb = get_supabase()
    cutoff = _cutoff_iso(days)

    all_users = sb.table("users").select("role, country, region, university_name, created_at").execute().data or []
    logins = sb.table("platform_sessions").select("user_id, login_at").gte("login_at", cutoff).execute().data or []

    by_role = dict(Counter(u["role"] for u in all_users))
    by_country = dict(Counter(u.get("country") or "Unknown" for u in all_users))
    by_university = dict(Counter(u.get("university_name") or "Unknown" for u in all_users))

    new_users = [u for u in all_users if (u.get("created_at") or "") >= cutoff]
    signup_trend = _date_bins(new_users, "created_at", days)
    login_trend = _date_bins(logins, "login_at", days)

    unique_active = len({r["user_id"] for r in logins})

    return {
        "total_users": len(all_users),
        "active_users_in_period": unique_active,
        "by_role": by_role,
        "by_country": by_country,
        "by_university": by_university,
        "signup_trend": signup_trend,
        "login_trend": login_trend,
    }


# ---------------------------------------------------------------------------
# Session activity
# ---------------------------------------------------------------------------

@router.get("/sessions")
def analytics_sessions(days: int = Query(30, ge=1, le=365), _user=Depends(admin_guard)):
    sb = get_supabase()
    cutoff = _cutoff_iso(days)

    sessions = (
        sb.table("sessions")
        .select("id, current_mode, course_id, started_at, ended_at")
        .gte("started_at", cutoff)
        .execute().data or []
    )
    mode_sessions = (
        sb.table("mode_sessions")
        .select("mode, completed, duration_sec, difficulty, message_count")
        .gte("started_at", cutoff)
        .execute().data or []
    )
    ws = (
        sb.table("ws_sessions")
        .select("duration_sec")
        .gte("connected_at", cutoff)
        .not_.is_("duration_sec", "null")
        .execute().data or []
    )

    by_mode = dict(Counter(s["current_mode"] for s in sessions))
    by_course = dict(Counter(s.get("course_id") or "none" for s in sessions))
    session_trend = _date_bins(sessions, "started_at", days)

    # Duration stats from ws_sessions
    durations = [r["duration_sec"] for r in ws if r["duration_sec"]]
    avg_session_min = round(sum(durations) / len(durations) / 60, 2) if durations else None

    # Mode session stats grouped by mode
    mode_stats: Dict[str, Any] = defaultdict(lambda: {"count": 0, "completed": 0, "durations": [], "messages": []})
    for ms in mode_sessions:
        m = ms["mode"]
        mode_stats[m]["count"] += 1
        if ms.get("completed"):
            mode_stats[m]["completed"] += 1
        if ms.get("duration_sec"):
            mode_stats[m]["durations"].append(ms["duration_sec"])
        if ms.get("message_count"):
            mode_stats[m]["messages"].append(ms["message_count"])

    mode_breakdown = {}
    for m, stats in mode_stats.items():
        d = stats["durations"]
        msgs = stats["messages"]
        mode_breakdown[m] = {
            "total": stats["count"],
            "completed": stats["completed"],
            "completion_rate": round(stats["completed"] / stats["count"], 2) if stats["count"] else None,
            "avg_duration_min": round(sum(d) / len(d) / 60, 2) if d else None,
            "avg_messages": round(sum(msgs) / len(msgs), 1) if msgs else None,
        }

    return {
        "total_sessions": len(sessions),
        "by_mode": by_mode,
        "by_course": by_course,
        "avg_session_duration_min": avg_session_min,
        "session_trend": session_trend,
        "mode_breakdown": mode_breakdown,
    }


# ---------------------------------------------------------------------------
# Feedback (integrated from all feedback tables)
# ---------------------------------------------------------------------------

@router.get("/feedback")
def analytics_feedback(days: int = Query(90, ge=1, le=365), _user=Depends(admin_guard)):
    sb = get_supabase()
    cutoff = _cutoff_iso(days)

    # Session surveys
    surveys = sb.table("session_surveys").select(
        "clarity_rating, helpfulness_rating, confidence_rating, overall_rating, created_at"
    ).gte("created_at", cutoff).execute().data or []

    survey_stats = {
        "count": len(surveys),
        "avg_clarity": _avg([s["clarity_rating"] for s in surveys]),
        "avg_helpfulness": _avg([s["helpfulness_rating"] for s in surveys]),
        "avg_confidence": _avg([s["confidence_rating"] for s in surveys]),
        "avg_overall": _avg([s["overall_rating"] for s in surveys]),
    }

    # Mode feedback — grouped by mode
    mf_rows = sb.table("mode_feedback").select(
        "mode, ease_of_understanding, engagement_level, usefulness, created_at"
    ).gte("created_at", cutoff).execute().data or []

    mf_by_mode: Dict[str, Any] = defaultdict(lambda: {"ease": [], "engagement": [], "usefulness": []})
    for r in mf_rows:
        m = r["mode"]
        if r.get("ease_of_understanding"):
            mf_by_mode[m]["ease"].append(r["ease_of_understanding"])
        if r.get("engagement_level"):
            mf_by_mode[m]["engagement"].append(r["engagement_level"])
        if r.get("usefulness"):
            mf_by_mode[m]["usefulness"].append(r["usefulness"])

    mode_feedback_stats = {
        m: {
            "count": len(v["ease"]) or len(v["engagement"]) or len(v["usefulness"]),
            "avg_ease": _avg(v["ease"]),
            "avg_engagement": _avg(v["engagement"]),
            "avg_usefulness": _avg(v["usefulness"]),
        }
        for m, v in mf_by_mode.items()
    }

    # User feedback
    uf_rows = sb.table("user_feedback").select(
        "id, feedback_type, message, created_at"
    ).gte("created_at", cutoff).order("created_at", desc=True).execute().data or []

    by_type = dict(Counter(r["feedback_type"] for r in uf_rows))
    recent_feedback = uf_rows[:20]

    # Content quality flags
    quality_flags = sb.table("content_quality_flags").select("*").order("detected_on", desc=True).limit(50).execute().data or []

    return {
        "session_surveys": survey_stats,
        "mode_feedback": mode_feedback_stats,
        "user_feedback": {
            "total": len(uf_rows),
            "by_type": by_type,
            "recent": recent_feedback,
        },
        "content_quality_flags": quality_flags,
    }


# ---------------------------------------------------------------------------
# Learning outcomes
# ---------------------------------------------------------------------------

@router.get("/learning-outcomes")
def analytics_learning_outcomes(days: int = Query(30, ge=1, le=365), _user=Depends(admin_guard)):
    sb = get_supabase()
    cutoff = _cutoff_iso(days)

    summaries = sb.table("review_summaries").select(
        "overall_score, generated_at"
    ).gte("generated_at", cutoff).execute().data or []

    attempts = sb.table("review_attempts").select(
        "verdict, score, created_at"
    ).gte("created_at", cutoff).execute().data or []

    surveys = sb.table("session_surveys").select(
        "confidence_rating, created_at"
    ).gte("created_at", cutoff).execute().data or []

    verdict_dist = dict(Counter(a["verdict"] for a in attempts))
    total_attempts = len(attempts)
    correct = verdict_dist.get("correct", 0) + verdict_dist.get("partially_correct", 0)
    pass_rate = round(correct / total_attempts, 3) if total_attempts else None

    avg_review_score = _avg([s["overall_score"] for s in summaries])
    avg_confidence = _avg([s["confidence_rating"] for s in surveys])
    avg_attempt_score = _avg([a["score"] for a in attempts])

    score_trend = _date_bins(summaries, "generated_at", days)

    return {
        "total_reviews": len(summaries),
        "total_attempts": total_attempts,
        "avg_review_score": avg_review_score,
        "avg_attempt_score": avg_attempt_score,
        "avg_confidence_rating": avg_confidence,
        "pass_rate": pass_rate,
        "verdict_distribution": verdict_dist,
        "score_trend": score_trend,
    }


# ---------------------------------------------------------------------------
# AI / system performance
# ---------------------------------------------------------------------------

@router.get("/performance")
def analytics_performance(days: int = Query(30, ge=1, le=365), _user=Depends(admin_guard)):
    sb = get_supabase()
    cutoff = _cutoff_iso(days)

    metrics = sb.table("request_metrics").select(
        "mode, response_format, processing_time_sec, ai_processing_ms, video_generation_ms, created_at"
    ).gte("created_at", cutoff).execute().data or []

    by_mode: Dict[str, Any] = defaultdict(lambda: {"count": 0, "total_sec": [], "ai_ms": [], "video_ms": []})
    by_format: Dict[str, int] = Counter()

    for r in metrics:
        m = r.get("mode") or "unknown"
        fmt = r.get("response_format") or "text"
        by_mode[m]["count"] += 1
        if r.get("processing_time_sec"):
            by_mode[m]["total_sec"].append(r["processing_time_sec"])
        if r.get("ai_processing_ms"):
            by_mode[m]["ai_ms"].append(r["ai_processing_ms"])
        if r.get("video_generation_ms"):
            by_mode[m]["video_ms"].append(r["video_generation_ms"])
        by_format[fmt] += 1

    mode_perf = {}
    for m, v in by_mode.items():
        mode_perf[m] = {
            "count": v["count"],
            "avg_processing_sec": _avg(v["total_sec"]),
            "avg_ai_ms": _avg(v["ai_ms"]),
            "avg_video_ms": _avg(v["video_ms"]),
        }

    all_sec = [r["processing_time_sec"] for r in metrics if r.get("processing_time_sec")]
    all_ai = [r["ai_processing_ms"] for r in metrics if r.get("ai_processing_ms")]
    all_video = [r["video_generation_ms"] for r in metrics if r.get("video_generation_ms")]

    return {
        "total_requests": len(metrics),
        "overall_avg_processing_sec": _avg(all_sec),
        "overall_avg_ai_ms": _avg(all_ai),
        "overall_avg_video_ms": _avg(all_video),
        "by_mode": mode_perf,
        "by_format": dict(by_format),
        "request_trend": _date_bins(metrics, "created_at", days),
    }
