from __future__ import annotations

import asyncio
import time as _time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, Query

from app.core.auth import admin_guard
from app.db.supabase import get_supabase

router = APIRouter(prefix="/analytics", tags=["Admin – Analytics"])


# ---------------------------------------------------------------------------
# L-11: Simple in-memory TTL cache (60 s) for all analytics endpoints.
# Admin analytics are expensive and don't need sub-second freshness.
# ---------------------------------------------------------------------------
_CACHE_TTL_SEC = 60
_CACHE: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> tuple[bool, Any]:
    if key in _CACHE:
        ts, val = _CACHE[key]
        if _time.monotonic() - ts < _CACHE_TTL_SEC:
            return True, val
    return False, None


def _cache_set(key: str, val: Any) -> None:
    _CACHE[key] = (_time.monotonic(), val)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# M-13: each helper runs get_supabase() inside the thread so the thread-local
# client is always used correctly — never shared across threads.

async def _t(fn):
    """Run a sync supabase query in a thread pool."""
    return await asyncio.to_thread(fn)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@router.get("/overview")
async def analytics_overview(_user=Depends(admin_guard)):
    """High-level platform snapshot for the admin dashboard."""
    # L-11: serve from cache when fresh
    cache_key = "overview"
    hit, cached = _cache_get(cache_key)
    if hit:
        return cached

    cutoff_30  = _cutoff_iso(30)
    cutoff_90  = _cutoff_iso(90)

    # M-13: run all independent queries concurrently
    (
        total_users,
        total_sessions,
        total_conversations,
        total_reviews,
        survey_count,
        user_feedback_count,
        mode_feedback_count,
        logins_30d_data,
        surveys_data,
        summaries_data,
        sessions_completed,
        ws_data,
    ) = await asyncio.gather(
        _t(lambda: (get_supabase().table("users").select("id", count="exact").limit(0).execute()).count or 0),
        _t(lambda: (get_supabase().table("sessions").select("id", count="exact").limit(0).execute()).count or 0),
        _t(lambda: (get_supabase().table("conversations").select("id", count="exact").limit(0).execute()).count or 0),
        _t(lambda: (get_supabase().table("review_summaries").select("id", count="exact").limit(0).execute()).count or 0),
        _t(lambda: (get_supabase().table("session_surveys").select("id", count="exact").limit(0).execute()).count or 0),
        _t(lambda: (get_supabase().table("user_feedback").select("id", count="exact").limit(0).execute()).count or 0),
        _t(lambda: (get_supabase().table("mode_feedback").select("id", count="exact").limit(0).execute()).count or 0),
        _t(lambda: (get_supabase().table("platform_sessions").select("user_id").gte("login_at", cutoff_30).limit(10000).execute()).data or []),
        _t(lambda: (get_supabase().table("session_surveys").select("overall_rating").gte("created_at", cutoff_90).limit(5000).execute()).data or []),
        _t(lambda: (get_supabase().table("review_summaries").select("overall_score").gte("generated_at", cutoff_90).limit(5000).execute()).data or []),
        _t(lambda: (get_supabase().table("sessions").select("id", count="exact").not_.is_("ended_at", "null").limit(0).execute()).count or 0),
        _t(lambda: (get_supabase().table("ws_sessions").select("duration_sec").gte("connected_at", cutoff_90).not_.is_("duration_sec", "null").limit(10000).execute()).data or []),
    )

    active_users_30d     = len({r["user_id"] for r in logins_30d_data})
    avg_overall_rating   = _avg([r["overall_rating"] for r in surveys_data])
    avg_review_score     = _avg([r["overall_score"]  for r in summaries_data])
    total_platform_min   = round(sum(r["duration_sec"] for r in ws_data) / 60, 1)

    result = {
        "total_users":             total_users,
        "active_users_30d":        active_users_30d,
        "total_sessions":          total_sessions,
        "sessions_completed":      sessions_completed,
        "total_conversations":     total_conversations,
        "total_reviews_completed": total_reviews,
        "avg_overall_rating":      avg_overall_rating,
        "avg_review_score":        avg_review_score,
        "total_platform_minutes":  total_platform_min,
        "survey_count":            survey_count,
        "user_feedback_count":     user_feedback_count,
        "mode_feedback_count":     mode_feedback_count,
    }
    _cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# User growth & activity
# ---------------------------------------------------------------------------

@router.get("/users")
async def analytics_users(days: int = Query(30, ge=1, le=365), _user=Depends(admin_guard)):
    cache_key = f"users:{days}"
    hit, cached = _cache_get(cache_key)
    if hit:
        return cached

    cutoff = _cutoff_iso(days)

    # M-13: parallel queries
    all_users, logins = await asyncio.gather(
        _t(lambda: (get_supabase().table("users").select("role, country, region, university_name, created_at").limit(50000).execute()).data or []),
        _t(lambda: (get_supabase().table("platform_sessions").select("user_id, login_at").gte("login_at", cutoff).limit(10000).execute()).data or []),
    )

    by_role        = dict(Counter(u["role"] for u in all_users))
    by_country     = dict(Counter(u.get("country") or "Unknown" for u in all_users))
    by_university  = dict(Counter(u.get("university_name") or "Unknown" for u in all_users))

    new_users    = [u for u in all_users if (u.get("created_at") or "") >= cutoff]
    signup_trend = _date_bins(new_users, "created_at", days)
    login_trend  = _date_bins(logins, "login_at", days)
    unique_active = len({r["user_id"] for r in logins})

    result = {
        "total_users":          len(all_users),
        "active_users_in_period": unique_active,
        "by_role":              by_role,
        "by_country":           by_country,
        "by_university":        by_university,
        "signup_trend":         signup_trend,
        "login_trend":          login_trend,
    }
    _cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Session activity
# ---------------------------------------------------------------------------

@router.get("/sessions")
async def analytics_sessions(days: int = Query(30, ge=1, le=365), _user=Depends(admin_guard)):
    cache_key = f"sessions:{days}"
    hit, cached = _cache_get(cache_key)
    if hit:
        return cached

    cutoff = _cutoff_iso(days)

    # M-13: parallel queries
    sessions, mode_sessions, ws = await asyncio.gather(
        _t(lambda: (get_supabase().table("sessions").select("id, current_mode, course_id, started_at, ended_at").gte("started_at", cutoff).execute()).data or []),
        _t(lambda: (get_supabase().table("mode_sessions").select("mode, completed, duration_sec, difficulty, message_count").gte("started_at", cutoff).execute()).data or []),
        _t(lambda: (get_supabase().table("ws_sessions").select("duration_sec").gte("connected_at", cutoff).not_.is_("duration_sec", "null").execute()).data or []),
    )

    by_mode        = dict(Counter(s["current_mode"] for s in sessions))
    by_course      = dict(Counter(s.get("course_id") or "none" for s in sessions))
    session_trend  = _date_bins(sessions, "started_at", days)

    durations      = [r["duration_sec"] for r in ws if r["duration_sec"]]
    avg_session_min = round(sum(durations) / len(durations) / 60, 2) if durations else None

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
        d, msgs = stats["durations"], stats["messages"]
        mode_breakdown[m] = {
            "total":            stats["count"],
            "completed":        stats["completed"],
            "completion_rate":  round(stats["completed"] / stats["count"], 2) if stats["count"] else None,
            "avg_duration_min": round(sum(d) / len(d) / 60, 2) if d else None,
            "avg_messages":     round(sum(msgs) / len(msgs), 1) if msgs else None,
        }

    result = {
        "total_sessions":          len(sessions),
        "by_mode":                 by_mode,
        "by_course":               by_course,
        "avg_session_duration_min": avg_session_min,
        "session_trend":           session_trend,
        "mode_breakdown":          mode_breakdown,
    }
    _cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

@router.get("/feedback")
async def analytics_feedback(days: int = Query(90, ge=1, le=365), _user=Depends(admin_guard)):
    cache_key = f"feedback:{days}"
    hit, cached = _cache_get(cache_key)
    if hit:
        return cached

    cutoff = _cutoff_iso(days)

    # M-13: parallel queries
    surveys, mf_rows, uf_rows, quality_flags = await asyncio.gather(
        _t(lambda: (get_supabase().table("session_surveys").select("clarity_rating, helpfulness_rating, confidence_rating, overall_rating, created_at").gte("created_at", cutoff).execute()).data or []),
        _t(lambda: (get_supabase().table("mode_feedback").select("mode, ease_of_understanding, engagement_level, usefulness, created_at").gte("created_at", cutoff).execute()).data or []),
        _t(lambda: (get_supabase().table("user_feedback").select("id, feedback_type, message, created_at").gte("created_at", cutoff).order("created_at", desc=True).execute()).data or []),
        _t(lambda: (get_supabase().table("content_quality_flags").select("*").order("detected_on", desc=True).limit(50).execute()).data or []),
    )

    survey_stats = {
        "count":            len(surveys),
        "avg_clarity":      _avg([s["clarity_rating"]      for s in surveys]),
        "avg_helpfulness":  _avg([s["helpfulness_rating"]   for s in surveys]),
        "avg_confidence":   _avg([s["confidence_rating"]    for s in surveys]),
        "avg_overall":      _avg([s["overall_rating"]       for s in surveys]),
    }

    mf_by_mode: Dict[str, Any] = defaultdict(lambda: {"ease": [], "engagement": [], "usefulness": []})
    for r in mf_rows:
        m = r["mode"]
        if r.get("ease_of_understanding"):   mf_by_mode[m]["ease"].append(r["ease_of_understanding"])
        if r.get("engagement_level"):         mf_by_mode[m]["engagement"].append(r["engagement_level"])
        if r.get("usefulness"):               mf_by_mode[m]["usefulness"].append(r["usefulness"])

    mode_feedback_stats = {
        m: {
            "count":           len(v["ease"]) or len(v["engagement"]) or len(v["usefulness"]),
            "avg_ease":        _avg(v["ease"]),
            "avg_engagement":  _avg(v["engagement"]),
            "avg_usefulness":  _avg(v["usefulness"]),
        }
        for m, v in mf_by_mode.items()
    }

    by_type         = dict(Counter(r["feedback_type"] for r in uf_rows))
    recent_feedback = uf_rows[:20]

    result = {
        "session_surveys":       survey_stats,
        "mode_feedback":         mode_feedback_stats,
        "user_feedback": {
            "total":    len(uf_rows),
            "by_type":  by_type,
            "recent":   recent_feedback,
        },
        "content_quality_flags": quality_flags,
    }
    _cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Learning outcomes
# ---------------------------------------------------------------------------

@router.get("/learning-outcomes")
async def analytics_learning_outcomes(days: int = Query(30, ge=1, le=365), _user=Depends(admin_guard)):
    cache_key = f"learning-outcomes:{days}"
    hit, cached = _cache_get(cache_key)
    if hit:
        return cached

    cutoff = _cutoff_iso(days)

    # M-13: parallel queries
    summaries, attempts, surveys = await asyncio.gather(
        _t(lambda: (get_supabase().table("review_summaries").select("overall_score, generated_at").gte("generated_at", cutoff).execute()).data or []),
        _t(lambda: (get_supabase().table("review_attempts").select("verdict, score, created_at").gte("created_at", cutoff).execute()).data or []),
        _t(lambda: (get_supabase().table("session_surveys").select("confidence_rating, created_at").gte("created_at", cutoff).execute()).data or []),
    )

    verdict_dist   = dict(Counter(a["verdict"] for a in attempts))
    total_attempts = len(attempts)
    correct        = verdict_dist.get("correct", 0) + verdict_dist.get("partially_correct", 0)
    pass_rate      = round(correct / total_attempts, 3) if total_attempts else None

    result = {
        "total_reviews":       len(summaries),
        "total_attempts":      total_attempts,
        "avg_review_score":    _avg([s["overall_score"]     for s in summaries]),
        "avg_attempt_score":   _avg([a["score"]             for a in attempts]),
        "avg_confidence_rating": _avg([s["confidence_rating"] for s in surveys]),
        "pass_rate":           pass_rate,
        "verdict_distribution": verdict_dist,
        "score_trend":         _date_bins(summaries, "generated_at", days),
    }
    _cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# AI / system performance
# ---------------------------------------------------------------------------

@router.get("/performance")
async def analytics_performance(days: int = Query(30, ge=1, le=365), _user=Depends(admin_guard)):
    cache_key = f"performance:{days}"
    hit, cached = _cache_get(cache_key)
    if hit:
        return cached

    cutoff = _cutoff_iso(days)

    metrics = await _t(
        lambda: (get_supabase().table("request_metrics").select(
            "mode, response_format, processing_time_sec, ai_processing_ms, video_generation_ms, created_at"
        ).gte("created_at", cutoff).execute()).data or []
    )

    by_mode: Dict[str, Any] = defaultdict(lambda: {"count": 0, "total_sec": [], "ai_ms": [], "video_ms": []})
    by_format: Dict[str, int] = Counter()

    for r in metrics:
        m   = r.get("mode") or "unknown"
        fmt = r.get("response_format") or "text"
        by_mode[m]["count"] += 1
        if r.get("processing_time_sec"):  by_mode[m]["total_sec"].append(r["processing_time_sec"])
        if r.get("ai_processing_ms"):     by_mode[m]["ai_ms"].append(r["ai_processing_ms"])
        if r.get("video_generation_ms"):  by_mode[m]["video_ms"].append(r["video_generation_ms"])
        by_format[fmt] += 1

    mode_perf = {
        m: {
            "count":               v["count"],
            "avg_processing_sec":  _avg(v["total_sec"]),
            "avg_ai_ms":           _avg(v["ai_ms"]),
            "avg_video_ms":        _avg(v["video_ms"]),
        }
        for m, v in by_mode.items()
    }

    all_sec   = [r["processing_time_sec"]  for r in metrics if r.get("processing_time_sec")]
    all_ai    = [r["ai_processing_ms"]     for r in metrics if r.get("ai_processing_ms")]
    all_video = [r["video_generation_ms"]  for r in metrics if r.get("video_generation_ms")]

    result = {
        "total_requests":            len(metrics),
        "overall_avg_processing_sec": _avg(all_sec),
        "overall_avg_ai_ms":         _avg(all_ai),
        "overall_avg_video_ms":      _avg(all_video),
        "by_mode":                   mode_perf,
        "by_format":                 dict(by_format),
        "request_trend":             _date_bins(metrics, "created_at", days),
    }
    _cache_set(cache_key, result)
    return result
