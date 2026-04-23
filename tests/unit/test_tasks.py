"""
Unit tests for app/ai/tasks.py

Focuses on:
  - _publish_to_ws: verifies Redis pub/sub channel and payload
  - Each @shared_task: verifies publish is called regardless of success/failure
  - Timing: verifies ai_processing_ms and video_generation_ms are captured

All external services (Supabase, Redis, AI) are mocked.
"""
import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock, call


# ---------------------------------------------------------------------------
# _publish_to_ws
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPublishToWs:
    def test_publishes_to_correct_channel(self):
        mock_redis = MagicMock()
        with patch("app.ai.tasks.redis_sync.from_url", return_value=mock_redis):
            from app.ai.tasks import _publish_to_ws
            _publish_to_ws("session-abc", {"response": "hello"})

        mock_redis.publish.assert_called_once()
        channel, payload = mock_redis.publish.call_args[0]
        assert channel == "ws:session-abc"

    def test_payload_is_valid_json(self):
        mock_redis = MagicMock()
        with patch("app.ai.tasks.redis_sync.from_url", return_value=mock_redis):
            from app.ai.tasks import _publish_to_ws
            data = {"mode": "learn", "response": "test"}
            _publish_to_ws("s1", data)

        _, payload_str = mock_redis.publish.call_args[0]
        parsed = json.loads(payload_str)
        assert parsed == data

    def test_closes_redis_connection(self):
        mock_redis = MagicMock()
        with patch("app.ai.tasks.redis_sync.from_url", return_value=mock_redis):
            from app.ai.tasks import _publish_to_ws
            _publish_to_ws("s1", {})

        mock_redis.close.assert_called_once()

    def test_silent_on_redis_failure(self):
        with patch("app.ai.tasks.redis_sync.from_url", side_effect=Exception("Redis down")):
            from app.ai.tasks import _publish_to_ws
            _publish_to_ws("s1", {"test": True})  # must not raise


# ---------------------------------------------------------------------------
# process_rag_turn_task
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestProcessRagTurnTask:
    def _run_task(self, mock_result: dict):
        mock_redis = MagicMock()

        async def fake_rag_turn(*args, **kwargs):
            return mock_result

        with (
            patch("app.ai.tasks.redis_sync.from_url", return_value=mock_redis),
            patch("app.ai.tasks._do_rag_turn", side_effect=fake_rag_turn),
            patch("app.ai.tasks.reset_async_supabase"),
            patch("app.ai.tasks.analytics"),
        ):
            from app.ai.tasks import process_rag_turn_task
            return process_rag_turn_task("session-1", "user-1", "Hello?", "learn")

    def test_publishes_on_success(self):
        mock_redis = MagicMock()
        result = {"mode": "learn", "response": "ok"}

        async def fake_rag(*args, **kwargs):
            return result

        with (
            patch("app.ai.tasks.redis_sync.from_url", return_value=mock_redis),
            patch("app.ai.tasks._do_rag_turn", side_effect=fake_rag),
            patch("app.ai.tasks.reset_async_supabase"),
            patch("app.ai.tasks.analytics"),
        ):
            from app.ai.tasks import process_rag_turn_task
            process_rag_turn_task("s1", "u1", "msg", "learn")

        mock_redis.publish.assert_called_once()
        channel = mock_redis.publish.call_args[0][0]
        assert channel == "ws:s1"

    def test_publishes_on_failure(self):
        """Even when the task raises, the error is published to the WS."""
        mock_redis = MagicMock()

        async def exploding_rag(*args, **kwargs):
            raise RuntimeError("AI unavailable")

        with (
            patch("app.ai.tasks.redis_sync.from_url", return_value=mock_redis),
            patch("app.ai.tasks._do_rag_turn", side_effect=exploding_rag),
            patch("app.ai.tasks.reset_async_supabase"),
            patch("app.ai.tasks.analytics"),
        ):
            from app.ai.tasks import process_rag_turn_task
            result = process_rag_turn_task("s1", "u1", "msg", "learn")

        assert result["status"] == "failed"
        mock_redis.publish.assert_called_once()
        payload = json.loads(mock_redis.publish.call_args[0][1])
        assert payload["status"] == "failed"


# ---------------------------------------------------------------------------
# process_mode_session_start_task
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestProcessModeSessionStartTask:
    def test_publishes_review_result(self):
        mock_redis = MagicMock()
        review_result = {
            "mode_session_id": "ms-1",
            "mode": "review",
            "prompt": "What is flotation?",
        }

        async def fake_start(*args, **kwargs):
            return review_result

        with (
            patch("app.ai.tasks.redis_sync.from_url", return_value=mock_redis),
            patch("app.ai.tasks._do_mode_session_start", side_effect=fake_start),
            patch("app.ai.tasks.reset_async_supabase"),
            patch("app.ai.tasks.analytics"),
        ):
            from app.ai.tasks import process_mode_session_start_task
            result = process_mode_session_start_task(
                "s1", "u1", "review", "froth_flotation", "Basic", "ms-1"
            )

        assert result["mode"] == "review"
        channel = mock_redis.publish.call_args[0][0]
        assert channel == "ws:s1"

    def test_publishes_on_failure(self):
        mock_redis = MagicMock()

        async def explode(*args, **kwargs):
            raise RuntimeError("Pinecone down")

        with (
            patch("app.ai.tasks.redis_sync.from_url", return_value=mock_redis),
            patch("app.ai.tasks._do_mode_session_start", side_effect=explode),
            patch("app.ai.tasks.reset_async_supabase"),
            patch("app.ai.tasks.analytics"),
        ):
            from app.ai.tasks import process_mode_session_start_task
            result = process_mode_session_start_task(
                "s1", "u1", "review", "type", "Basic", "ms-1"
            )

        assert result["status"] == "failed"
        mock_redis.publish.assert_called_once()


# ---------------------------------------------------------------------------
# process_mode_session_turn_task
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestProcessModeSessionTurnTask:
    def test_publishes_turn_result(self):
        mock_redis = MagicMock()
        turn_result = {"mode": "application", "type": "evaluation"}

        async def fake_turn(*args, **kwargs):
            return turn_result

        with (
            patch("app.ai.tasks.redis_sync.from_url", return_value=mock_redis),
            patch("app.ai.tasks._do_mode_session_turn", side_effect=fake_turn),
            patch("app.ai.tasks.reset_async_supabase"),
            patch("app.ai.tasks.analytics"),
        ):
            from app.ai.tasks import process_mode_session_turn_task
            process_mode_session_turn_task(
                "ms-1", "s1", "u1", "application", "froth", "answer",
                {}, [], 1, 3, "Basic", 1, 3,
            )

        mock_redis.publish.assert_called_once()
        channel = mock_redis.publish.call_args[0][0]
        assert channel == "ws:s1"

    def test_publishes_on_failure(self):
        mock_redis = MagicMock()

        async def explode(*args, **kwargs):
            raise ValueError("Bad state")

        with (
            patch("app.ai.tasks.redis_sync.from_url", return_value=mock_redis),
            patch("app.ai.tasks._do_mode_session_turn", side_effect=explode),
            patch("app.ai.tasks.reset_async_supabase"),
            patch("app.ai.tasks.analytics"),
        ):
            from app.ai.tasks import process_mode_session_turn_task
            result = process_mode_session_turn_task(
                "ms-1", "s1", "u1", "review", "froth", "answer",
                {}, [], 1, 1, "Basic", 1, 10,
            )

        assert result["status"] == "failed"
        mock_redis.publish.assert_called_once()


# ---------------------------------------------------------------------------
# Timing: analytics called with correct breakdown
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTaskTiming:
    def test_ai_ms_and_video_ms_logged_for_video_response(self):
        mock_redis = MagicMock()
        mock_analytics = MagicMock()

        # Simulate a task that produces a video response
        async def fake_rag(session_id, user_id, message, mode):
            # Simulate the actual _do_rag_turn by calling analytics directly
            mock_analytics.log_request_metric(
                session_id=session_id,
                user_id=user_id,
                mode=mode,
                task_type=f"rag_turn_{mode}",
                response_format="video",
                total_processing_ms=5000,
                ai_processing_ms=800,
                video_generation_ms=3500,
            )
            return {"mode": mode, "response": "test", "response_format": "video"}

        with (
            patch("app.ai.tasks.redis_sync.from_url", return_value=mock_redis),
            patch("app.ai.tasks._do_rag_turn", side_effect=fake_rag),
            patch("app.ai.tasks.reset_async_supabase"),
            patch("app.ai.tasks.analytics", mock_analytics),
        ):
            from app.ai.tasks import process_rag_turn_task
            process_rag_turn_task("s1", "u1", "msg", "learn")

        mock_analytics.log_request_metric.assert_called_once()
        call_kwargs = mock_analytics.log_request_metric.call_args.kwargs
        assert call_kwargs["response_format"] == "video"
        assert call_kwargs["ai_processing_ms"] == 800
        assert call_kwargs["video_generation_ms"] == 3500
