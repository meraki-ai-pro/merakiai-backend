from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.db.supabase import get_supabase, get_user_client

security = HTTPBearer()


def _validate_token(credentials: HTTPAuthorizationCredentials):
    token = credentials.credentials
    try:
        user_response = get_supabase().auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    if not user_response or not getattr(user_response, "user", None):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return user_response.user


async def admin_guard(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    user = _validate_token(credentials)
    profile = (
        get_user_client(token)
        .table("users")
        .select("id, role")
        .eq("id", user.id)
        .single()
        .execute()
        .data
    )
    if not profile:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User profile not found")
    if profile["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return {"id": user.id, "role": profile["role"], "email": user.email, "token": token}


async def auth_guard(credentials: HTTPAuthorizationCredentials = Depends(security)):
    user = _validate_token(credentials)
    return {"id": user.id, "email": user.email, "token": credentials.credentials}
