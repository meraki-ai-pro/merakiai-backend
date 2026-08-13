from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID
from app.db.supabase import get_async_supabase

logger = logging.getLogger(__name__)


async def get_session_course_id(session_id: str) -> str:
    """Return the course_id stored on the session. Raises ValueError if missing."""
    supabase = await get_async_supabase()
    res = (
        await supabase.table("sessions")
        .select("course_id")
        .eq("id", session_id)
        .execute()
    )
    if not res.data:
        raise ValueError(f"Session {session_id!r} not found")
    course_id = res.data[0].get("course_id")
    if not course_id:
        raise ValueError(f"Session {session_id!r} has no course assigned")
    return course_id


async def get_course_info(course_id: str) -> Dict[str, Any]:
    """
    Return the full course metadata row needed at inference time:
      name, persona, domain_topics, difficulty_descriptors
    Raises ValueError if the course does not exist.
    """
    supabase = await get_async_supabase()
    res = (
        await supabase.table("courses")
        .select("id, name, persona, domain_topics, difficulty_descriptors")
        .eq("id", course_id)
        .execute()
    )
    if not res.data:
        raise ValueError(f"Course {course_id!r} not found")
    return res.data[0]


async def ensure_session(
    session_id: str, user_id: str, current_mode: str = "learn"
) -> None:
    """
    Ensure session exists in DB. If it doesn't exist, create it.
    ✅ FIX: Don't set prefers_video here - it will be set via /sessions/video endpoint
    """
    try:
        UUID(str(session_id))
    except (ValueError, TypeError):
        raise ValueError("Invalid session_id format. Expected UUID.")

    supabase = await get_async_supabase()
    res = (
        await supabase.table("sessions")
        .select("id,user_id")
        .eq("id", session_id)
        .execute()
    )

    if not res.data:
        # ✅ FIX: Removed prefers_video - let it use database default or be set separately
        await supabase.table("sessions").insert(
            {
                "id": session_id,
                "user_id": user_id,
                "current_mode": current_mode,
                # Removed: "prefers_video": False  # This was forcing it to False!
            }
        ).execute()
        return

    # Ownership check
    row = res.data[0]
    # ✅ FIXED: Changed user["id"] to user_id (the parameter name)
    if row.get("user_id") and row["user_id"] != user_id:
        raise PermissionError("Session does not belong to user")


async def get_session_prefers_video(session_id: str) -> bool:
    """Get prefers_video for a session. Returns False if session doesn't exist."""
    supabase = await get_async_supabase()
    res = await (
        supabase.table("sessions")
        .select("prefers_video")
        .eq("id", session_id)
        .execute()
    )
    if not res.data:
        return False
    return bool(res.data[0].get("prefers_video", False))


async def response_format_for_mode(session_id: str, mode: str) -> str:
    """
    Determine response format based on mode and session preferences.
    - review: always text
    - learn/application: video if prefers_video=true, else text
    """
    mode = (mode or "").lower().strip()
    if mode == "review":
        return "text"
    
    prefers_video = await get_session_prefers_video(session_id)
    # Optional: Add logging to help debug
    logger.debug("response_format_for_mode  session=%s  mode=%s  prefers_video=%s", session_id, mode, prefers_video)
    
    return "video" if prefers_video else "text"


async def load_memory(session_id: str, limit_pairs: int = 6) -> List[str]:
    """Load conversation history for context."""
    supabase = await get_async_supabase()
    history = await (
        supabase.table("conversations")
        .select("user_input, tutor_response")
        .eq("session_id", session_id)
        .order("created_at", desc=True)
        .limit(limit_pairs)
        .execute()
    )

    mem: List[str] = []
    for row in reversed(history.data):
        mem.append(f"user: {row['user_input']}")
        mem.append(f"assistant: {row['tutor_response']}")
    return mem


async def log_conversation(
    session_id: str,
    user_id: str,
    mode: str,
    user_input: str,
    tutor_response: str,
    response_format: str,
    video_url: Optional[str] = None,
    audio_url: Optional[str] = None,
    sources: Optional[list] = None,
    attachments: Optional[list] = None,
) -> None:
    """Log conversation turn to database.

    ``sources`` is stored as an immutable snapshot of what the answer was
    grounded in. Without it a reloaded conversation shows [1][2] markers as
    inert text — the citation UI degrades to noise on every refresh.
    """
    supabase = await get_async_supabase()
    payload = {
        "session_id": session_id,
        "user_id": user_id,
        "mode": mode,
        "user_input": user_input,
        "tutor_response": tutor_response,
        "response_format": response_format,
        "video_url": video_url,
        "audio_url": audio_url,
    }

    # Written on a retry rather than in the first insert so the turn is still
    # recorded on a database that predates
    # 007_add_conversation_sources_and_file_retention.sql. Losing the transcript
    # would be far worse than losing its citations.
    optional = {}
    if sources:
        optional["sources"] = sources
    if attachments:
        optional["attachments"] = attachments

    if not optional:
        await supabase.table("conversations").insert(payload).execute()
        return

    try:
        await supabase.table("conversations").insert({**payload, **optional}).execute()
    except Exception as exc:  # noqa: BLE001 — columns may not exist yet
        logger.warning(
            "Conversation insert rejected sources/attachments; storing the turn "
            "without them. Apply 007_add_conversation_sources_and_file_retention.sql: %s",
            exc,
        )
        await supabase.table("conversations").insert(payload).execute()