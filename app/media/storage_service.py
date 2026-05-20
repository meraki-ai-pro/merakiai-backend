import os
import uuid
import logging
from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

# ── Audio bucket (AI-generated session audio) ─────────────────────────────
BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET")
PUBLIC_BASE = os.getenv("SUPABASE_STORAGE_PUBLIC_BASE")

# ── Profile picture bucket ─────────────────────────────────────────────────
_PROFILE_PICS_BUCKET = "user-profile-pics"
_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_PROFILE_PIC_BYTES = 5 * 1024 * 1024  # 5 MB

# Extension map — used to build a stable, overwritable filename per user
_EXT_MAP = {
    "image/jpeg": "jpg",
    "image/png":  "png",
    "image/webp": "webp",
}


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


def upload_profile_picture(user_id: str, image_bytes: bytes, content_type: str) -> str:
    """
    Upload a user profile picture to the 'user-profile-pics' bucket.

    - Content type must be image/jpeg, image/png, or image/webp.
    - Max size: 5 MB (caller is responsible for enforcing this before calling).
    - Each user has exactly one profile picture — re-uploading overwrites the
      previous file (upsert=true). Path pattern: {user_id}/profile.{ext}
    - The bucket is public — returns a stable public URL, no expiry.

    Raises:
        ValueError: if content_type is not an allowed image type.
        RuntimeError: if the Supabase storage upload fails.
    """
    if content_type not in _ALLOWED_IMAGE_TYPES:
        raise ValueError(
            f"Unsupported image type '{content_type}'. "
            f"Allowed: {', '.join(_ALLOWED_IMAGE_TYPES)}"
        )

    ext = _EXT_MAP[content_type]
    # Stable path per user — uploading again replaces the previous picture
    path = f"{user_id}/profile.{ext}"

    supabase = get_supabase()

    try:
        supabase.storage.from_(_PROFILE_PICS_BUCKET).upload(
            path=path,
            file=image_bytes,
            file_options={"content-type": content_type, "upsert": "true"},
        )
    except Exception as e:
        logger.error("Profile picture upload failed for user %s: %s", user_id, e)
        raise RuntimeError("Failed to upload profile picture. Please try again.") from e

    # user-profile-pics is a public bucket — build the permanent public URL
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    public_url = f"{supabase_url}/storage/v1/object/public/{_PROFILE_PICS_BUCKET}/{path}"
    return public_url
