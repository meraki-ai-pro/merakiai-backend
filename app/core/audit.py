"""Audit trail for lecturer and admin actions.

Every write that changes who can see what — uploads, deletions, publish
toggles, enrolment changes, video approvals — leaves a record here.

Logging is deliberately non-fatal. It runs alongside an action that has already
succeeded, so raising would turn a completed enrolment into a 500 and leave the
caller unsure whether it took effect. A missing log line is a gap in the record;
a failed action the user believes succeeded is worse.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)


def _client_meta(request: Request | None) -> dict[str, str | None]:
    if request is None:
        return {"ip_address": None, "user_agent": None}
    # X-Forwarded-For is client-controlled and only trustworthy behind a proxy
    # that overwrites it. Recorded as an observation, never as an authorisation
    # input.
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else None
    )
    return {"ip_address": ip, "user_agent": request.headers.get("user-agent")}


def record(
    *,
    actor: dict,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    course_id: str | None = None,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    """Write one audit row. Never raises."""
    row = {
        "actor_id": actor.get("id"),
        "actor_role": actor.get("role"),
        "action": action,
        "resource_type": resource_type,
        "resource_id": str(resource_id) if resource_id is not None else None,
        "course_id": course_id,
        "old_values": old_values,
        "new_values": new_values,
        **_client_meta(request),
    }

    try:
        get_supabase().table("audit_logs").insert(row).execute()
    except Exception as exc:  # noqa: BLE001 — see module docstring
        logger.warning(
            "Audit write failed (%s %s on %s); the action itself succeeded: %s",
            action, resource_type, resource_id, exc,
        )
