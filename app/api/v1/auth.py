import logging
import os
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core.auth import auth_guard, require_mfa_if_enrolled, security
from app.core.rate_limit import rate_limit
from app.db.supabase import get_supabase, get_supabase_anon
from app.models.models import ForgotPasswordPayload, LoginPayload, SignUpPayload, UpdatePasswordPayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth (Supabase)"])

PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "http://127.0.0.1:8000")


def _validate_redirect_url(redirect_to: str | None, fallback: str) -> str:
    """Allow redirect_to only if its origin is in ALLOWED_ORIGINS.

    Prevents open-redirect attacks on password-reset and OAuth flows.
    """
    if not redirect_to:
        return fallback

    raw_origins = os.getenv("ALLOWED_ORIGINS", PUBLIC_SITE_URL)
    allowed = {o.strip().rstrip("/") for o in raw_origins.split(",") if o.strip()}

    try:
        parsed = urlparse(redirect_to)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in allowed:
            return redirect_to
    except Exception:
        pass

    logger.warning("Blocked open-redirect attempt to disallowed origin: %s", redirect_to)
    return fallback


@router.post("/signup")
def signup(payload: SignUpPayload, _rl=Depends(rate_limit(max_calls=5, window_seconds=60))):
    anon = get_supabase_anon()
    try:
        res = anon.auth.sign_up({
            "email": payload.email,
            "password": payload.password,
            "options": {
                "data": {
                    "first_name": payload.first_name,
                    "last_name": payload.last_name,
                    "university_name": payload.university_name,
                    "region": payload.region,
                    "country": payload.country,
                }
            },
        })
    except Exception as e:
        logger.error("Signup error: %s", e)
        raise HTTPException(status_code=400, detail="Registration failed. Please try again.")

    session = getattr(res, "session", None)
    user = getattr(res, "user", None)

    if user and user.id:
        try:
            # Service role needed here: when email confirmation is on, the anon
            # client has no live JWT at this point so RLS would block the insert.
            # avatar_id/voice_id are omitted — set later via POST /users/avatar.
            get_supabase().table("users").upsert(
                {
                    "id": user.id,
                    "email": user.email,
                    "first_name": payload.first_name,
                    "last_name": payload.last_name,
                    "university_name": payload.university_name,
                    "region": payload.region,
                    "country": payload.country,
                },
                on_conflict="id",
            ).execute()
        except Exception as e:
            logger.error("Error upserting public.users for user_id=%s: %s", user.id, e)

    return {
        "user": {
            "id": getattr(user, "id", None),
            "email": getattr(user, "email", None),
        },
        "session": {
            "access_token": getattr(session, "access_token", None),
            "refresh_token": getattr(session, "refresh_token", None),
            "expires_in": getattr(session, "expires_in", None),
        },
        "note": "If access_token is null, email confirmation is required in Supabase settings.",
    }


@router.post("/login")
def login(payload: LoginPayload, _rl=Depends(rate_limit(max_calls=10, window_seconds=60))):
    from app.core.analytics import log_login

    try:
        res = get_supabase_anon().auth.sign_in_with_password({
            "email": payload.email,
            "password": payload.password,
        })
    except Exception as e:
        logger.warning("Login failed for email=%s: %s", payload.email, e)
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    try:
        log_login(str(res.user.id))
    except Exception:
        pass

    return {
        "user": {"id": res.user.id, "email": res.user.email},
        "access_token": res.session.access_token,
        "refresh_token": res.session.refresh_token,
        "expires_in": res.session.expires_in,
        "token_type": "bearer",
    }


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordPayload, _rl=Depends(rate_limit(max_calls=3, window_seconds=60))):
    # H-3: validate redirect_to against allowed origins before passing to Supabase
    redirect_to = _validate_redirect_url(
        str(payload.redirect_to) if payload.redirect_to else None,
        fallback=f"{PUBLIC_SITE_URL}/auth/reset-password",
    )
    try:
        get_supabase_anon().auth.reset_password_for_email(payload.email, {"redirect_to": redirect_to})
    except Exception as e:
        logger.error("Forgot-password error for email=%s: %s", payload.email, e)
        raise HTTPException(status_code=400, detail="If that email is registered, a reset link has been sent.")
    return {"status": "ok", "message": "If that email is registered, a reset link has been sent."}


@router.get("/google/url")
def google_login_url(redirect_to: str | None = None):
    # H-3: validate redirect_to against allowed origins before passing to Supabase
    safe_redirect = _validate_redirect_url(
        redirect_to,
        fallback=f"{PUBLIC_SITE_URL}/auth/google/callback",
    )
    try:
        res = get_supabase_anon().auth.sign_in_with_oauth({
            "provider": "google",
            "options": {"redirect_to": safe_redirect},
        })
    except Exception as e:
        logger.error("Google OAuth URL error: %s", e)
        raise HTTPException(status_code=400, detail="Could not initiate OAuth flow. Please try again.")
    url = getattr(res, "url", None) or getattr(res, "data", {}).get("url")
    return {"url": url}


@router.get("/google/callback")
def google_callback(code: str | None = None):
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")
    try:
        try:
            res = get_supabase_anon().auth.exchange_code_for_session(code)
        except TypeError:
            res = get_supabase_anon().auth.exchange_code_for_session({"auth_code": code})
    except Exception as e:
        logger.error("Google OAuth callback error: %s", e)
        raise HTTPException(status_code=400, detail="OAuth authentication failed. Please try again.")

    session = getattr(res, "session", None)
    user = getattr(res, "user", None)
    return {
        "user": {"id": getattr(user, "id", None), "email": getattr(user, "email", None)},
        "session": {
            "access_token": getattr(session, "access_token", None),
            "refresh_token": getattr(session, "refresh_token", None),
            "expires_in": getattr(session, "expires_in", None),
        },
    }


@router.post("/refresh")
def refresh_token(body: dict, _rl=Depends(rate_limit(max_calls=10, window_seconds=60))):
    refresh = (body or {}).get("refresh_token", "").strip()
    if not refresh:
        raise HTTPException(status_code=400, detail="refresh_token is required")
    try:
        res = get_supabase_anon().auth.refresh_session(refresh)
    except Exception as e:
        logger.warning("Token refresh failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token.")

    session = getattr(res, "session", None)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    return {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expires_in": session.expires_in,
        "token_type": "bearer",
    }


@router.post("/logout")
def logout(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    user=Depends(auth_guard),
):
    try:
        get_supabase().auth.admin.sign_out(credentials.credentials)
    except Exception:
        pass  # already expired — treat as success
    return {"status": "ok"}


@router.post("/update-password")
def update_password(
    payload: UpdatePasswordPayload,
    user=Depends(require_mfa_if_enrolled),
):
    if not payload.new_password or len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    try:
        get_supabase().auth.admin.update_user_by_id(user["id"], {"password": payload.new_password})
    except Exception as e:
        logger.error("Password update failed for user_id=%s: %s", user["id"], e)
        raise HTTPException(status_code=400, detail="Password update failed. Please try again.")
    return {"status": "ok"}
