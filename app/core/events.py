"""The analytics event stream.

This is the backbone of the research dataset (Tech Spec §5). Every question
the pilot needs to answer — which files students actually rely on, whether
video watching correlates with learning gains, how often retrieval fails —
is a query over this table.

Two rules the design turns on:

1. **Emission never fails the thing it observes.** Every call is wrapped and
   swallowed. Losing an event costs a row in a dataset; raising would cost a
   student their answer.

2. **Event types are a closed set.** Free-text names drift ("video_watched",
   "videoWatched", "watch_video") and a research query that misses a third of
   its rows to a typo is worse than one that returns nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

# ── Closed vocabulary ───────────────────────────────────────────────────────
# Server-emitted: the backend is the only thing that can see these.
SESSION_STARTED: Final = "session.started"
TURN_COMPLETED: Final = "turn.completed"
CHUNKS_RETRIEVED: Final = "retrieval.chunks"
RETRIEVAL_EMPTY: Final = "retrieval.empty"
RETRIEVAL_WEAK: Final = "retrieval.weak"
QUERY_REWRITTEN: Final = "retrieval.rewritten"
ENROLMENT_CREATED: Final = "enrolment.created"
ENROLMENT_CHANGED: Final = "enrolment.changed"
ASSESSMENT_SUBMITTED: Final = "assessment.submitted"
MASTERY_UPDATED: Final = "mastery.updated"

# Client-emitted: only the browser knows these happened.
CITATION_CLICKED: Final = "citation.clicked"
SOURCE_DRAWER_OPENED: Final = "source.drawer_opened"
BOARD_SLIDE_VIEWED: Final = "board.slide_viewed"
NARRATION_PLAYED: Final = "board.narration_played"
VIDEO_PROGRESS: Final = "video.progress"
VIDEO_COMPLETED: Final = "video.completed"

SERVER_EVENTS: Final = frozenset({
    SESSION_STARTED, TURN_COMPLETED, CHUNKS_RETRIEVED, RETRIEVAL_EMPTY,
    RETRIEVAL_WEAK, QUERY_REWRITTEN,
    ENROLMENT_CREATED, ENROLMENT_CHANGED, ASSESSMENT_SUBMITTED, MASTERY_UPDATED,
})

# The only names POST /events will accept. A client cannot forge a
# server-authoritative event like assessment.submitted and corrupt the study.
CLIENT_EVENTS: Final = frozenset({
    CITATION_CLICKED, SOURCE_DRAWER_OPENED, BOARD_SLIDE_VIEWED,
    NARRATION_PLAYED, VIDEO_PROGRESS, VIDEO_COMPLETED,
})

ALL_EVENTS: Final = SERVER_EVENTS | CLIENT_EVENTS

# Payloads are small by design. A stray transcript in here would put student
# text in the analytics table, outside the retention rules that govern
# conversations.
_MAX_PAYLOAD_KEYS = 20
_MAX_STRING_LEN = 500


def _clean(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    out: dict[str, Any] = {}
    for key, value in list(payload.items())[:_MAX_PAYLOAD_KEYS]:
        if isinstance(value, str):
            value = value[:_MAX_STRING_LEN]
        elif isinstance(value, (list, dict)):
            # Nested structures are summarised, not stored: they are where
            # transcripts and chunk text would sneak in.
            value = len(value)
        elif not isinstance(value, (int, float, bool, type(None))):
            value = str(value)[:_MAX_STRING_LEN]
        out[str(key)[:60]] = value
    return out


def emit(
    event_type: str,
    *,
    user_id: str | None = None,
    course_id: str | None = None,
    topic: str | None = None,
    session_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Record one event. Never raises, never blocks the caller meaningfully."""
    if event_type not in ALL_EVENTS:
        # Loud in the log, silent to the caller: an unknown type is a bug in
        # our code, not something a user should ever see.
        logger.warning("Refusing to emit unknown event type %r", event_type)
        return

    try:
        get_supabase().table("events").insert({
            "user_id": user_id,
            "event_type": event_type,
            "course_id": course_id,
            "topic": topic,
            "session_id": session_id,
            "payload": _clean(payload),
        }).execute()
    except Exception as exc:  # noqa: BLE001 — see module docstring
        logger.warning("Event %s dropped: %s", event_type, exc)


async def emit_async(event_type: str, **kwargs: Any) -> None:
    """Emit off the event loop, for use inside async request handlers."""
    import asyncio

    await asyncio.to_thread(emit, event_type, **kwargs)
