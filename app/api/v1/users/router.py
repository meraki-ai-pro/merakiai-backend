from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from app.core.auth import auth_guard, password_change_guard
from app.db.supabase import get_user_client
from app.models.models import AvatarSelectRequest, UpdateProfilePayload

router = APIRouter(prefix="/users", tags=["Users"])

_MAX_PROFILE_PIC_BYTES = 5 * 1024 * 1024  # 5 MB
_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _profile_view(row: dict) -> dict:
    """The profile shape every caller gets.

    Whitelisted rather than returning the row: `users` also carries deletion
    lifecycle columns and internal flags that the client has no use for and
    that would become an accidental API contract the moment they were shipped.
    """
    return {
        "id": row["id"],
        "email": row["email"],
        "role": row["role"],
        "first_name": row.get("first_name"),
        "last_name": row.get("last_name"),
        "university_name": row.get("university_name"),
        "region": row.get("region"),
        "country": row.get("country"),
        "avatar_id": row.get("avatar_id"),
        "avatar_provider": row.get("avatar_provider"),
        "avatar_gender": row.get("avatar_gender"),
        "voice_provider": row.get("voice_provider"),
        "voice_id": row.get("voice_id"),
        "voice_gender": row.get("voice_gender"),
        "profile_picture_url": row.get("profile_picture_url"),
        "created_at": row.get("created_at"),
    }


@router.get("/me")
def get_me(user=Depends(password_change_guard)):
    supabase = get_user_client(user["token"])
    res = supabase.table("users").select("*").eq("id", user["id"]).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _profile_view(res.data[0])


@router.patch("/me")
def update_me(payload: UpdateProfilePayload, user=Depends(auth_guard)):
    """Edit your own profile.

    Written through the caller's own JWT, so RLS is the enforcement — the
    service-role client would happily update any row if an id were ever
    threaded through from a payload.
    """
    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    supabase = get_user_client(user["token"])
    res = supabase.table("users").update(updates).eq("id", user["id"]).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"status": "ok", "profile": _profile_view(res.data[0])}


@router.post("/me/profile-picture")
async def upload_profile_picture(
    file: UploadFile = File(...),
    user=Depends(auth_guard),
):
    """
    Upload or replace the authenticated user's profile picture.
    Accepted formats: JPEG, PNG, WebP. Max size: 5 MB.
    The image is stored in the 'user-profile-pics' Supabase Storage bucket
    at path '{user_id}/profile.{ext}' and the public URL is saved to the
    users table.
    """
    # Validate content type before reading bytes
    content_type = file.content_type or ""
    if content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported image type '{content_type}'. Allowed: JPEG, PNG, WebP.",
        )

    image_bytes = await file.read()

    if len(image_bytes) > _MAX_PROFILE_PIC_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image exceeds the 5 MB limit ({len(image_bytes) // (1024 * 1024)} MB received).",
        )

    # Magic-byte validation — content-type header alone is not trustworthy
    if content_type == "image/jpeg" and not image_bytes[:2] == b"\xff\xd8":
        raise HTTPException(status_code=415, detail="File does not appear to be a valid JPEG.")
    if content_type == "image/png" and not image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        raise HTTPException(status_code=415, detail="File does not appear to be a valid PNG.")
    if content_type == "image/webp" and not (image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP"):
        raise HTTPException(status_code=415, detail="File does not appear to be a valid WebP.")

    from app.media.storage_service import upload_profile_picture as _upload
    try:
        public_url = _upload(
            user_id=user["id"],
            image_bytes=image_bytes,
            content_type=content_type,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Persist the public URL on the user's profile
    supabase = get_user_client(user["token"])
    supabase.table("users").update(
        {"profile_picture_url": public_url}
    ).eq("id", user["id"]).execute()

    return {
        "status": "ok",
        "profile_picture_url": public_url,
    }


@router.patch("/me/avatar")
def update_avatar(payload: AvatarSelectRequest, user=Depends(auth_guard)):
    """User selects/changes avatar. Voice is bundled automatically via avatar_voice_bundles."""
    return select_avatar(payload, user)

@router.post("/avatar")
def select_avatar(payload: AvatarSelectRequest, user=Depends(auth_guard)):
    supabase = get_user_client(user["token"])

    b = (
        supabase.table("avatar_voice_bundles")
        .select("*")
        .eq("avatar_id", payload.avatar_id)
        .eq("is_active", True)
        .execute()
    )
    if not b.data:
        raise HTTPException(status_code=400, detail="Invalid avatar selection")

    bundle = b.data[0]

    supabase.table("users").update({
        "avatar_id": bundle["avatar_id"],
        "avatar_provider": bundle.get("avatar_provider", "d-id"),
        "avatar_gender": bundle["avatar_gender"],
        "voice_provider": bundle.get("voice_provider", "elevenlabs"),
        "voice_id": bundle["voice_id"],
        "voice_gender": bundle["voice_gender"],
    }).eq("id", user["id"]).execute()

    return {
        "status": "ok",
        "avatar_id": bundle["avatar_id"],
        "voice_id": bundle["voice_id"],
        "did_presenter_id": bundle["did_presenter_id"],
    }
