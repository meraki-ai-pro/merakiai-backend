"""Admin feedback inbox: the actual submissions, with who sent them.

The admin dashboard already had `/admin/analytics/feedback`, but that endpoint
returns *aggregates* — four survey averages, a count per type, and the last
twenty messages with no author, no role and no course. It answers "how are we
doing on average" and cannot answer the question an administrator actually
opens the page with: **who said this, are they a student or a lecturer, and
which course were they on?**

It also reads only two of the three tables. `feedback_responses` (sql/011) is
where the NPS, mode and lecturer surveys land, and nothing on the admin side
ever looked at it, so every one of those submissions was invisible.

This module is the inbox: one merged, filterable, paginated stream over all
three sources, each row carrying the submitter's name, email and role. The
aggregates endpoint keeps its job; this one keeps the record.

Reading identity is deliberate and is what an admin surface is for — the
student-facing copy says feedback goes to "your lecturer and the research
team", not that it is anonymous. Nothing here is exposed to another student.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from app.core.auth import admin_guard
from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["Admin – Feedback"])

Source = Literal["user_feedback", "session_survey", "feedback_suite"]

# Hard ceiling per source before merging. A pilot generates thousands of rows,
# not millions, and an unbounded scan on an admin page is how a dashboard takes
# the database down.
_PER_SOURCE_LIMIT = 2000


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


async def _t(fn):
    """Run one blocking Supabase call off the event loop, degrading to []."""
    def _safe():
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — a missing table loses one source
            logger.warning("Admin feedback source unavailable: %s", exc)
            return []

    return await asyncio.to_thread(_safe)


def _profiles(user_ids: set[str]) -> dict[str, dict]:
    """Name, email and role for a set of ids, in one query.

    The whole point of this endpoint is attribution, so the lookup is a bulk
    `in_` rather than a per-row join — a page of 50 items would otherwise be
    50 round trips.
    """
    ids = [uid for uid in user_ids if uid]
    if not ids:
        return {}
    try:
        rows = (
            get_supabase().table("users")
            .select("id, email, first_name, last_name, role")
            .in_("id", ids).execute().data or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not resolve feedback authors: %s", exc)
        return {}
    return {r["id"]: r for r in rows}


def _author(profile: dict | None, role_at_time: str | None = None) -> dict:
    """Attribution for one row.

    ``role_at_time`` WINS over the account's current role where it was
    recorded. A student who has since been made a lecturer did not submit that
    rating as a lecturer, and reading it as one would misattribute the pilot's
    own data — which is exactly why sql/011 stores the role on the row.
    """
    if not profile:
        # A deleted account keeps its feedback — the research data outlives the
        # user — so "unknown" is a real state, not an error.
        return {"name": None, "email": None, "role": role_at_time or "unknown", "deleted": True}

    name = " ".join(
        part for part in (profile.get("first_name"), profile.get("last_name")) if part
    ).strip()
    return {
        "name": name or None,
        "email": profile.get("email"),
        "role": role_at_time or profile.get("role") or "user",
        "current_role": profile.get("role"),
        "deleted": False,
    }


@router.get("")
async def list_feedback(
    days: int = Query(90, ge=1, le=365),
    role: str | None = Query(None, description="student|lecturer|admin — role at submission"),
    source: Source | None = None,
    course_id: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _user=Depends(admin_guard),
):
    """Every piece of feedback in the window, newest first, with its author."""
    since = _cutoff(days)
    sb = get_supabase

    def _free_text(columns: str):
        return (
            sb().table("user_feedback").select(columns)
            .gte("created_at", since).order("created_at", desc=True)
            .limit(_PER_SOURCE_LIMIT).execute().data or []
        )

    def _free_text_tolerant():
        # Falling back rather than losing the source: before sql/013 there is no
        # course_id, and a 400 here would empty the whole free-text column of
        # the inbox to gain one field.
        try:
            return _free_text(
                "id, user_id, session_id, course_id, feedback_type, message, created_at"
            )
        except Exception:  # noqa: BLE001
            return _free_text("id, user_id, session_id, feedback_type, message, created_at")

    free_text, surveys, suite = await asyncio.gather(
        _t(_free_text_tolerant),
        _t(lambda: sb().table("session_surveys")
           .select("id, user_id, session_id, clarity_rating, helpfulness_rating, "
                   "confidence_rating, overall_rating, created_at")
           .gte("created_at", since).order("created_at", desc=True)
           .limit(_PER_SOURCE_LIMIT).execute().data or []),
        _t(lambda: sb().table("feedback_responses")
           .select("id, user_id, role_at_time, course_id, session_id, feedback_type, "
                   "rating, nps_score, mode, free_text, structured_answers, created_at")
           .gte("created_at", since).order("created_at", desc=True)
           .limit(_PER_SOURCE_LIMIT).execute().data or []),
    )

    profiles = _profiles(
        {r.get("user_id") for r in free_text}
        | {r.get("user_id") for r in surveys}
        | {r.get("user_id") for r in suite}
    )

    items: list[dict[str, Any]] = []

    for row in free_text:
        items.append({
            "id": row["id"],
            "source": "user_feedback",
            "kind": row.get("feedback_type"),
            "message": row.get("message"),
            "ratings": None,
            "course_id": row.get("course_id"),
            "session_id": row.get("session_id"),
            "created_at": row.get("created_at"),
            "author": _author(profiles.get(row.get("user_id"))),
        })

    for row in surveys:
        ratings = {
            "clarity": row.get("clarity_rating"),
            "helpfulness": row.get("helpfulness_rating"),
            "confidence": row.get("confidence_rating"),
            "overall": row.get("overall_rating"),
        }
        items.append({
            "id": row["id"],
            "source": "session_survey",
            "kind": "session_survey",
            "message": None,
            "ratings": ratings,
            "course_id": None,
            "session_id": row.get("session_id"),
            "created_at": row.get("created_at"),
            "author": _author(profiles.get(row.get("user_id"))),
        })

    for row in suite:
        items.append({
            "id": row["id"],
            "source": "feedback_suite",
            "kind": row.get("feedback_type"),
            "message": row.get("free_text"),
            "ratings": {
                "rating": row.get("rating"),
                "nps": row.get("nps_score"),
                "mode": row.get("mode"),
            },
            "structured_answers": row.get("structured_answers") or {},
            "course_id": row.get("course_id"),
            "session_id": row.get("session_id"),
            "created_at": row.get("created_at"),
            # role_at_time beats the current role: a student who has since been
            # made a lecturer did not submit that rating as a lecturer, and
            # reading it as one would misattribute the whole pilot.
            "author": _author(profiles.get(row.get("user_id")), row.get("role_at_time")),
        })

    if source:
        items = [i for i in items if i["source"] == source]
    if course_id:
        items = [i for i in items if i.get("course_id") == course_id]
    if role:
        wanted = role.strip().lower()
        # "student" is what an administrator calls the role the database stores
        # as 'user'. Making them type the internal value would be a trap.
        aliases = {"student": {"user", "student"}, "user": {"user", "student"}}
        allowed = aliases.get(wanted, {wanted})
        items = [i for i in items if (i["author"]["role"] or "").lower() in allowed]

    items.sort(key=lambda i: i.get("created_at") or "", reverse=True)

    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start:start + page_size]

    return {
        "items": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "days": days,
        # Counted over the whole filtered set, not the page, so the header
        # reads the same whichever page is open.
        "by_role": dict(Counter((i["author"]["role"] or "unknown") for i in items)),
        "by_source": dict(Counter(i["source"] for i in items)),
        "by_kind": dict(Counter(i["kind"] for i in items if i["kind"])),
    }
