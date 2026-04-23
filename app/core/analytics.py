"""
analytics.py — Centralised helpers for capturing platform metrics.

All functions are synchronous so they can be called directly from Celery
tasks. In async contexts (FastAPI routes, WebSocket handlers) wrap with
asyncio.to_thread():

    await asyncio.to_thread(analytics.log_login, user_id)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Login tracking
# ---------------------------------------------------------------------------

def log_login(user_id: str) -> None:
    """
    Record one login event for the user.
    Login count = SELECT COUNT(*) FROM platform_sessions WHERE user_id = ?
    """
    try:
        get_supabase().table("platform_sessions").insert(
            {"user_id": user_id, "login_at": _now_iso()}
        ).execute()
    except Exception as exc:
        logger.warning("analytics.log_login  user=%s  error=%s", user_id, exc)


# ---------------------------------------------------------------------------
# WebSocket / active-usage session tracking
# ---------------------------------------------------------------------------

def open_ws_session(user_id: str, session_id: str) -> str | None:
    """
    Record the start of an active WebSocket session.
    Returns the ws_session id, which must be passed to close_ws_session
    on disconnect.

    Total platform time = SELECT SUM(duration_sec) FROM ws_sessions WHERE user_id = ?
    """
    try:
        res = (
            get_supabase()
            .table("ws_sessions")
            .insert({
                "user_id":      user_id,
                "session_id":   session_id,
                "connected_at": _now_iso(),
            })
            .execute()
        )
        return res.data[0]["id"] if res.data else None
    except Exception as exc:
        logger.warning(
            "analytics.open_ws_session  user=%s  session=%s  error=%s",
            user_id, session_id, exc,
        )
        return None


def close_ws_session(ws_session_id: str) -> None:
    """
    Record the end of a WebSocket session and compute active duration.
    Called on WebSocket disconnect.
    """
    try:
        supabase = get_supabase()
        res = (
            supabase.table("ws_sessions")
            .select("connected_at")
            .eq("id", ws_session_id)
            .execute()
        )
        if not res.data:
            return

        raw = res.data[0]["connected_at"]
        # Supabase timestamps may carry timezone offset or end with 'Z'
        connected_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        duration_sec = max(0, int((now - connected_at).total_seconds()))

        supabase.table("ws_sessions").update({
            "disconnected_at": now.isoformat(),
            "duration_sec":    duration_sec,
        }).eq("id", ws_session_id).execute()

    except Exception as exc:
        logger.warning(
            "analytics.close_ws_session  ws_session=%s  error=%s",
            ws_session_id, exc,
        )


# ---------------------------------------------------------------------------
# Request / response timing
# ---------------------------------------------------------------------------

def log_request_metric(
    *,
    session_id: str,
    user_id: str,
    mode: str,
    task_type: str,
    response_format: str,
    total_processing_ms: int,
    ai_processing_ms: int | None = None,
    video_generation_ms: int | None = None,
) -> None:
    """
    Log a single AI request with full timing breakdown.

    Columns stored:
        processing_time_sec   — total wall-clock seconds (legacy + kept for compat)
        ai_processing_ms      — time spent calling the AI model only
        video_generation_ms   — time spent on TTS + upload + D-ID/Tavus (video only)
        response_format       — 'text' | 'video'
        mode                  — 'learn' | 'review' | 'application'
    """
    try:
        get_supabase().table("request_metrics").insert({
            "session_id":          session_id,
            "user_id":             user_id,
            "mode":                mode,
            "task_type":           task_type,
            "response_format":     response_format,
            "processing_time_sec": round(total_processing_ms / 1000, 3),
            "ai_processing_ms":    ai_processing_ms,
            "video_generation_ms": video_generation_ms,
        }).execute()
    except Exception as exc:
        logger.warning(
            "analytics.log_request_metric  session=%s  error=%s",
            session_id, exc,
        )


# ---------------------------------------------------------------------------
# Mode session duration
# ---------------------------------------------------------------------------

def close_mode_session(mode_session_id: str) -> None:
    """
    Compute and store the duration of a application/review mode session.
    Called when the session is ended (by user or auto-completion).

    Time per mode per user:
        SELECT user_id, mode, SUM(duration_sec)
        FROM mode_sessions
        WHERE duration_sec IS NOT NULL
        GROUP BY user_id, mode
    """
    try:
        supabase = get_supabase()
        res = (
            supabase.table("mode_sessions")
            .select("started_at")
            .eq("id", mode_session_id)
            .execute()
        )
        if not res.data:
            return

        raw = res.data[0]["started_at"]
        started_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        duration_sec = max(0, int((now - started_at).total_seconds()))

        supabase.table("mode_sessions").update({
            "ended_at":    now.isoformat(),
            "duration_sec": duration_sec,
        }).eq("id", mode_session_id).execute()

    except Exception as exc:
        logger.warning(
            "analytics.close_mode_session  mode_session=%s  error=%s",
            mode_session_id, exc,
        )
