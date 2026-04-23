from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.auth import admin_guard
from app.db.supabase import get_supabase

router = APIRouter(prefix="/users", tags=["Admin – Users"])

_ROLE_HIERARCHY = {"user": 0, "admin": 1, "super_admin": 2}


class RoleUpdatePayload(BaseModel):
    role: str


@router.get("")
def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    role: str | None = None,
    search: str | None = None,
    _user=Depends(admin_guard),
):
    sb = get_supabase()
    offset = (page - 1) * page_size

    q = sb.table("users").select(
        "id, email, role, first_name, last_name, university_name, country, created_at",
        count="exact",
    )
    if role:
        q = q.eq("role", role)
    if search:
        q = q.or_(f"email.ilike.%{search}%,first_name.ilike.%{search}%,last_name.ilike.%{search}%")

    res = q.order("created_at", desc=True).range(offset, offset + page_size - 1).execute()
    return {
        "users": res.data or [],
        "total": res.count or 0,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{user_id}")
def get_user_detail(user_id: str, _user=Depends(admin_guard)):
    sb = get_supabase()

    profile = sb.table("users").select("*").eq("id", user_id).single().execute().data
    if not profile:
        raise HTTPException(status_code=404, detail="User not found")

    sessions_res = sb.table("sessions").select("id", count="exact").eq("user_id", user_id).limit(0).execute()
    conversations_res = sb.table("conversations").select("id", count="exact").eq("user_id", user_id).limit(0).execute()
    reviews_res = sb.table("review_summaries").select("overall_score").eq("user_id", user_id).execute()
    logins_res = sb.table("platform_sessions").select("login_at").eq("user_id", user_id).order("login_at", desc=True).limit(1).execute()
    feedback_res = sb.table("user_feedback").select("feedback_type, message, created_at").eq("user_id", user_id).order("created_at", desc=True).execute()

    scores = [r["overall_score"] for r in (reviews_res.data or []) if r["overall_score"] is not None]
    avg_score = round(sum(scores) / len(scores), 2) if scores else None

    return {
        "profile": profile,
        "stats": {
            "total_sessions": sessions_res.count or 0,
            "total_conversations": conversations_res.count or 0,
            "total_reviews": len(reviews_res.data or []),
            "avg_review_score": avg_score,
            "last_login": logins_res.data[0]["login_at"] if logins_res.data else None,
        },
        "recent_feedback": feedback_res.data or [],
    }


@router.patch("/{user_id}/role")
def update_user_role(user_id: str, payload: RoleUpdatePayload, user=Depends(admin_guard)):
    new_role = payload.role
    if new_role not in _ROLE_HIERARCHY:
        raise HTTPException(status_code=400, detail=f"Invalid role: {new_role!r}")

    # Admins can only assign up to 'admin'; super_admins can assign any role
    if _ROLE_HIERARCHY.get(new_role, -1) > _ROLE_HIERARCHY.get(user["role"], -1):
        raise HTTPException(status_code=403, detail="Cannot assign a role higher than your own")

    # Prevent self-demotion
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    sb = get_supabase()
    res = sb.table("users").update({"role": new_role}).eq("id", user_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "ok", "user_id": user_id, "new_role": new_role}


@router.delete("/{user_id}")
def delete_user(user_id: str, user=Depends(admin_guard)):
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="Only super admins can delete users")
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    sb = get_supabase()
    # Deleting from auth.users cascades to public.users via FK
    sb.auth.admin.delete_user(user_id)
    return {"status": "ok", "deleted_user_id": user_id}
