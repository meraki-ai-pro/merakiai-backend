"""
Unit tests for app/core/websocket_manager.py

Tests connection tracking, listener task lifecycle, and message delivery.
The Redis pub/sub is mocked so no real Redis is needed.
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ws_mock():
    """Return an AsyncMock that behaves like a FastAPI WebSocket."""
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    ws.accept = AsyncMock()
    return ws


async def _instant_cancel():
    """Coroutine that is cancelled immediately — used to mock listener tasks."""
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestConnect:
    async def test_accept_is_called(self):
        from app.core.websocket_manager import WebSocketManager
        mgr = WebSocketManager()
        ws = _make_ws_mock()

        with patch("app.core.websocket_manager.aioredis.from_url") as mock_redis:
            # Make the pubsub.listen() return an empty async iterator so the
            # listener task doesn't run forever during the test
            mock_pubsub = AsyncMock()
            mock_pubsub.listen.return_value.__aiter__ = lambda s: s
            mock_pubsub.listen.return_value.__anext__ = AsyncMock(
                side_effect=asyncio.CancelledError
            )
            mock_redis.return_value.pubsub.return_value = mock_pubsub

            await mgr.connect("session-1", ws)

        ws.accept.assert_called_once()

    async def test_connection_is_tracked(self):
        from app.core.websocket_manager import WebSocketManager
        mgr = WebSocketManager()
        ws = _make_ws_mock()

        with patch("app.core.websocket_manager.aioredis.from_url"):
            await mgr.connect("session-1", ws)

        assert "session-1" in mgr._connections
        assert mgr._connections["session-1"] is ws

    async def test_listener_task_is_created(self):
        from app.core.websocket_manager import WebSocketManager
        mgr = WebSocketManager()
        ws = _make_ws_mock()

        with patch("app.core.websocket_manager.aioredis.from_url"):
            await mgr.connect("session-2", ws)

        assert "session-2" in mgr._listeners
        assert isinstance(mgr._listeners["session-2"], asyncio.Task)

    async def test_active_count_increments(self):
        from app.core.websocket_manager import WebSocketManager
        mgr = WebSocketManager()

        with patch("app.core.websocket_manager.aioredis.from_url"):
            await mgr.connect("s1", _make_ws_mock())
            await mgr.connect("s2", _make_ws_mock())

        assert mgr.active_count == 2


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDisconnect:
    async def test_connection_removed(self):
        from app.core.websocket_manager import WebSocketManager
        mgr = WebSocketManager()
        ws = _make_ws_mock()

        with patch("app.core.websocket_manager.aioredis.from_url"):
            await mgr.connect("session-x", ws)

        await mgr.disconnect("session-x")
        assert "session-x" not in mgr._connections

    async def test_listener_task_cancelled(self):
        from app.core.websocket_manager import WebSocketManager
        mgr = WebSocketManager()
        ws = _make_ws_mock()

        with patch("app.core.websocket_manager.aioredis.from_url"):
            await mgr.connect("session-x", ws)

        task = mgr._listeners.get("session-x")
        await mgr.disconnect("session-x")

        assert task is None or task.cancelled() or task.done()
        assert "session-x" not in mgr._listeners

    async def test_disconnect_nonexistent_session_is_safe(self):
        from app.core.websocket_manager import WebSocketManager
        mgr = WebSocketManager()
        # Should not raise even when session was never connected
        await mgr.disconnect("never-connected")

    async def test_active_count_decrements(self):
        from app.core.websocket_manager import WebSocketManager
        mgr = WebSocketManager()

        with patch("app.core.websocket_manager.aioredis.from_url"):
            await mgr.connect("s1", _make_ws_mock())
            await mgr.connect("s2", _make_ws_mock())

        await mgr.disconnect("s1")
        assert mgr.active_count == 1


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSend:
    async def test_delivers_json_to_connected_session(self):
        from app.core.websocket_manager import WebSocketManager
        mgr = WebSocketManager()
        ws = _make_ws_mock()

        with patch("app.core.websocket_manager.aioredis.from_url"):
            await mgr.connect("session-y", ws)

        payload = {"mode": "learn", "response": "Hello!"}
        await mgr.send("session-y", payload)

        ws.send_json.assert_called_once_with(payload)

    async def test_send_to_disconnected_session_is_silent(self):
        from app.core.websocket_manager import WebSocketManager
        mgr = WebSocketManager()
        # Should not raise
        await mgr.send("nonexistent-session", {"foo": "bar"})

    async def test_send_failure_disconnects_session(self):
        """If send_json raises, the manager should disconnect the session."""
        from app.core.websocket_manager import WebSocketManager
        mgr = WebSocketManager()
        ws = _make_ws_mock()
        ws.send_json.side_effect = RuntimeError("connection broken")

        with patch("app.core.websocket_manager.aioredis.from_url"):
            await mgr.connect("session-z", ws)

        await mgr.send("session-z", {"test": True})

        # Session should be removed after a broken send
        assert "session-z" not in mgr._connections
