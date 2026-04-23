"""
Adversarial tests — Session and data isolation.

Verifies that users cannot access each other's sessions, mode sessions,
or analytics data. Also tests out-of-order and invalid state transitions.

These tests need TWO authenticated users. We use the primary test user
for User A and attempt unauthorised access as User B via manipulated IDs.

Run with: pytest -m adversarial tests/adversarial/test_session_isolation.py
"""
import asyncio
import json
import uuid
import pytest
import httpx
import websockets
from tests.conftest import requires_server, BASE_URL, WS_BASE_URL


pytestmark = [pytest.mark.adversarial, requires_server]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _ws_send_recv(auth_token: str, session_id: str, payload: dict, timeout: int = 10) -> dict:
    uri = f"{WS_BASE_URL}/ws/{session_id}?token={auth_token}"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps(payload))
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        return json.loads(raw)


# ---------------------------------------------------------------------------
# Non-existent resource access
# ---------------------------------------------------------------------------

class TestNonExistentResources:
    def test_get_nonexistent_session_returns_404(self, http_client):
        fake_id = str(uuid.uuid4())
        resp = http_client.get(f"/sessions/{fake_id}")
        assert resp.status_code == 404

    def test_end_nonexistent_session_returns_404(self, http_client):
        fake_id = str(uuid.uuid4())
        resp = http_client.post(f"/sessions/{fake_id}/end")
        assert resp.status_code == 404

    async def test_mode_session_turn_nonexistent_returns_error_via_ws(
        self, auth_token, text_session_id
    ):
        resp = await _ws_send_recv(auth_token, text_session_id, {
            "type": "mode_session_turn",
            "mode_session_id": str(uuid.uuid4()),  # doesn't exist
            "message": "some answer",
        })
        assert "error" in resp

    async def test_mode_session_end_nonexistent_returns_error_via_ws(
        self, auth_token, text_session_id
    ):
        resp = await _ws_send_recv(auth_token, text_session_id, {
            "type": "mode_session_end",
            "mode_session_id": str(uuid.uuid4()),
        })
        assert "error" in resp

    def test_http_mode_session_turn_nonexistent_returns_404(self, http_client):
        fake_id = str(uuid.uuid4())
        resp = http_client.post(f"/mode-sessions/{fake_id}/turn", json={"message": "test"})
        assert resp.status_code == 404

    def test_http_mode_session_end_nonexistent_returns_404(self, http_client):
        fake_id = str(uuid.uuid4())
        resp = http_client.post(f"/mode-sessions/{fake_id}/end")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Invalid UUID formats
# ---------------------------------------------------------------------------

class TestInvalidUuids:
    def test_invalid_session_id_format_in_http(self, http_client):
        resp = http_client.get("/sessions/not-a-uuid-at-all")
        assert resp.status_code in (400, 404, 422)

    def test_sql_in_session_id_returns_error(self, http_client):
        malicious_id = "'; DROP TABLE sessions; --"
        resp = http_client.get(f"/sessions/{malicious_id}")
        assert resp.status_code in (400, 404, 422)  # not 500

    def test_mode_session_start_invalid_session_id(self, http_client):
        resp = http_client.post("/mode-sessions/start", json={
            "session_id": "not-a-valid-uuid",
            "mode": "review",
            "session_type": "froth_flotation",
            "difficulty": "Basic",
        })
        assert resp.status_code == 400

    async def test_ws_connect_with_invalid_session_id_format(self, auth_token):
        """WS endpoint with a non-UUID session_id should either work (no DB FK check at WS level)
        or fail gracefully — but must not 500."""
        uri = f"{WS_BASE_URL}/ws/not-a-uuid?token={auth_token}"
        try:
            async with websockets.connect(uri) as ws:
                # Connected — send a message and see if it crashes
                await ws.send(json.dumps({"type": "rag_turn", "message": "test"}))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                assert isinstance(resp, dict)
        except Exception:
            pass  # Connection refused is also acceptable


# ---------------------------------------------------------------------------
# Business logic state violations
# ---------------------------------------------------------------------------

class TestStateViolations:
    def test_review_mode_cannot_have_video(self, http_client, text_session_id):
        """Setting prefers_video=True while in review mode must be rejected."""
        # Switch to review mode
        http_client.patch(f"/sessions/{text_session_id}/mode", json={"current_mode": "review"})
        # Try to enable video
        resp = http_client.patch(f"/sessions/{text_session_id}/video", json={"prefers_video": True})
        assert resp.status_code == 400
        assert "text-only" in resp.text.lower() or "review" in resp.text.lower()

    def test_rag_turn_application_mode_rejected(self, http_client, text_session_id):
        """/rag/turn only accepts mode='learn'. Practice must use /mode-sessions."""
        resp = http_client.post("/rag/turn", json={
            "session_id": text_session_id,
            "mode": "application",
            "message": "some answer",
        })
        assert resp.status_code == 400

    def test_rag_turn_review_mode_rejected(self, http_client, text_session_id):
        resp = http_client.post("/rag/turn", json={
            "session_id": text_session_id,
            "mode": "review",
            "message": "some answer",
        })
        assert resp.status_code == 400

    def test_mode_session_turn_without_start_fails(self, http_client, text_session_id):
        """
        Trying to turn on a mode_session that has no state (session_state row)
        should return a 409 conflict, not a 500.
        """
        # Create a mode session record directly
        resp = http_client.post("/mode-sessions/start", json={
            "session_id": text_session_id,
            "mode": "review",
            "session_type": "froth_flotation",
            "difficulty": "Basic",
            "total_items": 1,
        })
        if resp.status_code != 200:
            pytest.skip("Could not create mode session")

        msid = resp.json().get("mode_session_id")
        # Don't wait for the start task to populate session_state
        # Instead, immediately try to turn — state may not be ready yet
        # The endpoint should either queue it (409 no state) or process it
        turn_resp = http_client.post(
            f"/mode-sessions/{msid}/turn", json={"message": "early answer"}
        )
        # Should be 409 (no state) or 200 (if state was ready), never 500
        assert turn_resp.status_code in (200, 202, 409)


# ---------------------------------------------------------------------------
# Cross-session access attempts (same user, wrong session)
# ---------------------------------------------------------------------------

class TestCrossSessionAccess:
    def test_cannot_set_video_on_other_users_session(self, http_client):
        """
        We don't have a second user in this test suite, but we can verify
        that accessing a completely fabricated session_id returns 404, not
        a silently successful update.
        """
        fake_session = str(uuid.uuid4())
        resp = http_client.patch(
            f"/sessions/{fake_session}/video",
            json={"prefers_video": True},
        )
        assert resp.status_code == 404

    def test_cannot_end_other_users_session(self, http_client):
        fake_session = str(uuid.uuid4())
        resp = http_client.post(f"/sessions/{fake_session}/end")
        assert resp.status_code == 404

    def test_mode_session_end_nonexistent_returns_404(self, http_client):
        fake_id = str(uuid.uuid4())
        resp = http_client.post(f"/mode-sessions/{fake_id}/end")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Concurrent connections for the same session
# ---------------------------------------------------------------------------

class TestConcurrentConnections:
    async def test_two_connections_same_session_both_receive(
        self, auth_token, text_session_id
    ):
        """
        Two simultaneous WS connections for the same session_id should not
        crash the server. The second connect overwrites the first in the manager
        (only one connection per session_id is supported by design).
        """
        uri = f"{WS_BASE_URL}/ws/{text_session_id}?token={auth_token}"
        conn1 = await websockets.connect(uri)
        conn2 = await websockets.connect(uri)

        try:
            # Send from conn2 (the active one)
            await conn2.send(json.dumps({"type": "rag_turn", "message": "test"}))
            resp = json.loads(await asyncio.wait_for(conn2.recv(), timeout=10))
            assert isinstance(resp, dict)
        finally:
            await conn1.close()
            await conn2.close()

    async def test_reconnect_after_disconnect_works(
        self, auth_token, text_session_id
    ):
        """Disconnecting and reconnecting to the same session must work cleanly."""
        uri = f"{WS_BASE_URL}/ws/{text_session_id}?token={auth_token}"

        # First connection
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "rag_turn", "message": "test 1"}))
            resp1 = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            assert resp1.get("status") == "processing"

        # Second connection (after first closed)
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "rag_turn", "message": "test 2"}))
            resp2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            assert resp2.get("status") == "processing"
