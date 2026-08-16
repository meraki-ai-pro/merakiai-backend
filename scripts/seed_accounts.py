"""Create/repair the E2E test accounts (lecturer + students) via the service role key.

Emails use ug.edu.gh: pydantic's EmailStr rejects reserved TLDs like .test, and
the pilot is a University of Ghana cohort so this also reads correctly on the
recording the client will watch.
"""
import os, sys, json
from pathlib import Path

# Resolved from this file rather than hard-coded, so the script runs from any
# working directory and on any checkout.
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)
from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")
from supabase import create_client

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

bundles = sb.table("avatar_voice_bundles").select("avatar_id, is_active").execute().data or []
avatar_id = next((b["avatar_id"] for b in bundles if b.get("is_active")), None)
print("using avatar_id:", avatar_id)

STALE = ["e2e.lecturer@merakiai.test", "e2e.student1@merakiai.test", "e2e.student2@merakiai.test"]

# Passwords come from the environment (or the frontend's gitignored
# e2e/.env.e2e, which Playwright reads too) so that real Supabase credentials
# are never committed. Set them before running this script.
def _password(var: str) -> str:
    value = os.getenv(var)
    if not value:
        raise SystemExit(
            f"{var} is not set. Set it in the environment, or copy "
            "merakiai-frontend/e2e/.env.e2e.example to .env.e2e and export the "
            "values, so the seeded passwords match what the E2E suite logs in with."
        )
    return value


ACCOUNTS = [
    (os.getenv("E2E_LECTURER_EMAIL", "e2e.lecturer@ug.edu.gh"),
     _password("E2E_LECTURER_PASSWORD"), "lecturer", "Ama", "Mensah"),
    (os.getenv("E2E_STUDENT1_EMAIL", "e2e.student1@ug.edu.gh"),
     _password("E2E_STUDENT1_PASSWORD"), "user", "Kwame", "Owusu"),
    (os.getenv("E2E_STUDENT2_EMAIL", "e2e.student2@ug.edu.gh"),
     _password("E2E_STUDENT2_PASSWORD"), "user", "Akosua", "Boateng"),
]


def all_users():
    page = sb.auth.admin.list_users()
    return page if isinstance(page, list) else getattr(page, "users", [])


def find_uid(email):
    for u in all_users():
        if (u.email or "").lower() == email.lower():
            return u.id
    return None


# Remove the accounts created with the rejected .test domain.
for email in STALE:
    uid = find_uid(email)
    if uid:
        try:
            sb.table("users").delete().eq("id", uid).execute()
            sb.auth.admin.delete_user(uid)
            print(f"deleted stale {email}")
        except Exception as e:
            print(f"delete {email} failed: {e}")

for email, password, role, first, last in ACCOUNTS:
    uid = None
    try:
        res = sb.auth.admin.create_user({
            "email": email, "password": password, "email_confirm": True,
            "user_metadata": {"first_name": first, "last_name": last},
        })
        uid = res.user.id
        print(f"created auth user {email} -> {uid}")
    except Exception as e:
        uid = find_uid(email)
        if uid:
            sb.auth.admin.update_user_by_id(uid, {"password": password, "email_confirm": True})
            print(f"reset password for existing {email} -> {uid}")
        else:
            print(f"!! create_user({email}) failed: {e}")
            continue

    row = {"id": uid, "email": email, "first_name": first, "last_name": last,
           "university_name": "University of Ghana", "country": "Ghana", "role": role}
    if avatar_id:
        row["avatar_id"] = avatar_id
    try:
        sb.table("users").upsert(row, on_conflict="id").execute()
        print(f"  public.users upserted role={role}")
    except Exception as e:
        print(f"  users upsert failed: {e}")

print("\n--- final ---")
for email, *_ in ACCOUNTS:
    print(sb.table("users").select("id,email,role,avatar_id").eq("email", email).execute().data)
