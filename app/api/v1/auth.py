import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core.auth import auth_guard, security
from app.db.supabase import get_supabase, get_supabase_anon
from app.models.models import ForgotPasswordPayload, LoginPayload, SignUpPayload, UpdatePasswordPayload

router = APIRouter(prefix="/auth", tags=["Auth (Supabase)"])

PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "http://127.0.0.1:8000")


@router.post("/signup")
def signup(payload: SignUpPayload):
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
        raise HTTPException(status_code=400, detail=str(e))

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
            print(f"Error upserting public.users: {e}")

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
def login(payload: LoginPayload):
    from app.core.analytics import log_login

    try:
        res = get_supabase_anon().auth.sign_in_with_password({
            "email": payload.email,
            "password": payload.password,
        })
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

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
def forgot_password(payload: ForgotPasswordPayload):
    redirect_to = payload.redirect_to or f"{PUBLIC_SITE_URL}/auth/reset-password"
    try:
        get_supabase_anon().auth.reset_password_for_email(payload.email, {"redirect_to": redirect_to})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "message": "Password reset email sent", "redirect_to": redirect_to}


@router.get("/google/url")
def google_login_url(redirect_to: str | None = None):
    redirect_to = redirect_to or f"{PUBLIC_SITE_URL}/auth/google/callback"
    try:
        res = get_supabase_anon().auth.sign_in_with_oauth({
            "provider": "google",
            "options": {"redirect_to": redirect_to},
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
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
        raise HTTPException(status_code=400, detail=str(e))

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
def refresh_token(body: dict):
    refresh = (body or {}).get("refresh_token", "").strip()
    if not refresh:
        raise HTTPException(status_code=400, detail="refresh_token is required")
    try:
        res = get_supabase_anon().auth.refresh_session(refresh)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Could not refresh session: {e}")

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
def update_password(payload: UpdatePasswordPayload, user=Depends(auth_guard)):
    if not payload.new_password or len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    try:
        get_supabase().auth.admin.update_user_by_id(user["id"], {"password": payload.new_password})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Password update failed: {e}")
    return {"status": "ok"}
