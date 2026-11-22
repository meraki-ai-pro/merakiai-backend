r"""Capacity checks for staged 25-, 50-, 100-, and 200-student cohorts.

These tests deliberately use a real, disposable Redis database. They do not
create Supabase users, call AI providers, or touch production data.

Run from the backend repository with local Redis available::

    set LOAD_TEST_REDIS_URL=redis://127.0.0.1:6379/15
    .venv\Scripts\python -m pytest tests/load/test_pilot_capacity.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from statistics import median

import pytest
import redis
import redis.asyncio as aioredis
from fastapi import HTTPException
from starlette.requests import Request

from app.core.rate_limit import enforce_rate_limit, rate_limit
from app.core.websocket_manager import WebSocketManager


REDIS_URL = os.getenv("LOAD_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
PILOT_COHORTS = (25, 50, 100, 200)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
    return ordered[index]


@dataclass
class RecordingWebSocket:
    accepted: bool = False
    messages: list[tuple[float, dict]] = field(default_factory=list)

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self.messages.append((time.perf_counter(), data))


async def _wait_for_listener_startup(
    client: aioredis.Redis,
    session_ids: list[str],
    timeout_seconds: float = 3.0,
) -> int:
    """Return the highest number of subscribed pilot channels observed."""
    channels = [f"ws:{session_id}" for session_id in session_ids]
    deadline = time.perf_counter() + timeout_seconds
    highest = 0
    while time.perf_counter() < deadline:
        counts = await client.pubsub_numsub(*channels)
        subscribed = sum(1 for _channel, count in counts if count > 0)
        # The scalable implementation uses one ws:* pattern subscription for
        # the whole API process rather than one Redis connection per student.
        if await client.pubsub_numpat() > 0:
            subscribed = len(session_ids)
        highest = max(highest, subscribed)
        if subscribed == len(session_ids):
            break
        await asyncio.sleep(0.05)
    return highest


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_sessions", PILOT_COHORTS)
async def test_realtime_text_delivery_reaches_every_student(
    concurrent_sessions: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every connected student must receive a Redis-backed realtime message."""
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()

    manager = WebSocketManager()
    session_ids = [
        f"pilot-{concurrent_sessions}-{index}"
        for index in range(concurrent_sessions)
    ]
    sockets = {session_id: RecordingWebSocket() for session_id in session_ids}

    try:
        await asyncio.gather(
            *(manager.connect(session_id, sockets[session_id]) for session_id in session_ids)
        )
        subscribed = await _wait_for_listener_startup(client, session_ids)

        published_at: dict[str, float] = {}
        for session_id in session_ids:
            payload = {"type": "pilot_probe", "session_id": session_id}
            published_at[session_id] = time.perf_counter()
            await client.publish(f"ws:{session_id}", json.dumps(payload))

        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline:
            if sum(bool(socket.messages) for socket in sockets.values()) == concurrent_sessions:
                break
            await asyncio.sleep(0.02)

        delivered = {
            session_id: socket.messages[0][0] - published_at[session_id]
            for session_id, socket in sockets.items()
            if socket.messages
        }
        latencies_ms = [seconds * 1000 for seconds in delivered.values()]
        metrics = (
            f"subscribed={subscribed}/{concurrent_sessions}, "
            f"delivered={len(delivered)}/{concurrent_sessions}, "
            f"p50_ms={median(latencies_ms):.1f}, "
            f"p95_ms={_percentile(latencies_ms, 0.95):.1f}, "
            f"p99_ms={_percentile(latencies_ms, 0.99):.1f}"
            if latencies_ms
            else f"subscribed={subscribed}/{concurrent_sessions}, delivered=0/{concurrent_sessions}"
        )
        print(f"realtime cohort={concurrent_sessions}: {metrics}")
        assert len(delivered) == concurrent_sessions, metrics
    finally:
        await asyncio.gather(*(manager.disconnect(session_id) for session_id in session_ids))
        await manager._pool.aclose()
        await client.aclose()


def _login_request(client_ip: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/auth/login",
            "raw_path": b"/auth/login",
            "query_string": b"",
            "headers": [],
            "client": (client_ip, 443),
            "server": ("load.test", 443),
        }
    )


@pytest.mark.parametrize("cohort_size", PILOT_COHORTS)
def test_classroom_login_burst_is_not_blocked_by_shared_ip(
    cohort_size: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A class behind one school NAT should not be rejected before authentication."""
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    from app.core import rate_limit as rate_limit_module

    rate_limit_module._redis_pool = None
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()

    dependency = rate_limit(max_calls=300, window_seconds=60)
    client_ip = f"198.51.100.{cohort_size}"

    def attempt(student_number: int) -> bool:
        try:
            dependency(_login_request(client_ip), credentials=None)
            enforce_rate_limit(
                scope="auth:login:account",
                identifier=f"student-{cohort_size}-{student_number}@load.test",
                max_calls=5,
                window_seconds=60,
            )
            return True
        except HTTPException as exc:
            assert exc.status_code == 429
            return False

    try:
        with ThreadPoolExecutor(max_workers=cohort_size) as executor:
            outcomes = list(executor.map(attempt, range(cohort_size)))
        accepted = sum(outcomes)
        rejected = cohort_size - accepted
        shared_ip_count = int(
            client.get(f"rl:/auth/login:{client_ip}") or 0
        )
        account_key_count = sum(
            1 for _ in client.scan_iter(match="rl:auth:login:account:*")
        )
    finally:
        client.close()
        if rate_limit_module._redis_pool is not None:
            rate_limit_module._redis_pool.disconnect()
        rate_limit_module._redis_pool = None

    assert rejected == 0, (
        f"accepted={accepted}/{cohort_size}, rejected={rejected}/{cohort_size}; "
        "the login policy blocks a classroom sharing one public IP"
    )
    assert shared_ip_count == cohort_size
    assert account_key_count == cohort_size
    print(
        f"login cohort={cohort_size}: accepted={accepted}/{cohort_size}, "
        f"rejected={rejected}/{cohort_size}"
    )


def test_repeated_attempts_against_one_account_remain_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    from app.core import rate_limit as rate_limit_module

    rate_limit_module._redis_pool = None
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()

    rejected = 0
    try:
        for _ in range(6):
            try:
                enforce_rate_limit(
                    scope="auth:login:account",
                    identifier="same-student@load.test",
                    max_calls=5,
                    window_seconds=60,
                )
            except HTTPException as exc:
                assert exc.status_code == 429
                rejected += 1
    finally:
        client.close()
        if rate_limit_module._redis_pool is not None:
            rate_limit_module._redis_pool.disconnect()
        rate_limit_module._redis_pool = None

    assert rejected == 1
