import logging
import os
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.auth import auth_guard, require_mfa_if_enrolled, security
from app.core.rate_limit import rate_limit
from app.db.supabase import get_supabase, get_supabase_anon
from app.models.models import (
    ForgotPasswordPayload,
    LoginPayload,
    ResetPasswordPayload,
    SignUpPayload,
    UpdatePasswordPayload,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth (Supabase)"])

PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "http://localhost:3001")
recovery_security = HTTPBearer()


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

        # A lecturer who imported a class list before the cohort registered has
        # pending invitations waiting on this address. Converting them here is
        # what makes roster import work at the start of a semester, when almost
        # nobody on the list has an account yet.
        try:
            get_supabase().rpc(
                "accept_enrolment_invitations",
                {"p_user_id": user.id, "p_email": user.email},
            ).execute()
        except Exception as e:  # noqa: BLE001 — sql/013 may not be applied yet
            logger.warning(
                "Could not accept pending enrolment invitations for %s: %s", user.email, e
            )

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


@router.post("/reset-password")
def reset_password(
    payload: ResetPasswordPayload,
    credentials: HTTPAuthorizationCredentials = Depends(recovery_security),
    _rl=Depends(rate_limit(max_calls=5, window_seconds=60)),
):
    """Set a new password for the user in a valid Supabase session."""
    try:
        response = get_supabase_anon().auth.get_user(credentials.credentials)
        user = getattr(response, "user", None)
        user_id = getattr(user, "id", None)
        if not user_id:
            raise ValueError("Recovery session has no user")

        get_supabase().auth.admin.update_user_by_id(
            user_id,
            {"password": payload.new_password},
        )
    except Exception as e:
        logger.warning("Password recovery failed: %s", e)
        raise HTTPException(
            status_code=401,
            detail="This password reset link is invalid or has expired. Please request a new one.",
        )

    return {"status": "ok", "message": "Password updated successfully."}


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

    # The other account-creation path, so it needs the same invitation
    # conversion as /signup. Idempotent: an account with nothing pending
    # returns 0 and writes nothing.
    if getattr(user, "id", None):
        try:
            # public.users first. enrolments.student_id is a foreign key to it,
            # and on this path the row is created by a trigger whose ordering
            # we do not control — accepting an invitation before it exists
            # would fail and leave an imported student off their course. Only
            # id and email are written, so a trigger that already filled in
            # names keeps them.
            get_supabase().table("users").upsert(
                {"id": user.id, "email": getattr(user, "email", None)},
                on_conflict="id",
            ).execute()
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not ensure the profile row after OAuth: %s", e)

        try:
            get_supabase().rpc(
                "accept_enrolment_invitations",
                {"p_user_id": user.id, "p_email": getattr(user, "email", "") or ""},
            ).execute()
        except Exception as e:  # noqa: BLE001 — sql/013 may not be applied yet
            logger.warning("Could not accept pending invitations after OAuth: %s", e)

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
    _rl=Depends(rate_limit(max_calls=5, window_seconds=300)),
):
    """Change your own password. Requires the current one.

    Re-authenticating with the current password is what stops a leaked access
    token being enough to take an account over: without it, anyone holding a
    token for the next hour could set a password the real owner does not know
    and keep the account for good.

    Rate-limited tightly because this endpoint is also an oracle for guessing
    the current password.
    """
    if not payload.new_password or len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if payload.new_password == payload.current_password:
        raise HTTPException(
            status_code=400, detail="The new password must be different from the current one."
        )

    email = user.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="This account has no email to verify against.")

    try:
        get_supabase_anon().auth.sign_in_with_password({
            "email": email,
            "password": payload.current_password,
        })
    except Exception:  # noqa: BLE001 — any failure here means "wrong password"
        raise HTTPException(status_code=401, detail="Your current password is incorrect.")

    try:
        get_supabase().auth.admin.update_user_by_id(user["id"], {"password": payload.new_password})
    except Exception as e:
        logger.error("Password update failed for user_id=%s: %s", user["id"], e)
        raise HTTPException(status_code=400, detail="Password update failed. Please try again.")
    return {"status": "ok"}
