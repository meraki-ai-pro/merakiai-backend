import asyncio
import logging
import os
from types import SimpleNamespace

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt as _jwt
from jwt import InvalidTokenError

from app.db.supabase import get_supabase, get_user_client

logger = logging.getLogger(__name__)

security = HTTPBearer()

# Algorithm is pinned to HS256 — never allow "none" or RS256 confusion attacks.
_ALGORITHM = "HS256"


def _validate_token(
    credentials: HTTPAuthorizationCredentials,
    *,
    allow_password_change: bool = False,
):
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
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id: str | None = payload.get("sub")
    user_email: str = payload.get("email", "")
    # Authenticator Assurance Level: "aal2" once the user has passed MFA this
    # session, "aal1" otherwise. Surfaced so endpoints can require step-up.
    aal: str = payload.get("aal", "aal1")
    user_metadata = payload.get("user_metadata") or {}
    must_change_password = bool(user_metadata.get("must_change_password"))

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    if must_change_password and not allow_password_change:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must change your temporary password before continuing.",
        )

    return SimpleNamespace(
        id=user_id,
        email=user_email,
        aal=aal,
        must_change_password=must_change_password,
    )


# Roles carrying platform-wide authority. A lecturer is deliberately absent:
# lecturer authority is scoped to owned courses, never platform-wide.
ADMIN_ROLES = ("admin", "super_admin")

# Roles that may reach lecturer-scoped endpoints at all. Admins are included so
# they can support a lecturer without a role swap, but every course-scoped
# route must still pass through assert_course_owner().
LECTURER_ROLES = ("lecturer", "admin", "super_admin")


def _fetch_role(token: str, user_id: str) -> str:
    """Read the caller's role through their own JWT, so RLS still applies."""
    profile = (
        get_user_client(token)
        .table("users")
        .select("id, role")
        .eq("id", user_id)
        .single()
        .execute()
        .data
    )
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User profile not found"
        )
    return profile["role"]


async def admin_guard(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    user = _validate_token(credentials)
    role = _fetch_role(token, user.id)
    if role not in ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return {"id": user.id, "role": role, "email": user.email, "token": token}


async def lecturer_guard(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Gate for /lecturer routes. Says nothing about *which* courses.

    Passing this guard only proves the caller is a lecturer (or an admin acting
    on their behalf). Any route touching a specific course must additionally
    call assert_course_owner() — see Lecturer Side Technical Documentation §6.
    """
    token = credentials.credentials
    user = _validate_token(credentials)
    role = _fetch_role(token, user.id)
    if role not in LECTURER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Instructor privileges required"
        )
    return {"id": user.id, "role": role, "email": user.email, "token": token}


def assert_course_owner(user: dict, course_id: str) -> None:
    """Raise unless the caller owns this course (admins override).

    Reads through the service-role client on purpose: ownership must be
    answerable even when the caller's own RLS view of `courses` is restricted,
    and the question asked is narrow enough to leak nothing.

    A missing course and an unowned course both raise 404. Returning 403 for
    "exists but is not yours" would let a lecturer enumerate other lecturers'
    course ids by probing.
    """
    if user.get("role") in ADMIN_ROLES:
        return

    course = (
        get_supabase()
        .table("courses")
        .select("id, owner_id")
        .eq("id", course_id)
        .execute()
        .data
    )
    if not course or course[0].get("owner_id") != user["id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")


async def course_owner_guard(course_id: str, user=Depends(lecturer_guard)):
    """Drop-in dependency for any route whose path contains ``{course_id}``.

    FastAPI resolves ``course_id`` from the path, so this composes as
    ``Depends(course_owner_guard)`` without repeating the ownership call.
    """
    assert_course_owner(user, course_id)
    return user


async def auth_guard(credentials: HTTPAuthorizationCredentials = Depends(security)):
    user = _validate_token(credentials)
    return {
        "id": user.id,
        "email": user.email,
        "token": credentials.credentials,
        "aal": user.aal,
    }


async def password_change_guard(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Authenticate while allowing an account marked for a mandatory reset.

    This dependency is intentionally limited to the profile read and password
    change flow. All normal guards reject the same token until the flag clears.
    """
    user = _validate_token(credentials, allow_password_change=True)
    return {
        "id": user.id,
        "email": user.email,
        "token": credentials.credentials,
        "aal": user.aal,
        "must_change_password": user.must_change_password,
    }


def _user_has_verified_mfa(user_id: str) -> bool:
    """Return True if the user has a verified TOTP factor.

    Uses the Supabase admin API (service role). On lookup failure we fail
    closed with a temporary-service error: silently treating an unknown MFA
    state as "not enrolled" would let a sensitive action bypass step-up auth.
    """
    try:
        resp = get_supabase().auth.admin.get_user_by_id(user_id)
        factors = getattr(resp.user, "factors", None) or []
        return any(
            getattr(f, "factor_type", None) == "totp"
            and getattr(f, "status", None) == "verified"
            for f in factors
        )
    except Exception as exc:  # noqa: BLE001 — external identity-provider boundary
        logger.exception("MFA factor lookup failed for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to verify multi-factor authentication status. Please try again.",
        ) from exc


async def require_mfa_if_enrolled(user=Depends(auth_guard)):
    """Dependency for sensitive endpoints (password change, admin mutations).

    Enforces MFA *only for users who have enrolled it* (mirrors Mike's
    requireMfaIfEnrolled): users without MFA pass through unchanged; users with
    a verified TOTP factor must present an MFA-verified (aal2) session. This
    makes MFA meaningful server-side rather than a login-UI gate that a raw
    aal1 token could bypass.
    """
    if user.get("aal") == "aal2":
        return user
    has_mfa = await asyncio.to_thread(_user_has_verified_mfa, user["id"])
    if has_mfa:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This action requires two-factor verification. "
                "Please re-authenticate with your authenticator app."
            ),
        )
    return user


async def require_mfa_for_password_change(user=Depends(password_change_guard)):
    """MFA check for the one flow mandatory-reset accounts may access."""
    if user.get("aal") == "aal2":
        return user
    has_mfa = await asyncio.to_thread(_user_has_verified_mfa, user["id"])
    if has_mfa:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This action requires two-factor verification. "
                "Please re-authenticate with your authenticator app."
            ),
        )
    return user
