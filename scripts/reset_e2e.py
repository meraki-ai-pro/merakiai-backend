"""Reset the E2E course so the recorded walkthrough always starts from zero.

Only touches rows belonging to the e2e-* course and the three e2e accounts.
Pinecone namespaces for the course are dropped too, otherwise a re-run would
retrieve vectors from the previous run's ingestion and the demo would show
citations that predate the upload the client just watched.
"""
import os, sys
from pathlib import Path

# Resolved from this file rather than hard-coded, so the script runs from any
# working directory and on any checkout.
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)
from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")
from supabase import create_client

COURSE = "e2e-calculus-101"
EMAILS = ["e2e.lecturer@ug.edu.gh", "e2e.student1@ug.edu.gh", "e2e.student2@ug.edu.gh"]

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

users = sb.table("users").select("id,email").in_("email", EMAILS).execute().data or []
uids = [u["id"] for u in users]
print("accounts:", {u["email"]: u["id"][:8] for u in users})

# Sessions for the test accounts and every session attached to the disposable
# course. A lecturer can open a course in student view, and older walkthroughs
# therefore left sessions owned by accounts outside EMAILS. Leaving those rows
# behind prevents the course delete through sessions_course_id_fkey.
sessions_by_user = []
if uids:
    sessions_by_user = sb.table("sessions").select("id").in_("user_id", uids).execute().data or []
sessions_by_course = (
    sb.table("sessions").select("id").eq("course_id", COURSE).execute().data or []
)
sids = sorted({s["id"] for s in sessions_by_user + sessions_by_course})
print("sessions to clear:", len(sids))

for table, col, vals in [
    ("conversations", "session_id", sids),
    ("session_surveys", "session_id", sids),
    ("mode_sessions", "session_id", sids),
]:
    if not vals:
        continue
    try:
        sb.table(table).delete().in_(col, vals).execute()
        print(f"cleared {table}")
    except Exception as e:
        print(f"skip {table}: {str(e)[:110]}")

for table, col, vals in [
    ("user_feedback", "user_id", uids),
    ("sessions", "id", sids),
]:
    if not vals:
        continue
    try:
        sb.table(table).delete().in_(col, vals).execute()
        print(f"cleared {table}")
    except Exception as e:
        print(f"skip {table}: {str(e)[:110]}")

# document_chunks belongs to a document, not directly to a course.
documents = sb.table("documents").select("id").eq("course_id", COURSE).execute().data or []
document_ids = [row["id"] for row in documents]
if document_ids:
    try:
        sb.table("document_chunks").delete().in_("document_id", document_ids).execute()
        print("cleared document_chunks")
    except Exception as e:
        print(f"skip document_chunks: {str(e)[:110]}")

for table in ["assessment_attempts", "media_assets", "documents", "enrolments",
              "enrolment_invitations", "invite_codes", "assessments", "courses"]:
    try:
        sb.table(table).delete().eq("course_id" if table != "courses" else "id", COURSE).execute()
        print(f"cleared {table}")
    except Exception as e:
        print(f"skip {table}: {str(e)[:110]}")

# Pinecone namespaces for this course.
try:
    from app.ai.ingestion.pinecone import _get_index
    index = _get_index()
    stats = index.describe_index_stats()
    for ns in list((stats or {}).get("namespaces", {}) or {}):
        if ns.startswith(COURSE):
            index.delete(delete_all=True, namespace=ns)
            print("dropped namespace", ns)
except Exception as e:
    print("pinecone cleanup skipped:", str(e)[:160])

print("reset done")
