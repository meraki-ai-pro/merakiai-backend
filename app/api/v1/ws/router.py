from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials

from app.core.auth import _validate_token
from app.core.websocket_manager import manager
from app.core import analytics
from app.db.supabase import get_user_client, get_async_supabase
from app.ai.tasks import (
    process_rag_turn_task,
    process_mode_session_start_task,
    process_mode_session_turn_task,
)
# The same two rules the HTTP route enforces. Both paths start mode sessions,
# and validation in only one of them is validation in neither.
from app.ai.rag.modes_sessions.router import REVIEW_QUESTION_FORMATS
from app.ai.rag.modes_sessions.service import GENERAL_SCENARIO

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws", tags=["WebSocket"])

# ---------------------------------------------------------------------------
# Priority constants
#   text_tasks  → priority 9 (high)  — target ≤200ms infrastructure delivery
#   video_tasks → priority 3 (low)   — target ≤2s   infrastructure delivery
# ---------------------------------------------------------------------------
_TEXT_QUEUE     = "text_tasks"
_VIDEO_QUEUE    = "video_tasks"
_TEXT_PRIORITY  = 9
_VIDEO_PRIORITY = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Per-session WebSocket message rate limiter (M-2)
# Sliding-window, in-memory. Cleaned up on WS disconnect.
# ---------------------------------------------------------------------------
_WS_MAX_MSGS_PER_MINUTE = 20
_ws_msg_timestamps: dict[str, list[float]] = defaultdict(list)


def _ws_rate_check(session_id: str) -> bool:
    """Return True if the message is allowed, False if rate-limited."""
    now = time.monotonic()
    cutoff = now - 60.0
    ts = [t for t in _ws_msg_timestamps[session_id] if t > cutoff]
    _ws_msg_timestamps[session_id] = ts
    if len(ts) >= _WS_MAX_MSGS_PER_MINUTE:
        return False
    _ws_msg_timestamps[session_id].append(now)
    return True


def _ws_rate_cleanup(session_id: str) -> None:
    """Remove rate-limit state when the WebSocket disconnects."""
    _ws_msg_timestamps.pop(session_id, None)


# ---------------------------------------------------------------------------
# Sync DB helpers — called via asyncio.to_thread so the event loop is never
# blocked. Each uses get_user_client(token) so Postgres RLS is enforced at
# the DB level under the authenticated user's identity (H-1).
# ---------------------------------------------------------------------------

def _db_create_mode_session(token: str, payload: dict) -> dict | None:
    """Insert a new mode_session row as the authenticated user."""
    res = get_user_client(token).table("mode_sessions").insert(payload).execute()
    return res.data[0] if res.data else None


def _db_fetch_mode_session_and_state(
    token: str, mode_session_id: str
) -> tuple[dict | None, dict | None]:
    """Fetch mode_session + session_state rows owned by the authenticated user.

    RLS ensures rows from other users are invisible — no manual user_id check
    needed, though we keep it below as defence-in-depth.
    """
    sb = get_user_client(token)
    ms_res = sb.table("mode_sessions").select("*").eq("id", mode_session_id).execute()
    if not ms_res.data:
        return None, None
    st_res = (
        sb.table("session_state")
        .select("*")
        .eq("mode_session_id", mode_session_id)
        .execute()
    )
    return ms_res.data[0], (st_res.data[0] if st_res.data else None)


def _db_end_mode_session(token: str, mode_session_id: str) -> dict | None:
    """Mark a mode_session as completed and delete its transient state."""
    sb = get_user_client(token)
    ms_res = (
        sb.table("mode_sessions")
        .select("user_id, completed")
        .eq("id", mode_session_id)
        .execute()
    )
    if not ms_res.data:
        return None
    sb.table("mode_sessions").update({
        "completed": True,
        "ended_at": _now_iso(),
    }).eq("id", mode_session_id).execute()
    sb.table("session_state").delete().eq("mode_session_id", mode_session_id).execute()
    return ms_res.data[0]


async def _session_prefers_video(session_id: str) -> bool:
    supabase = await get_async_supabase()
    res = await (
        supabase.table("sessions")
        .select("prefers_video")
        .eq("id", session_id)
        .execute()
    )
    return bool(res.data[0].get("prefers_video", False)) if res.data else False


# ---------------------------------------------------------------------------
# WebSocket endpoint
#
#   ws://<host>/ws/<session_id>?token=<supabase_jwt>
#
# Supported message types:
#   { "type": "rag_turn",            "message": "..." }
#   { "type": "mode_session_start",  "mode": "...", "session_type": "...",
#                                    "difficulty": "...", "total_items": N }
#   { "type": "mode_session_turn",   "mode_session_id": "...", "message": "..." }
#   { "type": "mode_session_end",    "mode_session_id": "..." }
# ---------------------------------------------------------------------------

@router.websocket("/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    session_id: str,
    token: str = Query(..., description="Supabase JWT auth token"),
):
    # --- Authentication ---------------------------------------------------
    try:
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        user = _validate_token(creds)
        user_id: str = user.id
    except Exception:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    # --- Open connection, Redis listener, and WS session analytics --------
    await manager.connect(session_id, websocket)
    ws_session_id = await asyncio.to_thread(
        analytics.open_ws_session, user_id, session_id
    )
    logger.info("WS open  session=%s  user=%s", session_id, user_id)

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                data: dict = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"error": "Invalid JSON"})
                continue

            # M-2: per-session sliding-window rate limit
            if not _ws_rate_check(session_id):
                await websocket.send_json({
                    "error": f"Rate limit exceeded. Max {_WS_MAX_MSGS_PER_MINUTE} messages per minute."
                })
                continue

            msg_type: str = data.get("type", "")

            # ---------------------------------------------------------------
            # rag_turn — Learn mode question (text or video response)
            # ---------------------------------------------------------------
            if msg_type == "rag_turn":
                message = (data.get("message") or "").strip()
                if not message:
                    await websocket.send_json({"error": "message cannot be empty"})
                    continue

                prefers_video = await _session_prefers_video(session_id)
                queue    = _VIDEO_QUEUE    if prefers_video else _TEXT_QUEUE
                priority = _VIDEO_PRIORITY if prefers_video else _TEXT_PRIORITY

                # M-17: pass prefers_video to avoid a duplicate DB lookup inside the task
                task = process_rag_turn_task.apply_async(
                    args=[session_id, user_id, message, "learn", prefers_video],
                    queue=queue,
                    priority=priority,
                )
                await websocket.send_json({"status": "processing", "task_id": task.id})

            # ---------------------------------------------------------------
            # mode_session_start — begin an Assessment or Review session
            # ---------------------------------------------------------------
            elif msg_type == "mode_session_start":
                mode         = (data.get("mode") or "").lower().strip()
                session_type = (data.get("session_type") or "").lower().strip()
                difficulty   = (data.get("difficulty") or "Basic").strip().title()
                # M-9: cap total_items to prevent a single user spawning unlimited AI calls
                total_items  = min(int(data.get("total_items") or 10), 50)

                if mode not in ("application", "review"):
                    await websocket.send_json({"error": "mode must be 'application' or 'review'"})
                    continue

                if mode == "review":
                    # The generator branches on this. Defaulting silently would
                    # hand a student MCQs when they picked short answer.
                    if session_type not in REVIEW_QUESTION_FORMATS:
                        await websocket.send_json({
                            "error": (
                                "session_type must be one of "
                                f"{', '.join(sorted(REVIEW_QUESTION_FORMATS))} for review"
                            )
                        })
                        continue
                else:
                    # Assessment has no topic picker any more, so the client
                    # sends nothing and the server supplies the placeholder —
                    # mode_sessions.session_type is NOT NULL.
                    session_type = session_type or GENERAL_SCENARIO

                ms_row = await asyncio.to_thread(
                    _db_create_mode_session,
                    token,
                    {
                        "session_id":   session_id,
                        "user_id":      user_id,
                        "mode":         mode,
                        "session_type": session_type,
                        "difficulty":   difficulty,
                        "total_items":  total_items if mode == "review" else 3,
                        "current_item": 1,
                        "completed":    False,
                        "started_at":   _now_iso(),
                    },
                )

                if not ms_row:
                    await websocket.send_json({"error": "Failed to create mode session"})
                    continue

                mode_session_id: str = ms_row["id"]

                task = process_mode_session_start_task.apply_async(
                    args=[session_id, user_id, mode, session_type, difficulty, mode_session_id],
                    queue=_TEXT_QUEUE,
                    priority=_TEXT_PRIORITY,
                )
                await websocket.send_json({
                    "status":          "processing",
                    "task_id":         task.id,
                    "mode_session_id": mode_session_id,
                })

            # ---------------------------------------------------------------
            # mode_session_turn — answer a question in an active session
            # ---------------------------------------------------------------
            elif msg_type == "mode_session_turn":
                mode_session_id = data.get("mode_session_id", "").strip()
                student_answer  = (data.get("message") or "").strip()

                if not mode_session_id or not student_answer:
                    await websocket.send_json(
                        {"error": "mode_session_id and message are required"}
                    )
                    continue

                ms, state = await asyncio.to_thread(
                    _db_fetch_mode_session_and_state, token, mode_session_id
                )

                if not ms:
                    await websocket.send_json({"error": "Mode session not found"})
                    continue

                # Defence-in-depth: RLS already filters by owner; double-check here.
                if ms["user_id"] != user_id:
                    await websocket.send_json({"error": "Not allowed"})
                    continue

                if not state:
                    await websocket.send_json(
                        {"error": "No active state for this mode session"}
                    )
                    continue

                task = process_mode_session_turn_task.apply_async(
                    args=[
                        mode_session_id,
                        ms["session_id"],
                        user_id,
                        ms["mode"],
                        ms["session_type"],
                        student_answer,
                        state["pending_payload"],
                        state.get("contexts") or [],
                        int(state.get("step", 1)),
                        int(state.get("total_steps", 1)),
                        ms["difficulty"],
                        int(ms.get("current_item") or 1),
                        int(ms.get("total_items") or 10),
                    ],
                    queue=_TEXT_QUEUE,
                    priority=_TEXT_PRIORITY,
                )
                await websocket.send_json({"status": "processing", "task_id": task.id})

            # ---------------------------------------------------------------
            # mode_session_end — explicitly end a application/review session
            # ---------------------------------------------------------------
            elif msg_type == "mode_session_end":
                mode_session_id = data.get("mode_session_id", "").strip()
                if not mode_session_id:
                    await websocket.send_json({"error": "mode_session_id is required"})
                    continue

                ms_end = await asyncio.to_thread(
                    _db_end_mode_session, token, mode_session_id
                )

                if not ms_end:
                    await websocket.send_json({"error": "Mode session not found"})
                    continue

                # Defence-in-depth ownership check (RLS already enforces this).
                if ms_end["user_id"] != user_id:
                    await websocket.send_json({"error": "Not allowed"})
                    continue

                # Compute and store mode session duration
                await asyncio.to_thread(analytics.close_mode_session, mode_session_id)

                await websocket.send_json({
                    "status":          "ended",
                    "mode_session_id": mode_session_id,
                })

            else:
                await websocket.send_json({"error": f"Unknown message type: '{msg_type}'"})

    except WebSocketDisconnect:
        logger.info("WS disconnected  session=%s", session_id)
    except Exception as exc:
        logger.error("WS error  session=%s  error=%s", session_id, exc)
    finally:
        _ws_rate_cleanup(session_id)  # M-2: release in-memory rate-limit state
        if ws_session_id:
            await asyncio.to_thread(analytics.close_ws_session, ws_session_id)
        await manager.disconnect(session_id)
