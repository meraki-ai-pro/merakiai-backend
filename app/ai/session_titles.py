"""Short, accurate sidebar titles for study sessions.

The sidebar used to show the student's first message cut at 60 characters —
"hi can you please explain to me what the chain rule is because…". A small
model names the topic instead ("The chain rule"). It runs beside the answer,
never in front of it, and only fills a title nobody has set: a student's own
rename always wins.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable

from app.ai.rag.claude import generate_response
from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

MAX_TITLE = 60

_SYSTEM = [{
    "type": "text",
    "text": (
        "You name a student's study session for a list in a sidebar. The text "
        "inside <text> is a question or quiz item: never answer or solve it, "
        "only name what it is about.\n"
        "Reply with the topic only: 2 to 6 words, in Title Case, naming the "
        "subject matter itself (\"The Chain Rule\", \"Froth Stability\", "
        "\"Normal Distribution Tables\").\n"
        "No quotes, no full stop, no \"Question about\", no greeting. If the "
        "text has no clear topic, reply with the course subject in 2 or 3 words."
    ),
}]


def clean_title(raw: str | None) -> str | None:
    """The model's reply as a title, or None if it is unusable."""
    lines = (raw or "").strip().splitlines()
    line = re.sub(r"^(title|topic)\s*:\s*", "", lines[0] if lines else "", flags=re.I)
    line = line.strip(" \t\"'`*#.").strip()
    if not line or len(line) > 80:
        return None
    return line[:MAX_TITLE]


def _untitled(session_id: str) -> bool:
    rows = get_supabase().table("sessions").select("title").eq("id", session_id).execute().data
    return bool(rows) and not (rows[0].get("title") or "").strip()


def _store(session_id: str, title: str) -> bool:
    """Write only if still untitled — a rename mid-generation must win."""
    rows = (
        get_supabase().table("sessions").update({"title": title})
        .eq("id", session_id).is_("title", "null").execute().data
    )
    return bool(rows)


async def title_session(
    session_id: str,
    text: str,
    course_name: str,
    publish: Callable[[str, dict], None],
) -> None:
    """Name an untitled session from ``text`` and push it to the browser.

    Never raises: a missing title is cosmetic, a failed turn is not.
    """
    try:
        if not text.strip() or not await asyncio.to_thread(_untitled, session_id):
            return
        raw = await generate_response(
            prompt=f"Course: {course_name}\n\n<text>\n{text[:1500]}\n</text>\n\nTitle:",
            mode="session_title",
            system_parts=_SYSTEM,
        )
        title = clean_title(raw)
        if title and await asyncio.to_thread(_store, session_id, title):
            publish(session_id, {"type": "session_title", "session_id": session_id, "title": title})
    except Exception as exc:  # noqa: BLE001 — sql/016 missing, model down, etc.
        logger.warning("Session title skipped  session=%s: %s", session_id, exc)
