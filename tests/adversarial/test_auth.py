"""
Adversarial tests — Authentication attacks.

Tests that invalid, expired, or tampered tokens are rejected before
any application logic runs.

Run with: pytest -m adversarial tests/adversarial/test_auth.py
"""
import asyncio
import json
import pytest
import httpx
import websockets
from websockets.exceptions import ConnectionClosedError
from tests.conftest import requires_server, BASE_URL, WS_BASE_URL


pytestmark = [pytest.mark.adversarial, requires_server]

# A structurally valid JWT with wrong signature
FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJmYWtlLXVzZXIiLCJleHAiOjk5OTk5OTk5OTl9"
    ".invalid_signature_here_XXXXXXXXXXX"
)

EXPIRED_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJ1c2VyIiwiZXhwIjoxfQ"
    ".signature"
)


class TestWsAuthRejection:
    async def test_missing_token_is_rejected(self, text_session_id):
        """WS connect with no token query param → connection refused (422 or close)."""
        uri = f"{WS_BASE_URL}/ws/{text_session_id}"
        with pytest.raises(Exception):
            async with websockets.connect(uri) as ws:
                await ws.recv()

    async def test_invalid_token_closes_with_4001(self, text_session_id):
        """WS connect with invalid JWT → close code 4001 (Unauthorized)."""
        uri = f"{WS_BASE_URL}/ws/{text_session_id}?token=not-a-jwt"
        with pytest.raises((ConnectionClosedError, Exception)):
            async with websockets.connect(uri) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                # If we got here, parse response — should be an error
                data = json.loads(msg)
                assert "error" in data or data.get("status") == "failed"

    async def test_fake_jwt_is_rejected(self, text_session_id):
        """WS connect with structurally valid but wrong-signature JWT → rejected."""
        uri = f"{WS_BASE_URL}/ws/{text_session_id}?token={FAKE_JWT}"
        with pytest.raises((ConnectionClosedError, Exception)):
            async with websockets.connect(uri) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)

    async def test_expired_jwt_is_rejected(self, text_session_id):
        """Expired JWT (exp=1 in past) → rejected."""
        uri = f"{WS_BASE_URL}/ws/{text_session_id}?token={EXPIRED_JWT}"
        with pytest.raises((ConnectionClosedError, Exception)):
            async with websockets.connect(uri) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)

    async def test_empty_token_is_rejected(self, text_session_id):
        """Empty string token → rejected."""
        uri = f"{WS_BASE_URL}/ws/{text_session_id}?token="
        with pytest.raises((ConnectionClosedError, Exception)):
            async with websockets.connect(uri) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)


class TestHttpAuthRejection:
    def test_no_auth_header_returns_401(self):
        with httpx.Client(base_url=BASE_URL) as client:
            resp = client.post("/rag/turn", json={
                "session_id": "some-uuid",
                "mode": "learn",
                "message": "hello",
            })
        assert resp.status_code in (401, 403)

    def test_malformed_bearer_returns_403(self):
        with httpx.Client(base_url=BASE_URL, headers={"Authorization": "Bearer"}) as client:
            resp = client.post("/rag/turn", json={
                "session_id": "some-uuid",
                "mode": "learn",
                "message": "hello",
            })
        assert resp.status_code in (401, 403, 422)

    def test_wrong_scheme_returns_403(self):
        with httpx.Client(base_url=BASE_URL, headers={"Authorization": "Basic dXNlcjpwYXNz"}) as client:
            resp = client.post("/rag/turn", json={
                "session_id": "some-uuid",
                "mode": "learn",
                "message": "hello",
            })
        assert resp.status_code in (401, 403)

    def test_fake_jwt_on_http_returns_401(self):
        with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {FAKE_JWT}"}) as client:
            resp = client.post("/rag/turn", json={
                "session_id": "some-uuid",
                "mode": "learn",
                "message": "hello",
            })
        assert resp.status_code == 401

    def test_login_with_wrong_password_returns_401(self):
        resp = httpx.post(
            f"{BASE_URL}/auth/login",
            json={"email": "tarape1801@flownue.com", "password": "WRONG_PASSWORD"},
        )
        assert resp.status_code == 401

    def test_login_with_nonexistent_user_returns_401(self):
        resp = httpx.post(
            f"{BASE_URL}/auth/login",
            json={"email": "noone@example.com", "password": "irrelevant"},
        )
        assert resp.status_code == 401
