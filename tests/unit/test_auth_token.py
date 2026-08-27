from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core.auth import _validate_token


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_validate_token_accepts_a_pinned_hs256_supabase_token(monkeypatch):
    secret = "unit-test-secret-with-enough-entropy"
    monkeypatch.setenv("SUPABASE_JWT_SECRET", secret)
    token = jwt.encode(
        {
            "sub": "user-123",
            "email": "student@example.com",
            "aal": "aal2",
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        secret,
        algorithm="HS256",
    )

    user = _validate_token(_credentials(token))

    assert user.id == "user-123"
    assert user.email == "student@example.com"
    assert user.aal == "aal2"


def test_validate_token_rejects_an_expired_token(monkeypatch):
    secret = "unit-test-secret-with-enough-entropy"
    monkeypatch.setenv("SUPABASE_JWT_SECRET", secret)
    token = jwt.encode(
        {
            "sub": "user-123",
            "exp": datetime.now(UTC) - timedelta(minutes=1),
        },
        secret,
        algorithm="HS256",
    )

    with pytest.raises(HTTPException) as exc_info:
        _validate_token(_credentials(token))

    assert exc_info.value.status_code == 401


def test_validate_token_fails_closed_without_a_server_secret(monkeypatch):
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        _validate_token(_credentials("not-a-token"))

    assert exc_info.value.status_code == 500
