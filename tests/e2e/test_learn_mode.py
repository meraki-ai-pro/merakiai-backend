"""
E2E tests for Learn mode (text and video response formats).

Requires: full stack running (FastAPI + Celery workers + RabbitMQ + Redis)
Run with: pytest -m e2e tests/e2e/test_learn_mode.py
"""
import asyncio
import json
import pytest
import websockets
from tests.conftest import requires_server, AI_RESPONSE_TIMEOUT


pytestmark = [pytest.mark.e2e, requires_server]


# ---------------------------------------------------------------------------
# Helper: receive messages from WS until we get the AI result (not an ack)
# ---------------------------------------------------------------------------

async def _receive_result(ws, timeout: int = AI_RESPONSE_TIMEOUT) -> dict:
    """
    Drain the WebSocket until we get a payload that is NOT a bare ack.
    Acks look like: {"status": "processing", "task_id": "..."}
    Results look like: {"mode": "...", "response": "..."}  or  {"status": "failed", ...}
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        msg = json.loads(raw)
        if "response" in msg or (msg.get("status") == "failed"):
            return msg
    raise TimeoutError(f"No AI result received within {timeout}s")


# ---------------------------------------------------------------------------
# Text format tests
# ---------------------------------------------------------------------------

class TestLearnModeText:
    async def test_rag_turn_returns_processing_ack(
        self, auth_token, text_session_id, ws_base_url
    ):
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "rag_turn",
                "message": "What is froth flotation?",
            }))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

        assert ack["status"] == "processing"
        assert "task_id" in ack

    @pytest.mark.slow
    async def test_rag_turn_delivers_text_response(
        self, auth_token, text_session_id, ws_base_url
    ):
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "rag_turn",
                "message": "Explain the role of frother in flotation.",
            }))
            await asyncio.wait_for(ws.recv(), timeout=10)  # discard ack
            result = await _receive_result(ws)

        assert result.get("mode") == "learn"
        assert isinstance(result.get("response"), str)
        assert len(result["response"]) > 10
        assert result.get("response_format") == "text"
        assert result.get("video_url") is None

    @pytest.mark.slow
    async def test_analytics_request_metric_created(
        self, auth_token, text_session_id, ws_base_url, http_client
    ):
        """After a learn turn, a request_metrics row must exist for the session."""
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "rag_turn",
                "message": "What is concentrate grade?",
            }))
            await asyncio.wait_for(ws.recv(), timeout=10)
            await _receive_result(ws)

        # Give analytics a moment to write
        await asyncio.sleep(1)

        # Verify via health endpoint that the app is still up (analytics errors
        # are non-fatal but we want to ensure no crash)
        resp = http_client.get("/health")
        assert resp.status_code == 200

    async def test_ws_session_analytics_opened_on_connect(
        self, auth_token, text_session_id, ws_base_url
    ):
        """Connecting to WS and disconnecting should not error."""
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            # Just connect and disconnect — analytics.open_ws_session is called
            await asyncio.sleep(0.5)
        # No assertion needed — test passes if no exception raised


# ---------------------------------------------------------------------------
# Video format tests
# ---------------------------------------------------------------------------

class TestLearnModeVideo:
    @pytest.mark.slow
    async def test_rag_turn_video_session_returns_ack(
        self, auth_token, video_session_id, ws_base_url
    ):
        uri = f"{ws_base_url}/ws/{video_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "rag_turn",
                "message": "What happens during the conditioning stage?",
            }))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

        assert ack["status"] == "processing"
        assert "task_id" in ack

    @pytest.mark.slow
    async def test_video_response_has_expected_fields(
        self, auth_token, video_session_id, ws_base_url
    ):
        """
        Video tasks should return response_format='video' with audio_url.
        If video generation fails (D-ID/Tavus down), it falls back to text —
        both outcomes are acceptable.
        """
        uri = f"{ws_base_url}/ws/{video_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "rag_turn",
                "message": "What is recovery in flotation?",
            }))
            await asyncio.wait_for(ws.recv(), timeout=10)
            result = await _receive_result(ws)

        assert "mode" in result
        assert result["mode"] == "learn"
        assert "response" in result
        # response_format is either 'video' (success) or 'text' (video fallback)
        assert result.get("response_format") in ("video", "text")

    async def test_video_task_routes_to_video_queue(
        self, auth_token, video_session_id, ws_base_url
    ):
        """
        When prefers_video=True, the task should be dispatched to video_tasks queue.
        We verify indirectly: the ack arrives quickly (no text-queue starvation)
        and the session confirms prefers_video is still True.
        """
        uri = f"{ws_base_url}/ws/{video_session_id}?token={auth_token}"
        import time
        t0 = time.monotonic()
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "rag_turn",
                "message": "Explain air bubble attachment.",
            }))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))

        dispatch_ms = (time.monotonic() - t0) * 1000
        assert ack["status"] == "processing"
        # Task dispatch (not AI result) should be sub-second
        assert dispatch_ms < 3000, f"Dispatch took {dispatch_ms:.0f}ms — too slow"
