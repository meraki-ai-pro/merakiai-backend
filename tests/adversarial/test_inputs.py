"""
Adversarial tests — Input validation and injection attacks.

Verifies the system handles malformed, malicious, and edge-case inputs
without crashing, leaking data, or executing injected code.

Run with: pytest -m adversarial tests/adversarial/test_inputs.py
"""
import asyncio
import json
import pytest
import websockets
from tests.conftest import requires_server, WS_BASE_URL


pytestmark = [pytest.mark.adversarial, requires_server]


async def _connect_and_send(auth_token, session_id, payload: dict, recv_timeout: int = 15) -> dict:
    """Connect, send a single message, return first response."""
    uri = f"{WS_BASE_URL}/ws/{session_id}?token={auth_token}"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps(payload))
        raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
        return json.loads(raw)


class TestMalformedMessages:
    async def test_invalid_json_returns_error(
        self, auth_token, text_session_id
    ):
        uri = f"{WS_BASE_URL}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send("this is not json {{{")
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

        assert "error" in resp

    async def test_missing_type_field_returns_error(
        self, auth_token, text_session_id
    ):
        resp = await _connect_and_send(auth_token, text_session_id, {
            "message": "hello"
            # "type" key missing
        })
        assert "error" in resp

    async def test_unknown_message_type_returns_error(
        self, auth_token, text_session_id
    ):
        resp = await _connect_and_send(auth_token, text_session_id, {
            "type": "do_something_evil",
            "message": "payload",
        })
        assert "error" in resp

    async def test_empty_json_object_returns_error(
        self, auth_token, text_session_id
    ):
        resp = await _connect_and_send(auth_token, text_session_id, {})
        assert "error" in resp


class TestEmptyAndWhitespace:
    async def test_empty_message_rejected(
        self, auth_token, text_session_id
    ):
        resp = await _connect_and_send(auth_token, text_session_id, {
            "type": "rag_turn",
            "message": "",
        })
        assert "error" in resp

    async def test_whitespace_only_message_rejected(
        self, auth_token, text_session_id
    ):
        resp = await _connect_and_send(auth_token, text_session_id, {
            "type": "rag_turn",
            "message": "     ",
        })
        assert "error" in resp

    async def test_null_message_rejected(
        self, auth_token, text_session_id
    ):
        resp = await _connect_and_send(auth_token, text_session_id, {
            "type": "rag_turn",
            "message": None,
        })
        assert "error" in resp

    async def test_missing_mode_session_id_in_turn(
        self, auth_token, text_session_id
    ):
        resp = await _connect_and_send(auth_token, text_session_id, {
            "type": "mode_session_turn",
            "message": "some answer",
            # mode_session_id missing
        })
        assert "error" in resp

    async def test_missing_answer_in_turn(
        self, auth_token, text_session_id
    ):
        resp = await _connect_and_send(auth_token, text_session_id, {
            "type": "mode_session_turn",
            "mode_session_id": "some-id",
            "message": "",
        })
        assert "error" in resp


class TestInjectionAttacks:
    async def test_sql_injection_in_message_does_not_crash(
        self, auth_token, text_session_id
    ):
        """
        SQL injection in the message field must be treated as plain text,
        not executed. The AI receives it as a string, the DB stores it safely
        (Supabase uses parameterized queries internally).
        """
        injection = "'; DROP TABLE sessions; SELECT * FROM users WHERE '1'='1"
        resp = await _connect_and_send(auth_token, text_session_id, {
            "type": "rag_turn",
            "message": injection,
        }, recv_timeout=10)

        # Should return a processing ack, NOT a 500 error
        assert resp.get("status") == "processing" or "error" not in resp

    async def test_xss_payload_does_not_crash(
        self, auth_token, text_session_id
    ):
        """XSS payload in message must be handled safely (backend returns text, not HTML)."""
        xss = "<script>alert('xss')</script><img src=x onerror=alert(1)>"
        resp = await _connect_and_send(auth_token, text_session_id, {
            "type": "rag_turn",
            "message": xss,
        }, recv_timeout=10)

        assert resp.get("status") == "processing" or "error" not in resp

    async def test_null_bytes_in_message_do_not_crash(
        self, auth_token, text_session_id
    ):
        """Null bytes should be handled without crashing."""
        # Null bytes in JSON string — the message field gets sanitized by pydantic
        resp = await _connect_and_send(auth_token, text_session_id, {
            "type": "rag_turn",
            "message": "hello\x00world",
        }, recv_timeout=10)

        # Should not crash — either processed or error returned
        assert isinstance(resp, dict)

    async def test_unicode_and_emoji_in_message(
        self, auth_token, text_session_id
    ):
        """Unicode, RTL text, and emoji must not crash the system."""
        unicode_msg = "مرحبا بالعالم 🌍 Привет мир こんにちは"
        resp = await _connect_and_send(auth_token, text_session_id, {
            "type": "rag_turn",
            "message": unicode_msg,
        }, recv_timeout=10)

        assert isinstance(resp, dict)
        assert "status" in resp  # at minimum an ack

    async def test_path_traversal_in_session_id_rejected(self, auth_token):
        """Session ID with path traversal chars should be rejected or ignored."""
        import httpx
        from tests.conftest import BASE_URL
        with httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {auth_token}"},
        ) as client:
            resp = client.get("/sessions/../../../../etc/passwd")

        assert resp.status_code in (404, 422, 400)


class TestOversizedPayloads:
    async def test_very_long_message_handled_gracefully(
        self, auth_token, text_session_id
    ):
        """A 50KB message should not crash the server. It may error or process."""
        huge_message = "Describe froth flotation in detail. " * 1500  # ~55KB
        uri = f"{WS_BASE_URL}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(
            uri, max_size=1024 * 1024  # allow large client-side messages
        ) as ws:
            await ws.send(json.dumps({"type": "rag_turn", "message": huge_message}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))

        # App must not 500 — either processes or rejects gracefully
        assert isinstance(resp, dict)
        # If it crashes, result would be missing or have an unstructured error
        if "error" in resp:
            assert isinstance(resp["error"], str)  # at least a string error

    async def test_deeply_nested_json_does_not_crash(
        self, auth_token, text_session_id
    ):
        """Deeply nested extra fields in the WS message should be ignored."""
        resp = await _connect_and_send(auth_token, text_session_id, {
            "type": "rag_turn",
            "message": "What is flotation?",
            "extra": {"a": {"b": {"c": {"d": {"e": "deeply nested"}}}}},
            "injection_attempt": [1, 2, 3] * 100,
        }, recv_timeout=10)

        # Extra fields ignored, message processed normally
        assert resp.get("status") == "processing" or "error" not in resp


class TestRapidFireMessages:
    async def test_flooding_does_not_crash_server(
        self, auth_token, text_session_id
    ):
        """Sending 20 messages rapidly should not crash. Some may queue."""
        uri = f"{WS_BASE_URL}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            for i in range(20):
                await ws.send(json.dumps({
                    "type": "rag_turn",
                    "message": f"Question {i}: What is froth flotation?",
                }))

            # Read at least one ack to confirm connection is alive
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))

        assert isinstance(resp, dict)

    async def test_duplicate_mode_session_end_is_idempotent(
        self, auth_token, text_session_id, ws_base_url, http_client
    ):
        """Ending a session twice should not crash — second call should be graceful."""
        # Create a mode session via HTTP to get a mode_session_id
        resp = http_client.post("/mode-sessions/start", json={
            "session_id": text_session_id,
            "mode": "review",
            "session_type": "froth_flotation",
            "difficulty": "Basic",
            "total_items": 1,
        })
        if resp.status_code != 200:
            pytest.skip("Could not create mode session for this test")

        msid = resp.json().get("mode_session_id")
        if not msid:
            pytest.skip("No mode_session_id returned")

        # End once via HTTP
        http_client.post(f"/mode-sessions/{msid}/end")

        # End again via HTTP — should not 500
        resp2 = http_client.post(f"/mode-sessions/{msid}/end")
        assert resp2.status_code in (200, 404, 409)  # not 500
