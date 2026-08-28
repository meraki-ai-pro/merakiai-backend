"""Inventory or remove only the isolated production E2E course and accounts.

The default is a read-only inventory. Destructive cleanup requires an explicit
``--execute`` flag and an exact confirmation phrase. Synthetic account removal
is optional and goes through the application's audited lifecycle service.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND / ".env")

from app.core import lifecycle  # noqa: E402
from supabase import create_client  # noqa: E402


COURSE = "e2e-calculus-101"
EMAILS = (
    "e2e.lecturer@ug.edu.gh",
    "e2e.student1@ug.edu.gh",
    "e2e.student2@ug.edu.gh",
)
DATA_CONFIRMATION = "DELETE E2E DATA"
ACCOUNT_CONFIRMATION = "DELETE E2E DATA AND ACCOUNTS"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--delete-accounts", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser.parse_args()


def _client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
    return create_client(url, key)


def _ids(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({str(row["id"]) for row in rows if row.get("id")})


def inventory(sb) -> dict[str, Any]:
    users = (
        sb.table("users")
        .select("id,email,role,deleted_at")
        .in_("email", EMAILS)
        .execute()
        .data
        or []
    )
    user_ids = _ids(users)
    sessions_by_user = []
    if user_ids:
        sessions_by_user = (
            sb.table("sessions").select("id").in_("user_id", user_ids).execute().data
            or []
        )
    sessions_by_course = (
        sb.table("sessions").select("id").eq("course_id", COURSE).execute().data or []
    )
    session_ids = _ids(sessions_by_user + sessions_by_course)
    documents = (
        sb.table("documents")
        .select("id,source_filename,status,is_published")
        .eq("course_id", COURSE)
        .execute()
        .data
        or []
    )
    courses = (
        sb.table("courses").select("id,name,owner_id").eq("id", COURSE).execute().data
        or []
    )
    return {
        "users": users,
        "user_ids": user_ids,
        "sessions": session_ids,
        "documents": documents,
        "document_ids": _ids(documents),
        "courses": courses,
    }


def print_inventory(state: dict[str, Any]) -> None:
    print(f"course: {state['courses'] or 'not present'}")
    print("accounts:")
    found = {row["email"]: row for row in state["users"]}
    for email in EMAILS:
        row = found.get(email)
        if row:
            print(
                f"  {email} id={row['id']} role={row.get('role')} "
                f"deleted={bool(row.get('deleted_at'))}"
            )
        else:
            print(f"  {email} not present")
    print(f"sessions: {len(state['sessions'])}")
    print(f"documents: {len(state['documents'])}")
    for row in state["documents"]:
        print(
            f"  {row.get('source_filename')} status={row.get('status')} "
            f"published={bool(row.get('is_published'))}"
        )


def _delete_in(sb, table: str, column: str, values: list[str]) -> None:
    if not values:
        return
    try:
        rows = sb.table(table).delete().in_(column, values).execute().data or []
        print(f"cleared {table}: {len(rows)}")
    except Exception as exc:  # noqa: BLE001 - optional legacy tables vary by deployment
        print(f"skip {table}: {str(exc)[:160]}")


def _delete_eq(sb, table: str, column: str, value: str) -> None:
    try:
        rows = sb.table(table).delete().eq(column, value).execute().data or []
        print(f"cleared {table}: {len(rows)}")
    except Exception as exc:  # noqa: BLE001 - optional legacy tables vary by deployment
        print(f"skip {table}: {str(exc)[:160]}")


def remove_data(sb, state: dict[str, Any]) -> None:
    session_ids = state["sessions"]
    user_ids = state["user_ids"]

    # Session children first; deployments created at different stages of the
    # pilot do not all have every legacy table, so absent tables are reported.
    for table in (
        "conversations",
        "session_surveys",
        "mode_sessions",
        "mode_feedback",
        "review_attempts",
        "review_summaries",
        "session_state",
    ):
        _delete_in(sb, table, "session_id", session_ids)

    for table in ("user_feedback", "request_metrics", "platform_sessions", "ws_sessions"):
        _delete_in(sb, table, "user_id", user_ids)

    _delete_in(sb, "sessions", "id", session_ids)
    _delete_in(sb, "document_chunks", "document_id", state["document_ids"])

    # Explicit deletes keep the cleanup observable even where course FKs also
    # cascade. The course row is always last.
    for table in (
        "assessment_attempts",
        "mastery_states",
        "feedback_responses",
        "media_assets",
        "documents",
        "enrolments",
        "enrolment_invitations",
        "invite_codes",
        "assessments",
    ):
        _delete_eq(sb, table, "course_id", COURSE)
    _delete_eq(sb, "courses", "id", COURSE)

    try:
        from app.ai.ingestion.pinecone import _get_index

        index = _get_index()
        stats = index.describe_index_stats()
        namespaces = getattr(stats, "namespaces", None)
        if namespaces is None and isinstance(stats, dict):
            namespaces = stats.get("namespaces")
        for namespace in list(namespaces or {}):
            if str(namespace).startswith(COURSE):
                index.delete(delete_all=True, namespace=namespace)
                print(f"dropped Pinecone namespace {namespace}")
    except Exception as exc:  # noqa: BLE001
        print(f"Pinecone cleanup failed: {str(exc)[:200]}")


def remove_accounts(state: dict[str, Any]) -> None:
    # These identities are synthetic fixtures, not people. They still go
    # through soft-delete + purge so deletion_records captures what happened.
    for user in state["users"]:
        user_id = str(user["id"])
        if not user.get("deleted_at"):
            lifecycle.soft_delete(
                user_id,
                requested_by=user_id,
                reason="synthetic E2E account cleanup",
            )
        result = lifecycle.purge(user_id)
        print(f"purged account {user['email']}: {result['rows']}")


def main() -> int:
    args = arguments()
    sb = _client()
    state = inventory(sb)
    print_inventory(state)

    if not args.execute:
        phrase = ACCOUNT_CONFIRMATION if args.delete_accounts else DATA_CONFIRMATION
        print(f'DRY RUN: rerun with --execute --confirm "{phrase}"')
        return 0

    expected = ACCOUNT_CONFIRMATION if args.delete_accounts else DATA_CONFIRMATION
    if args.confirm != expected:
        raise RuntimeError(f'Cleanup requires --confirm "{expected}"')

    remove_data(sb, state)
    if args.delete_accounts:
        remove_accounts(state)

    remaining = inventory(sb)
    print("POST-CLEANUP INVENTORY")
    print_inventory(remaining)
    if remaining["courses"] or remaining["sessions"] or remaining["documents"]:
        raise RuntimeError("E2E cleanup verification failed: scoped data remains")
    if args.delete_accounts and remaining["users"]:
        raise RuntimeError("E2E account cleanup verification failed: scoped users remain")
    print("E2E CLEANUP VERIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
