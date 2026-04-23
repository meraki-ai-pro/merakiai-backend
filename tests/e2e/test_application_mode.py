"""
E2E tests for Application mode via WebSocket.

Practice flow: 3 guided questions → auto-completion on 3rd answer.

Run with: pytest -m e2e tests/e2e/test_application_mode.py
"""
import asyncio
import json
import pytest
import websockets
from tests.conftest import requires_server, AI_RESPONSE_TIMEOUT


pytestmark = [pytest.mark.e2e, requires_server]


async def _recv(ws, timeout: int = AI_RESPONSE_TIMEOUT) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def _wait_for_prompt_or_eval(ws, timeout: int = AI_RESPONSE_TIMEOUT) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        remaining = max(1, int(deadline - asyncio.get_event_loop().time()))
        msg = await _recv(ws, timeout=remaining)
        if "prompt" in msg or "evaluation" in msg or "summary" in msg:
            return msg
        if msg.get("status") == "failed":
            return msg
    raise TimeoutError("No result received")


class TestPracticeModeWs:
    @pytest.mark.slow
    async def test_mode_session_start_practice_returns_ack(
        self, auth_token, text_session_id, ws_base_url
    ):
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "mode_session_start",
                "mode": "application",
                "session_type": "froth_flotation",
                "difficulty": "Basic",
            }))
            ack = await _recv(ws, timeout=10)

        assert ack["status"] == "processing"
        assert "mode_session_id" in ack

    @pytest.mark.slow
    async def test_first_practice_question_delivered(
        self, auth_token, text_session_id, ws_base_url
    ):
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "mode_session_start",
                "mode": "application",
                "session_type": "froth_flotation",
                "difficulty": "Basic",
            }))
            await _recv(ws, timeout=10)
            result = await _wait_for_prompt_or_eval(ws)

        assert "prompt" in result
        assert result.get("mode") == "application"
        assert result.get("response_format") == "text"

    @pytest.mark.slow
    async def test_practice_turn_delivers_evaluation_and_next_prompt(
        self, auth_token, text_session_id, ws_base_url
    ):
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            # Start
            await ws.send(json.dumps({
                "type": "mode_session_start",
                "mode": "application",
                "session_type": "froth_flotation",
                "difficulty": "Basic",
            }))
            ack = await _recv(ws, timeout=10)
            mode_session_id = ack["mode_session_id"]
            await _wait_for_prompt_or_eval(ws)  # question 1

            # Answer question 1
            await ws.send(json.dumps({
                "type": "mode_session_turn",
                "mode_session_id": mode_session_id,
                "message": "The collector makes the mineral surface hydrophobic, allowing air bubbles to attach.",
            }))
            await _recv(ws, timeout=10)  # ack
            eval_result = await _wait_for_prompt_or_eval(ws)

        # Should have evaluation for Q1 and next_prompt for Q2
        assert eval_result.get("type") == "evaluation"
        assert "evaluation" in eval_result
        assert "next_prompt" in eval_result

    @pytest.mark.slow
    async def test_practice_auto_completes_after_3_turns(
        self, auth_token, text_session_id, ws_base_url
    ):
        """
        Completing all 3 guided questions triggers auto-completion
        with key_learning_points.
        """
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            # Start
            await ws.send(json.dumps({
                "type": "mode_session_start",
                "mode": "application",
                "session_type": "froth_flotation",
                "difficulty": "Basic",
            }))
            ack = await _recv(ws, timeout=10)
            msid = ack["mode_session_id"]
            await _wait_for_prompt_or_eval(ws)  # Q1

            answer = "Froth flotation uses surface chemistry differences between minerals."

            # Turn 1
            await ws.send(json.dumps({"type": "mode_session_turn", "mode_session_id": msid, "message": answer}))
            await _recv(ws, timeout=10)
            r1 = await _wait_for_prompt_or_eval(ws)
            assert r1["type"] == "evaluation"

            # Turn 2
            await ws.send(json.dumps({"type": "mode_session_turn", "mode_session_id": msid, "message": answer}))
            await _recv(ws, timeout=10)
            r2 = await _wait_for_prompt_or_eval(ws)
            assert r2["type"] == "evaluation"

            # Turn 3 — final
            await ws.send(json.dumps({"type": "mode_session_turn", "mode_session_id": msid, "message": answer}))
            await _recv(ws, timeout=10)
            r3 = await _wait_for_prompt_or_eval(ws)

        assert r3["type"] == "completed"
        assert "key_learning_points" in r3

    @pytest.mark.slow
    async def test_practice_and_review_text_only(
        self, auth_token, text_session_id, ws_base_url
    ):
        """Practice/Review must ALWAYS return text — even if session has prefers_video=True."""
        uri = f"{ws_base_url}/ws/{text_session_id}?token={auth_token}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "mode_session_start",
                "mode": "application",
                "session_type": "froth_flotation",
                "difficulty": "Basic",
            }))
            await _recv(ws, timeout=10)
            result = await _wait_for_prompt_or_eval(ws)

        assert result.get("response_format") == "text"
        assert result.get("video_url") is None
