"""Student offboarding: consent, soft-delete, retention, purge.

Ref: Meraki_AI_Integration_Roadmap Part D
     AI_Teaching_System_Technical_Specification_v3 §3

Two states, deliberately distinct:

**Soft-delete** revokes access immediately and starts a retention clock. It is
reversible, which matters because deletion requests are sometimes made in
anger, by mistake, or by a lecturer clicking the wrong row.

**Purge** is irreversible and only runs after the window expires. It removes
the account, its conversations, its uploads and its storage objects. If the
student consented to research, their event rows are *detached* rather than
deleted — the count survives, the person does not.

The order of operations in purge is not arbitrary. Storage and vectors are
cleared before the database rows that point at them: doing it the other way
loses the pointers and leaves orphans nobody can find.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from app.db.supabase import get_supabase
from app.media.storage_service import STUDENT_UPLOADS_BUCKET

logger = logging.getLogger(__name__)

# How long a soft-deleted account is recoverable. Long enough to undo a
# mistake, short enough to be a real deletion promise.
RETENTION_DAYS = 30


def _hash_email(email: str | None) -> str | None:
    """Kept so a later 'did you delete my data?' can be answered without
    retaining the address itself."""
    if not email:
        return None
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def soft_delete(user_id: str, *, requested_by: str, reason: str | None = None) -> dict:
    """Revoke access now, schedule the purge. Reversible until it runs."""
    sb = get_supabase()

    rows = sb.table("users").select("id, email, research_consent, deleted_at").eq(
        "id", user_id
    ).execute().data
    if not rows:
        raise LookupError(f"No user {user_id}")
    user = rows[0]

    if user.get("deleted_at"):
        return {"status": "already_deleted", "user_id": user_id}

    now = datetime.now(timezone.utc)
    purge_after = now + timedelta(days=RETENTION_DAYS)

    sb.table("users").update({
        "deleted_at": now.isoformat(),
        "deletion_requested_at": now.isoformat(),
        "purge_after": purge_after.isoformat(),
    }).eq("id", user_id).execute()

    # Withdraw from every course so the permission stack refuses the very next
    # turn, rather than waiting for the purge weeks later.
    try:
        sb.table("enrolments").update({
            "status": "withdrawn", "withdrawn_at": now.isoformat()
        }).eq("student_id", user_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not withdraw enrolments for %s: %s", user_id, exc)

    # Revoke live sessions. Best-effort: the account is already soft-deleted,
    # and auth_guard will refuse it on the next request regardless.
    try:
        sb.auth.admin.sign_out(user_id)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.info("Session revocation unavailable for %s: %s", user_id, exc)

    _record(
        subject_user_id=user_id,
        subject_email_hash=_hash_email(user.get("email")),
        requested_by=requested_by,
        reason=reason,
        research_consent=bool(user.get("research_consent")),
        soft_deleted_at=now.isoformat(),
    )

    return {
        "status": "soft_deleted",
        "user_id": user_id,
        "purge_after": purge_after.isoformat(),
        "recoverable_until": purge_after.isoformat(),
    }


def restore(user_id: str) -> dict:
    """Undo a soft-delete. Enrolments are NOT auto-restored.

    Withdrawal may have been a separate, deliberate decision by a lecturer, and
    silently re-enrolling someone into courses they were removed from would be
    a worse error than making them redeem an invite code again.
    """
    sb = get_supabase()
    updated = sb.table("users").update({
        "deleted_at": None, "deletion_requested_at": None, "purge_after": None,
    }).eq("id", user_id).execute().data

    if not updated:
        raise LookupError(f"No user {user_id}")
    return {"status": "restored", "user_id": user_id, "enrolments_restored": False}


def due_for_purge() -> list[dict]:
    """Soft-deleted accounts whose retention window has expired."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        return (
            get_supabase().table("users")
            .select("id, email, research_consent, purge_after")
            .not_.is_("deleted_at", "null")
            .lte("purge_after", now)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Purge scan failed: %s", exc)
        return []


def purge(user_id: str) -> dict:
    """Irreversibly remove a user's data. Only call after the window expires."""
    sb = get_supabase()

    rows = sb.table("users").select("id, email, research_consent, deleted_at").eq(
        "id", user_id
    ).execute().data
    if not rows:
        raise LookupError(f"No user {user_id}")
    user = rows[0]

    if not user.get("deleted_at"):
        # Guard rail: purge is irreversible and must never run against a live
        # account because someone called it with the wrong id.
        raise ValueError("Refusing to purge a user who has not been soft-deleted first.")

    consented = bool(user.get("research_consent"))
    counts: dict[str, int] = {}

    # 1. Storage first — while the rows that point at it still exist.
    storage_removed = _purge_storage(user_id)

    # 2. Event rows: detached if consented, deleted otherwise.
    if consented:
        try:
            res = sb.rpc("anonymise_user_events", {"p_user_id": user_id}).execute()
            counts["events_anonymised"] = res.data if isinstance(res.data, int) else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("Event anonymisation failed for %s: %s", user_id, exc)
    else:
        counts["events_deleted"] = _delete_where("events", "user_id", user_id)

    # 3. Personal content. Conversations carry the student's own words and
    # photographs of their work; these go regardless of research consent,
    # because consent covers aggregates, not transcripts.
    for table, column in (
        ("conversations", "user_id"),
        ("sessions", "user_id"),
        ("mode_sessions", "user_id"),
        ("assessment_attempts", "student_id"),
        ("mastery_states", "student_id"),
        ("enrolments", "student_id"),
        ("feedback_responses", "user_id"),
    ):
        counts[table] = _delete_where(table, column, user_id)

    # 4. The account itself, last.
    try:
        sb.auth.admin.delete_user(user_id)
        counts["auth_user"] = 1
    except Exception as exc:  # noqa: BLE001
        logger.error("Auth deletion failed for %s: %s", user_id, exc)
        counts["auth_user"] = 0

    now = datetime.now(timezone.utc).isoformat()
    _record(
        subject_user_id=user_id,
        subject_email_hash=_hash_email(user.get("email")),
        requested_by=None,
        reason="retention window expired",
        research_consent=consented,
        purged_at=now,
        rows_purged=counts,
        storage_purged=storage_removed,
    )

    logger.info("Purged user %s: %s", user_id, counts)
    return {"status": "purged", "user_id": user_id, "rows": counts,
            "storage_objects": storage_removed, "events_anonymised": consented}


def _delete_where(table: str, column: str, value: str) -> int:
    try:
        res = get_supabase().table(table).delete().eq(column, value).execute()
        return len(res.data or [])
    except Exception as exc:  # noqa: BLE001 — a missing table must not stop the purge
        logger.warning("Purge of %s failed: %s", table, exc)
        return 0


def _purge_storage(user_id: str) -> int:
    """Remove the student's uploaded photographs."""
    sb = get_supabase()
    removed = 0
    try:
        entries = sb.storage.from_(STUDENT_UPLOADS_BUCKET).list(user_id) or []
        paths: list[str] = []
        for entry in entries:
            name = entry.get("name") if isinstance(entry, dict) else getattr(entry, "name", None)
            if not name:
                continue
            # Uploads are stored under {user_id}/{session_id}/{uuid}.ext, so
            # each entry here is a session folder that must be listed in turn.
            sub = sb.storage.from_(STUDENT_UPLOADS_BUCKET).list(f"{user_id}/{name}") or []
            for item in sub:
                fname = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
                if fname:
                    paths.append(f"{user_id}/{name}/{fname}")
        if paths:
            sb.storage.from_(STUDENT_UPLOADS_BUCKET).remove(paths)
            removed = len(paths)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Storage purge failed for %s: %s", user_id, exc)
    return removed


def _record(**fields) -> None:
    """Write the deletion record. Lives outside the deleted data on purpose."""
    try:
        get_supabase().table("deletion_records").insert(fields).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not write deletion record for %s: %s", fields.get("subject_user_id"), exc)
