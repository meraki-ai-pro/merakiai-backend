import asyncio
import json
import logging
import os

import redis.asyncio as aioredis
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """
    Singleton that tracks all active WebSocket connections keyed by session_id.

    When a client connects, a dedicated async Redis Pub/Sub listener is started
    for that session. When a Celery worker publishes a result to the Redis
    channel  ws:{session_id},  the listener receives it and forwards it over
    the open WebSocket — completing the worker → client delivery path without
    any polling.

    Connection lifecycle:
        connect()    → accept WS, register, start Redis listener
        disconnect() → remove WS, cancel Redis listener, clean up
    """

    def __init__(self) -> None:
        # session_id → active WebSocket
        self._connections: dict[str, WebSocket] = {}
        # session_id → running asyncio listener task
        self._listeners: dict[str, asyncio.Task] = {}
        self._redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[session_id] = websocket
        self._listeners[session_id] = asyncio.create_task(
            self._redis_listener(session_id),
            name=f"ws-listener-{session_id}",
        )
        logger.info(
            "WebSocket connected  session=%s  total_active=%d",
            session_id,
            len(self._connections),
        )

    async def disconnect(self, session_id: str) -> None:
        self._connections.pop(session_id, None)
        task = self._listeners.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.debug("Listener task cancelled successfully  session=%s", session_id)
        logger.info(
            "WebSocket disconnected  session=%s  total_active=%d",
            session_id,
            len(self._connections),
        )

    async def send(self, session_id: str, data: dict) -> None:
        """Push a JSON payload to the client. Silently drops if disconnected."""
        ws = self._connections.get(session_id)
        if not ws:
            return
        try:
            await ws.send_json(data)
        except Exception as exc:
            logger.warning("WS send failed  session=%s  error=%s", session_id, exc)
            await self.disconnect(session_id)

    @property
    def active_count(self) -> int:
        return len(self._connections)

    # ------------------------------------------------------------------
    # Internal Redis Pub/Sub listener
    # ------------------------------------------------------------------

    async def _redis_listener(self, session_id: str) -> None:
        """
        Subscribes to  ws:{session_id}  on Redis and forwards every published
        message to the open WebSocket connection.  Runs for the lifetime of
        the connection; cancelled via disconnect().
        """
        channel = f"ws:{session_id}"
        redis = aioredis.from_url(self._redis_url, decode_responses=True)
        pubsub = redis.pubsub()

        try:
            await pubsub.subscribe(channel)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError):
                    payload = {"raw": message["data"]}
                await self.send(session_id, payload)
        except asyncio.CancelledError:
            logger.debug("Redis listener cancelled  session=%s", session_id)
        except Exception as exc:
            logger.error(
                "Redis listener error  session=%s  error=%s", session_id, exc
            )
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await redis.aclose()
            except Exception:
                logger.debug("Failed to close Redis connection  session=%s", session_id)


# ---------------------------------------------------------------------------
# Singleton — imported everywhere that needs to send to a WebSocket
# ---------------------------------------------------------------------------
manager = WebSocketManager()
