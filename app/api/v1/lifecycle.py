"""Consent and account deletion.

Ref: Meraki_AI_Integration_Roadmap Part D, Proposal §7.2

A student can request their own deletion. An admin can action one on their
behalf. Nobody can trigger the irreversible purge from an endpoint — that runs
only against accounts whose retention window has already expired, and only
through the explicit sweep below.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core import audit, lifecycle
from app.core.auth import admin_guard, auth_guard, require_mfa_if_enrolled
from app.db.supabase import get_supabase, get_user_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/account", tags=["Account lifecycle"])


class ConsentIn(BaseModel):
    research_consent: bool


class DeleteIn(BaseModel):
    reason: str | None = Field(None, max_length=1000)
    # Typed confirmation rather than a boolean: a mis-sent JSON body should not
    # be able to delete an account.
    confirm: str = Field(..., description="Must be exactly 'DELETE MY ACCOUNT'")


@router.get("/consent")
def get_consent(user=Depends(auth_guard)):
    rows = (
        get_user_client(user["token"]).table("users")
        .select("research_consent, research_consent_at")
        .eq("id", user["id"]).execute().data
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Profile not found")
    return rows[0]


@router.patch("/consent")
def set_consent(body: ConsentIn, request: Request, user=Depends(auth_guard)):
    """Record or withdraw research consent.

    Withdrawal is honoured going forward. It does not retroactively delete
    already-anonymised aggregates, because those no longer identify anyone —
    that is stated plainly so the student is not misled about what changes.
    """
    updates = {
        "research_consent": body.research_consent,
        "research_consent_at": "now()" if body.research_consent else None,
    }
    get_user_client(user["token"]).table("users").update(updates).eq(
        "id", user["id"]
    ).execute()

    audit.record(
        actor=user, action="consent.set", resource_type="user",
        resource_id=user["id"], new_values={"research_consent": body.research_consent},
        request=request,
    )
    return {
        "status": "ok",
        "research_consent": body.research_consent,
        "note": (
            "Applies going forward. Data already anonymised for research no "
            "longer identifies you and is not affected."
        ),
    }


@router.post("/delete")
def request_deletion(
    body: DeleteIn,
    request: Request,
    user=Depends(auth_guard),
    _mfa=Depends(require_mfa_if_enrolled),
):
    """Student-initiated deletion. Reversible for the retention window."""
    if body.confirm != "DELETE MY ACCOUNT":
        raise HTTPException(
            status_code=400,
            detail="Type DELETE MY ACCOUNT exactly to confirm.",
        )

    try:
        result = lifecycle.soft_delete(user["id"], requested_by=user["id"], reason=body.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc

    audit.record(
        actor=user, action="account.delete_requested", resource_type="user",
        resource_id=user["id"], new_values={"reason": body.reason}, request=request,
    )
    return {
        **result,
        "message": (
            f"Your account is deactivated and will be permanently deleted after "
            f"{lifecycle.RETENTION_DAYS} days. Contact your lecturer before then "
            "if this was a mistake."
        ),
    }


# ── Admin ───────────────────────────────────────────────────────────────────

@router.post("/admin/{user_id}/delete")
def admin_delete(
    user_id: str,
    body: DeleteIn,
    request: Request,
    user=Depends(admin_guard),
    _mfa=Depends(require_mfa_if_enrolled),
):
    if body.confirm != "DELETE MY ACCOUNT":
        raise HTTPException(status_code=400, detail="Confirmation phrase required.")
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Use /account/delete for your own account.")

    try:
        result = lifecycle.soft_delete(user_id, requested_by=user["id"], reason=body.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="User not found") from exc

    audit.record(
        actor=user, action="account.admin_deleted", resource_type="user",
        resource_id=user_id, new_values={"reason": body.reason}, request=request,
    )
    return result


@router.post("/admin/{user_id}/restore")
def admin_restore(user_id: str, request: Request, user=Depends(admin_guard)):
    try:
        result = lifecycle.restore(user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="User not found") from exc

    audit.record(
        actor=user, action="account.restored", resource_type="user",
        resource_id=user_id, request=request,
    )
    return {
        **result,
        "note": "Course enrolments were not restored — re-enrol the student explicitly.",
    }


@router.get("/admin/pending-purge")
def pending_purge(user=Depends(admin_guard)):
    """Accounts whose retention window has expired and are ready to purge."""
    due = lifecycle.due_for_purge()
    return {
        "count": len(due),
        "users": [{"id": u["id"], "purge_after": u.get("purge_after")} for u in due],
    }


@router.post("/admin/run-purge")
def run_purge(
    request: Request,
    dry_run: bool = True,
    user=Depends(admin_guard),
    _mfa=Depends(require_mfa_if_enrolled),
):
    """Purge every account past its retention window.

    Defaults to a dry run. This is the only irreversible operation in the
    system and it deletes real people's work — the default must be the safe
    one, and the caller has to opt in to the destructive path explicitly.
    """
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="Only a super admin may run a purge.")

    due = lifecycle.due_for_purge()

    if dry_run:
        return {
            "dry_run": True,
            "would_purge": len(due),
            "users": [u["id"] for u in due],
            "hint": "Call again with dry_run=false to execute.",
        }

    results = []
    for candidate in due:
        try:
            results.append(lifecycle.purge(candidate["id"]))
        except Exception as exc:  # noqa: BLE001 — one failure must not stop the rest
            logger.error("Purge failed for %s: %s", candidate["id"], exc)
            results.append({"status": "failed", "user_id": candidate["id"], "error": str(exc)[:300]})

    audit.record(
        actor=user, action="account.purge_run", resource_type="user",
        new_values={"count": len(results)}, request=request,
    )
    return {"dry_run": False, "purged": len(results), "results": results}
