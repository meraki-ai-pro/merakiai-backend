import os
import uuid
from app.db.supabase import get_supabase
from app.config import load_env

load_env()

BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET")
PUBLIC_BASE = os.getenv("SUPABASE_STORAGE_PUBLIC_BASE")

def upload_audio_and_get_url(user_id: str, session_id: str, mp3_bytes: bytes) -> str:
    supabase = get_supabase()
    path = f"{user_id}/{session_id}/{uuid.uuid4()}.mp3"

    supabase.storage.from_(BUCKET).upload(
        path=path,
        file=mp3_bytes,
        file_options={"content-type": "audio/mpeg", "upsert": "false"},
    )

    # If bucket is public:
    if PUBLIC_BASE:
        return f"{PUBLIC_BASE}/{BUCKET}/{path}"

    # If bucket is private, generate signed URL:
    signed = supabase.storage.from_(BUCKET).create_signed_url(path, 60 * 30)  # 30 mins
    return signed["signedURL"]
