from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials

from app.core.auth import _validate_token
from app.core.websocket_manager import manager
from app.core import analytics
from app.db.supabase import get_supabase, get_async_supabase
from app.ai.tasks import (
    process_rag_turn_task,
    process_mode_session_start_task,
    process_mode_session_turn_task,
)

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

                task = process_rag_turn_task.apply_async(
                    args=[session_id, user_id, message, "learn"],
                    queue=queue,
                    priority=priority,
                )
                await websocket.send_json({"status": "processing", "task_id": task.id})

            # ---------------------------------------------------------------
            # mode_session_start — begin a Practice or Review session
            # ---------------------------------------------------------------
            elif msg_type == "mode_session_start":
                mode         = (data.get("mode") or "").lower().strip()
                session_type = (data.get("session_type") or "").lower().strip()
                difficulty   = (data.get("difficulty") or "Basic").strip().title()
                total_items  = int(data.get("total_items") or 10)

                if mode not in ("application", "review"):
                    await websocket.send_json({"error": "mode must be 'application' or 'review'"})
                    continue

                supabase = get_supabase()
                ms = supabase.table("mode_sessions").insert({
                    "session_id":   session_id,
                    "user_id":      user_id,
                    "mode":         mode,
                    "session_type": session_type,
                    "difficulty":   difficulty,
                    "total_items":  total_items if mode == "review" else 3,
                    "current_item": 1,
                    "completed":    False,
                    "started_at":   _now_iso(),
                }).execute()

                if not ms.data:
                    await websocket.send_json({"error": "Failed to create mode session"})
                    continue

                mode_session_id: str = ms.data[0]["id"]

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

                supabase = get_supabase()

                ms_res = (
                    supabase.table("mode_sessions")
                    .select("*")
                    .eq("id", mode_session_id)
                    .execute()
                )
                if not ms_res.data:
                    await websocket.send_json({"error": "Mode session not found"})
                    continue

                ms = ms_res.data[0]
                if ms["user_id"] != user_id:
                    await websocket.send_json({"error": "Not allowed"})
                    continue

                st_res = (
                    supabase.table("session_state")
                    .select("*")
                    .eq("mode_session_id", mode_session_id)
                    .execute()
                )
                if not st_res.data:
                    await websocket.send_json(
                        {"error": "No active state for this mode session"}
                    )
                    continue

                state = st_res.data[0]

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

                supabase = get_supabase()
                ms_res = (
                    supabase.table("mode_sessions")
                    .select("user_id, completed")
                    .eq("id", mode_session_id)
                    .execute()
                )
                if not ms_res.data:
                    await websocket.send_json({"error": "Mode session not found"})
                    continue

                if ms_res.data[0]["user_id"] != user_id:
                    await websocket.send_json({"error": "Not allowed"})
                    continue

                supabase.table("mode_sessions").update({
                    "completed": True,
                    "ended_at":  _now_iso(),
                }).eq("id", mode_session_id).execute()

                supabase.table("session_state").delete().eq(
                    "mode_session_id", mode_session_id
                ).execute()

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
        # Close WS session analytics — computes active usage duration
        if ws_session_id:
            await asyncio.to_thread(analytics.close_ws_session, ws_session_id)
        await manager.disconnect(session_id)
