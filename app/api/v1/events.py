"""Client-emitted analytics events.

Only the browser knows a citation was clicked or a video reached 80%. This is
the one place those enter the stream, and it is deliberately narrow:

  * only names in CLIENT_EVENTS are accepted, so a caller cannot forge
    assessment.submitted or mastery.updated and corrupt the study;
  * user_id is taken from the JWT, never from the body, so nobody can attribute
    engagement to another student;
  * enrolment is checked, so events cannot be manufactured for a course the
    caller has nothing to do with.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core import events
from app.core.auth import auth_guard
from app.core.enrolment import require_enrolment
from app.core.rate_limit import rate_limit

router = APIRouter(prefix="/events", tags=["Analytics"])


class EventIn(BaseModel):
    event_type: str = Field(..., max_length=60)
    course_id: str = Field(..., max_length=100)
    topic: str | None = Field(None, max_length=200)
    session_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("")
async def record_event(
    body: EventIn,
    user=Depends(auth_guard),
    # Generous, because slide-viewed and video-progress fire often, but bounded
    # so a loop in the client cannot flood the research table.
    _rl=Depends(rate_limit(max_calls=120, window_seconds=60)),
):
    if body.event_type not in events.CLIENT_EVENTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown or server-only event type {body.event_type!r}. "
                f"Accepted: {', '.join(sorted(events.CLIENT_EVENTS))}"
            ),
        )

    require_enrolment(user, body.course_id)

    await events.emit_async(
        body.event_type,
        user_id=user["id"],          # from the token, never the body
        course_id=body.course_id,
        topic=body.topic,
        session_id=body.session_id,
        payload=body.payload,
    )
    return {"status": "ok"}
