"""
E2E tests for Review mode via WebSocket.

Flow:
  WS connect → mode_session_start (review) → receive first question
             → mode_session_turn (answer)   → receive evaluation
             → mode_session_end             → session closed, duration stored

Run with: pytest -m e2e tests/e2e/test_review_mode.py
"""
import asyncio
import json
import pytest
import websockets
from tests.conftest import requires_server, AI_RESPONSE_TIMEOUT


pytestmark = [pytest.mark.e2e, requires_server]


async def _recv(ws, timeout: int = AI_RESPONSE_TIMEOUT) -> dict:
    """Receive and parse next WS message."""
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def _wait_for_result(ws, timeout: int = AI_RESPONSE_TIMEOUT) -> dict:
    """Skip processing acks, return the first substantive payload."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        msg = await _recv(ws, timeout=int(remaining) + 1)
        if "prompt" in msg or "evaluation" in msg or msg.get("status") == "failed":
            return msg
    raise TimeoutError("No result received")


class TestReviewModeWs:
    @pytest.mark.slow
    async def test_mode_session_start_returns_ack_and_mode_session_id(
        self, auth_token, text_session_id, ws_base_url
    ):
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "mode_session_start",
                "mode": "review",
                "session_type": "froth_flotation",
                "difficulty": "Basic",
                "total_items": 2,
            }))
            ack = await _recv(ws, timeout=10)

        assert ack["status"] == "processing"
        assert "mode_session_id" in ack
        assert "task_id" in ack

    @pytest.mark.slow
    async def test_first_review_question_delivered(
        self, auth_token, text_session_id, ws_base_url
    ):
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "mode_session_start",
                "mode": "review",
                "session_type": "froth_flotation",
                "difficulty": "Basic",
                "total_items": 2,
            }))
            await _recv(ws, timeout=10)   # ack
            result = await _wait_for_result(ws)

        assert "prompt" in result
        assert result.get("mode") == "review"
        assert result.get("response_format") == "text"

    @pytest.mark.slow
    async def test_review_turn_delivers_evaluation(
        self, auth_token, text_session_id, ws_base_url
    ):
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            # Start session
            await ws.send(json.dumps({
                "type": "mode_session_start",
                "mode": "review",
                "session_type": "froth_flotation",
                "difficulty": "Basic",
                "total_items": 2,
            }))
            ack = await _recv(ws, timeout=10)
            mode_session_id = ack["mode_session_id"]
            await _wait_for_result(ws)   # first question

            # Submit an answer
            await ws.send(json.dumps({
                "type": "mode_session_turn",
                "mode_session_id": mode_session_id,
                "message": "Froth flotation separates minerals based on surface hydrophobicity.",
            }))
            turn_ack = await _recv(ws, timeout=10)
            assert turn_ack["status"] == "processing"

            evaluation = await _wait_for_result(ws)

        assert "evaluation" in evaluation
        assert "score" in evaluation["evaluation"]
        assert "verdict" in evaluation["evaluation"]
        assert "feedback" in evaluation["evaluation"]

    @pytest.mark.slow
    async def test_mode_session_end_via_ws(
        self, auth_token, text_session_id, ws_base_url
    ):
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "mode_session_start",
                "mode": "review",
                "session_type": "froth_flotation",
                "difficulty": "Basic",
                "total_items": 1,
            }))
            ack = await _recv(ws, timeout=10)
            mode_session_id = ack["mode_session_id"]
            await _wait_for_result(ws)

            # End the session
            await ws.send(json.dumps({
                "type": "mode_session_end",
                "mode_session_id": mode_session_id,
            }))
            end_response = await _recv(ws, timeout=10)

        assert end_response["status"] == "ended"
        assert end_response["mode_session_id"] == mode_session_id

    @pytest.mark.slow
    async def test_mode_session_invalid_type_rejected(
        self, auth_token, text_session_id, ws_base_url
    ):
        """mode must be 'review' or 'application', not 'learn'."""
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "mode_session_start",
                "mode": "learn",   # invalid for mode_session
                "session_type": "froth_flotation",
            }))
            response = await _recv(ws, timeout=10)

        assert "error" in response
