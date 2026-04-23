from fastapi import APIRouter, Depends, HTTPException
from app.core.auth import auth_guard
from app.db.supabase import get_user_client
from app.models.models import AvatarSelectRequest

router = APIRouter(prefix="/users", tags=["Users"])

@router.get("/me")
def get_me(user=Depends(auth_guard)):
    supabase = get_user_client(user["token"])
    res = supabase.table("users").select("*").eq("id", user["id"]).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Profile not found")
    row = res.data[0]
    return {
        "id": row["id"],
        "email": row["email"],
        "role": row["role"],
        "avatar_id": row["avatar_id"],
        "avatar_provider": row["avatar_provider"],
        "avatar_gender": row.get("avatar_gender"),
        "voice_provider": row.get("voice_provider"),
        "voice_id": row.get("voice_id"),
        "voice_gender": row.get("voice_gender"),
        "created_at": row.get("created_at"),
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
