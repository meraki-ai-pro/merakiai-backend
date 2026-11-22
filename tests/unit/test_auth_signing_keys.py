from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import ec
import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core import auth


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_validate_token_accepts_supabase_es256_signing_keys(monkeypatch):
    supabase_url = "https://uat-project.supabase.co"
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": "user-123",
            "email": "student@example.test",
            "aud": "authenticated",
            "iss": f"{supabase_url}/auth/v1",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "aal": "aal1",
        },
        private_key,
        algorithm="ES256",
        headers={"kid": "uat-key"},
    )

    monkeypatch.setenv("SUPABASE_URL", supabase_url)
    monkeypatch.setattr(
        auth,
        "_get_jwks_client",
        lambda _url: SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)
        ),
    )

    user = auth._validate_token(_credentials(token))

    assert user.id == "user-123"
    assert user.email == "student@example.test"


def test_validate_token_keeps_legacy_hs256_compatibility(monkeypatch):
    secret = "legacy-test-secret-with-enough-entropy"
    token = jwt.encode(
        {"sub": "legacy-user", "email": "legacy@example.test"},
        secret,
        algorithm="HS256",
    )
    monkeypatch.setenv("SUPABASE_JWT_SECRET", secret)

    user = auth._validate_token(_credentials(token))

    assert user.id == "legacy-user"


def test_validate_token_rejects_unapproved_algorithms(monkeypatch):
    token = jwt.encode(
        {"sub": "user-123"},
        "test-secret-with-enough-entropy-for-sha384-validation-only",
        algorithm="HS384",
    )

    with pytest.raises(HTTPException) as exc:
        auth._validate_token(_credentials(token))

    assert exc.value.status_code == 401
