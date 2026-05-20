import logging
import os
from types import SimpleNamespace

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt as _jwt

from app.db.supabase import get_user_client

logger = logging.getLogger(__name__)

security = HTTPBearer()

# Algorithm is pinned to HS256 — never allow "none" or RS256 confusion attacks.
_ALGORITHM = "HS256"


def _validate_token(credentials: HTTPAuthorizationCredentials):
    """Validate a Supabase JWT locally — no network round-trip required.

    Requires SUPABASE_JWT_SECRET (found in Supabase Dashboard → Settings → API
    → JWT Secret) to be present in the environment.
    """
    token = credentials.credentials
    secret = os.getenv("SUPABASE_JWT_SECRET", "")

    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server authentication is not properly configured.",
        )

    try:
        payload = _jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            options={"verify_aud": False},  # Supabase sets aud="authenticated"
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id: str | None = payload.get("sub")
    user_email: str = payload.get("email", "")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    return SimpleNamespace(id=user_id, email=user_email)


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
