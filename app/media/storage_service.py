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


# ── Private buckets ────────────────────────────────────────────────────────
# Unlike the three buckets above these are NOT public. Course documents are the
# lecturer's copyrighted material; student uploads are photographs of a named
# person's work. Both are reached only through short-lived signed URLs.
COURSE_DOCS_BUCKET = "course-documents"
STUDENT_UPLOADS_BUCKET = "student-uploads"
RENDERED_MEDIA_BUCKET = "rendered-media"

# Long enough for a lecturer to open a file from the dashboard, short enough
# that a leaked URL in a browser history or proxy log expires quickly.
_SIGNED_URL_TTL_SECONDS = 60 * 15


def _safe_segment(value: str) -> str:
    """Make a string safe as a single storage path segment.

    Filenames arrive from uploads, so they can contain '../', control
    characters or separators. Anything outside the allowlist becomes '_', which
    keeps the path one segment deep and traversal-free.
    """
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in (value or ""))
    cleaned = cleaned.lstrip(".") or "file"
    return cleaned[:120]


def upload_course_document(
    course_id: str, document_id: str, filename: str, content: bytes, content_type: str
) -> str | None:
    """Retain the original upload so it can be re-ingested or previewed later.

    Returns the storage path, or None if the upload failed. Failure is not
    fatal on purpose: the vectors are what serve students, and losing the
    archive copy must not fail an otherwise good ingestion. Same reasoning as
    _store_chunk_rows in the ingestion service.
    """
    path = f"{_safe_segment(course_id)}/{_safe_segment(document_id)}/{_safe_segment(filename)}"

    try:
        get_supabase().storage.from_(COURSE_DOCS_BUCKET).upload(
            path=path,
            file=content,
            file_options={"content-type": content_type, "upsert": "true"},
        )
    except Exception as exc:  # noqa: BLE001 — archival is best-effort
        logger.warning(
            "Could not retain source file for document %s; ingestion continues "
            "but re-ingestion will need the original: %s",
            document_id, exc,
        )
        return None

    return path


def upload_student_image(
    user_id: str, session_id: str, image_bytes: bytes, media_type: str
) -> str | None:
    """Keep a student's submitted photo so the turn still makes sense on reload."""
    ext = media_type.rsplit("/", 1)[-1]
    path = f"{_safe_segment(user_id)}/{_safe_segment(session_id)}/{uuid.uuid4()}.{ext}"

    try:
        get_supabase().storage.from_(STUDENT_UPLOADS_BUCKET).upload(
            path=path,
            file=image_bytes,
            file_options={"content-type": media_type, "upsert": "false"},
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("Could not retain student image for session %s: %s", session_id, exc)
        return None

    return path


def upload_rendered_media(
    course_id: str, asset_id: str, content: bytes, media_type: str, extension: str
) -> str | None:
    """Store a rendered artefact (Manim/Remotion output).

    Keyed by asset id, and upsert=true, so re-running a failed render replaces
    the partial output rather than accumulating orphans.
    """
    path = f"{_safe_segment(course_id)}/{_safe_segment(asset_id)}.{_safe_segment(extension)}"

    try:
        get_supabase().storage.from_(RENDERED_MEDIA_BUCKET).upload(
            path=path,
            file=content,
            file_options={"content-type": media_type, "upsert": "true"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Rendered media upload failed for asset %s: %s", asset_id, exc)
        return None

    return path


def signed_url(bucket: str, path: str, ttl_seconds: int = _SIGNED_URL_TTL_SECONDS) -> str | None:
    """Mint a time-limited URL for a private object."""
    if not path:
        return None
    try:
        signed = get_supabase().storage.from_(bucket).create_signed_url(path, ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not sign %s/%s: %s", bucket, path, exc)
        return None
    return signed.get("signedURL") or signed.get("signedUrl")


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
