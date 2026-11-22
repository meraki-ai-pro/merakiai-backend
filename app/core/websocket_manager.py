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

    One Redis pattern subscription serves every WebSocket owned by this API
    process. When a Celery worker publishes a result to ``ws:{session_id}``,
    the shared listener extracts the session id and forwards the payload to the
    local connection. This keeps Redis connection usage constant as the number
    of students grows.

    Connection lifecycle:
        connect()    → accept WS, register, ensure shared Redis listener
        disconnect() → remove WS, stop shared listener when the process is idle
    """

    def __init__(self) -> None:
        # session_id → active WebSocket
        self._connections: dict[str, WebSocket] = {}
        self._listener_task: asyncio.Task | None = None
        self._listener_lock = asyncio.Lock()
        self._listener_ready = asyncio.Event()
        self._redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        # The process uses one long-lived Pub/Sub connection. Keep a little
        # headroom for reconnect overlap and future health checks.
        self._pool: aioredis.ConnectionPool = aioredis.ConnectionPool.from_url(
            self._redis_url,
            decode_responses=True,
            max_connections=10,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[session_id] = websocket
        await self._ensure_listener()
        logger.info(
            "WebSocket connected  session=%s  total_active=%d",
            session_id,
            len(self._connections),
        )

    async def disconnect(self, session_id: str) -> None:
        self._connections.pop(session_id, None)
        if not self._connections:
            await self._stop_listener()
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
    # Internal shared Redis Pub/Sub listener
    # ------------------------------------------------------------------

    async def _ensure_listener(self) -> None:
        async with self._listener_lock:
            if self._listener_task is None or self._listener_task.done():
                self._listener_ready.clear()
                self._listener_task = asyncio.create_task(
                    self._redis_listener_loop(),
                    name="ws-listener-shared",
                )

        try:
            await asyncio.wait_for(self._listener_ready.wait(), timeout=3.0)
        except TimeoutError:
            # The loop keeps retrying. Accepting the socket lets the client
            # reconnect normally instead of turning a brief Redis outage into
            # an immediate handshake failure.
            logger.warning("Redis listener was not ready within 3 seconds")

    async def _stop_listener(self) -> None:
        async with self._listener_lock:
            task = self._listener_task
            if task is None:
                return
            if task is asyncio.current_task():
                # send() can discover a broken final socket from inside the
                # listener. The loop checks _connections after that send and
                # exits without attempting to await itself.
                self._listener_task = None
                self._listener_ready.clear()
                return
            self._listener_task = None
            self._listener_ready.clear()
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.debug("Shared Redis listener cancelled successfully")

    async def _redis_listener_loop(self) -> None:
        """Forward all ``ws:*`` events through one Redis connection."""
        retry_delay = 0.1
        try:
            while self._connections:
                redis = aioredis.Redis(connection_pool=self._pool)
                pubsub = redis.pubsub()
                try:
                    await pubsub.psubscribe("ws:*")
                    self._listener_ready.set()
                    retry_delay = 0.1

                    async for message in pubsub.listen():
                        if message["type"] != "pmessage":
                            continue
                        channel = str(message.get("channel") or "")
                        if not channel.startswith("ws:"):
                            continue
                        session_id = channel[3:]
                        try:
                            payload = json.loads(message["data"])
                        except (json.JSONDecodeError, TypeError):
                            payload = {"raw": message["data"]}
                        await self.send(session_id, payload)
                        if not self._connections:
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._listener_ready.clear()
                    logger.error("Shared Redis listener error: %s", exc)
                    if not self._connections:
                        return
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 5.0)
                finally:
                    try:
                        await pubsub.punsubscribe("ws:*")
                        await pubsub.aclose()
                    except Exception:
                        logger.debug("Failed to close shared Redis subscription")
        finally:
            self._listener_ready.clear()


# ---------------------------------------------------------------------------
# Singleton — imported everywhere that needs to send to a WebSocket
# ---------------------------------------------------------------------------
manager = WebSocketManager()
