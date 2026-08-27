"""Provision a lecturer without exposing a temporary password.

The command generates a cryptographically random one-time password, marks the
Supabase user as requiring a password change, upserts the public lecturer
profile, and sends Supabase's recovery email. The generated password is never
printed or written to disk; the lecturer chooses their own password from the
emailed link.

Run with ``--dry-run`` first. Existing accounts are never password-rotated
unless ``--rotate-existing`` is explicitly supplied.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.config import load_env  # noqa: E402
from supabase import create_client  # noqa: E402

load_env()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--first-name", required=True)
    parser.add_argument("--last-name", required=True)
    parser.add_argument("--institution", default="")
    parser.add_argument("--country", default="Ghana")
    parser.add_argument("--redirect-to", default="")
    parser.add_argument("--rotate-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _all_users(client):
    response = client.auth.admin.list_users()
    return response if isinstance(response, list) else getattr(response, "users", [])


def _find_user(client, email: str):
    expected = email.casefold()
    return next(
        (user for user in _all_users(client) if (user.email or "").casefold() == expected),
        None,
    )


def main() -> int:
    args = _arguments()
    email = args.email.strip().lower()
    redirect_to = args.redirect_to.strip() or (
        f"{os.environ.get('PUBLIC_SITE_URL', 'http://localhost:3001').rstrip('/')}"
        "/auth/reset-password"
    )

    if args.dry_run:
        print(
            "DRY RUN: would provision lecturer "
            f"{email}, require a password reset, and email a recovery link to {redirect_to}"
        )
        return 0

    url = os.environ.get("SUPABASE_URL", "").strip()
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not service_key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")

    client = create_client(url, service_key)
    existing = _find_user(client, email)
    temporary_password = secrets.token_urlsafe(32)
    metadata = dict(getattr(existing, "user_metadata", None) or {})
    metadata.update(
        {
            "first_name": args.first_name.strip(),
            "last_name": args.last_name.strip(),
            "must_change_password": True,
        }
    )

    if existing:
        auth_user_id = existing.id
        update = {"email_confirm": True, "user_metadata": metadata}
        if args.rotate_existing:
            update["password"] = temporary_password
        client.auth.admin.update_user_by_id(auth_user_id, update)
        action = "updated"
    else:
        response = client.auth.admin.create_user(
            {
                "email": email,
                "password": temporary_password,
                "email_confirm": True,
                "user_metadata": metadata,
            }
        )
        auth_user_id = response.user.id
        action = "created"

    profile = {
        "id": auth_user_id,
        "email": email,
        "first_name": args.first_name.strip(),
        "last_name": args.last_name.strip(),
        "country": args.country.strip() or "Ghana",
        "role": "lecturer",
    }
    if args.institution.strip():
        profile["university_name"] = args.institution.strip()
    client.table("users").upsert(profile, on_conflict="id").execute()

    client.auth.reset_password_for_email(email, {"redirect_to": redirect_to})
    print(
        f"Lecturer {action}: {email}. A password-setup link was sent; "
        "the generated temporary password was not disclosed or stored."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
