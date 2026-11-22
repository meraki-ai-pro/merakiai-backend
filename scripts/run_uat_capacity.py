#!/usr/bin/env python3
"""Run an isolated, staged capacity check against the live UAT environment.

The runner creates temporary Supabase Auth users, exercises the public login
and WebSocket paths at 25/50/100/200 concurrency, and deletes the users in a
finally block. It never calls an AI model or writes application course data.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
import secrets
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from urllib.parse import quote, urlparse

import boto3
import httpx
import psutil
import redis.asyncio as aioredis
import websockets


DEFAULT_COHORTS = (25, 50, 100, 200)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return ordered[index]


@dataclass
class StageResult:
    cohort: int
    login_ok: int
    login_errors: int
    login_statuses: dict[str, int]
    login_p50_ms: float
    login_p95_ms: float
    login_p99_ms: float
    websocket_ok: int
    websocket_errors: int
    websocket_p50_ms: float
    websocket_p95_ms: float
    websocket_p99_ms: float
    peak_cpu_percent: float
    peak_memory_percent: float
    duration_seconds: float

    @property
    def passed(self) -> bool:
        return (
            self.login_ok == self.cohort
            and self.login_errors == 0
            and self.websocket_ok == self.cohort
            and self.websocket_errors == 0
            and self.login_p95_ms <= 5_000
            and self.websocket_p95_ms <= 2_000
            and self.peak_cpu_percent <= 90
            and self.peak_memory_percent <= 90
        )


def _load_secret(secret_id: str, region: str) -> dict[str, str]:
    client = boto3.client("secretsmanager", region_name=region)
    payload = client.get_secret_value(SecretId=secret_id)
    return json.loads(payload["SecretString"])


def _websocket_url(base_url: str, session_id: str, token: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws/{session_id}?token={quote(token, safe='')}"


async def _delete_rate_limit_keys(redis_url: str) -> None:
    client = aioredis.from_url(redis_url, decode_responses=True)
    try:
        for pattern in (
            "rl:/auth/login:*",
            "rl:auth:login:account:*",
        ):
            keys = [key async for key in client.scan_iter(match=pattern, count=500)]
            if keys:
                await client.delete(*keys)
    finally:
        await client.aclose()


async def _sample_resources(stop: asyncio.Event, samples: list[tuple[float, float]]) -> None:
    psutil.cpu_percent(interval=None)
    while not stop.is_set():
        samples.append((psutil.cpu_percent(interval=None), psutil.virtual_memory().percent))
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.2)
        except TimeoutError:
            pass


async def _create_users(
    client: httpx.AsyncClient,
    count: int,
    run_id: str,
    password: str,
) -> list[tuple[str, str]]:
    semaphore = asyncio.Semaphore(20)

    async def create_one(index: int) -> tuple[str, str]:
        email = f"capacity-{run_id}-{index:03d}@load-test.merakiai.online"
        async with semaphore:
            response = await client.post(
                "/auth/v1/admin/users",
                json={"email": email, "password": password, "email_confirm": True},
            )
        response.raise_for_status()
        return email, str(response.json()["id"])

    return list(await asyncio.gather(*(create_one(index) for index in range(count))))


async def _delete_users(
    client: httpx.AsyncClient,
    users: list[tuple[str, str]],
) -> int:
    semaphore = asyncio.Semaphore(20)

    async def delete_one(user_id: str) -> bool:
        try:
            async with semaphore:
                response = await client.delete(f"/auth/v1/admin/users/{user_id}")
            return response.status_code in (200, 204, 404)
        except Exception:
            return False

    outcomes = await asyncio.gather(*(delete_one(user_id) for _email, user_id in users))
    return sum(outcomes)


async def _login_users(
    client: httpx.AsyncClient,
    users: list[tuple[str, str]],
    password: str,
) -> tuple[list[str], list[float], int, dict[str, int]]:
    async def login_one(email: str) -> tuple[str | None, float, str]:
        started = time.perf_counter()
        try:
            response = await client.post(
                "/auth/login",
                json={"email": email, "password": password},
            )
            elapsed = (time.perf_counter() - started) * 1_000
            if response.status_code != 200:
                return None, elapsed, str(response.status_code)
            token = response.json().get("access_token")
            return (str(token) if token else None), elapsed, str(response.status_code)
        except Exception:
            return None, (time.perf_counter() - started) * 1_000, "exception"

    outcomes = await asyncio.gather(*(login_one(email) for email, _user_id in users))
    tokens = [token for token, _elapsed, _status in outcomes if token]
    latencies = [elapsed for token, elapsed, _status in outcomes if token]
    statuses = dict(sorted(Counter(status for _token, _elapsed, status in outcomes).items()))
    return tokens, latencies, len(outcomes) - len(tokens), statuses


async def _probe_websockets(
    base_url: str,
    tokens: list[str],
    cohort: int,
) -> tuple[list[float], int]:
    ready = asyncio.Event()
    connected = 0
    connected_lock = asyncio.Lock()

    async def probe_one(index: int, token: str) -> tuple[bool, float]:
        nonlocal connected
        uri = _websocket_url(base_url, f"capacity-{cohort}-{index}", token)
        started = time.perf_counter()
        try:
            async with websockets.connect(
                uri,
                open_timeout=20,
                close_timeout=5,
                ping_interval=None,
                max_size=64 * 1024,
            ) as socket:
                async with connected_lock:
                    connected += 1
                    if connected == cohort:
                        ready.set()
                await asyncio.wait_for(ready.wait(), timeout=20)
                await socket.send("not-json")
                payload = json.loads(await asyncio.wait_for(socket.recv(), timeout=20))
                elapsed = (time.perf_counter() - started) * 1_000
                return payload.get("error") == "Invalid JSON", elapsed
        except Exception:
            return False, (time.perf_counter() - started) * 1_000

    outcomes = await asyncio.gather(
        *(probe_one(index, token) for index, token in enumerate(tokens))
    )
    latencies = [elapsed for ok, elapsed in outcomes if ok]
    return latencies, len(outcomes) - len(latencies)


async def _run_stage(
    base_url: str,
    users: list[tuple[str, str]],
    password: str,
    redis_url: str,
) -> StageResult:
    cohort = len(users)
    await _delete_rate_limit_keys(redis_url)
    samples: list[tuple[float, float]] = []
    stop = asyncio.Event()
    sampler = asyncio.create_task(_sample_resources(stop, samples))
    started = time.perf_counter()

    limits = httpx.Limits(max_connections=max(250, cohort + 25), max_keepalive_connections=50)
    async with httpx.AsyncClient(base_url=base_url, timeout=30, limits=limits) as client:
        tokens, login_latencies, login_errors, login_statuses = await _login_users(
            client,
            users,
            password,
        )
    websocket_latencies, websocket_errors = await _probe_websockets(
        base_url,
        tokens,
        cohort,
    )

    stop.set()
    await sampler
    duration = time.perf_counter() - started
    cpu_values = [cpu for cpu, _memory in samples] or [0.0]
    memory_values = [memory for _cpu, memory in samples] or [0.0]

    result = StageResult(
        cohort=cohort,
        login_ok=len(tokens),
        login_errors=login_errors,
        login_statuses=login_statuses,
        login_p50_ms=round(percentile(login_latencies, 0.50), 1),
        login_p95_ms=round(percentile(login_latencies, 0.95), 1),
        login_p99_ms=round(percentile(login_latencies, 0.99), 1),
        websocket_ok=len(websocket_latencies),
        websocket_errors=websocket_errors + max(0, cohort - len(tokens)),
        websocket_p50_ms=round(percentile(websocket_latencies, 0.50), 1),
        websocket_p95_ms=round(percentile(websocket_latencies, 0.95), 1),
        websocket_p99_ms=round(percentile(websocket_latencies, 0.99), 1),
        peak_cpu_percent=round(max(cpu_values), 1),
        peak_memory_percent=round(max(memory_values), 1),
        duration_seconds=round(duration, 2),
    )
    print(json.dumps({"event": "stage", **asdict(result), "passed": result.passed}), flush=True)
    return result


async def run(args: argparse.Namespace) -> int:
    secret = _load_secret(args.secret_id, args.region)
    supabase_url = str(secret.get("SUPABASE_URL") or "").rstrip("/")
    service_key = str(secret.get("SUPABASE_SERVICE_ROLE_KEY") or "")
    redis_url = str(secret.get("REDIS_URL") or "redis://127.0.0.1:6379/0")
    if not supabase_url or not service_key:
        raise RuntimeError("UAT Supabase configuration is incomplete")

    cohorts = tuple(sorted(set(args.cohorts)))
    maximum = max(cohorts)
    run_id = f"{int(time.time())}-{secrets.token_hex(3)}"
    password = f"Uat-{secrets.token_urlsafe(24)}"
    users: list[tuple[str, str]] = []
    results: list[StageResult] = []

    admin_headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    }
    limits = httpx.Limits(max_connections=30, max_keepalive_connections=20)
    async with httpx.AsyncClient(
        base_url=supabase_url,
        headers=admin_headers,
        timeout=30,
        limits=limits,
    ) as admin_client:
        try:
            print(json.dumps({"event": "setup", "temporary_users": maximum}), flush=True)
            users = await _create_users(admin_client, maximum, run_id, password)
            for cohort in cohorts:
                results.append(
                    await _run_stage(
                        args.base_url.rstrip("/"),
                        users[:cohort],
                        password,
                        redis_url,
                    )
                )
        finally:
            deleted = await _delete_users(admin_client, users)
            print(
                json.dumps(
                    {
                        "event": "cleanup",
                        "temporary_users_created": len(users),
                        "temporary_users_deleted": deleted,
                    }
                ),
                flush=True,
            )

    passed = len(results) == len(cohorts) and all(result.passed for result in results)
    print(
        json.dumps(
            {
                "event": "summary",
                "passed": passed,
                "cohorts": list(cohorts),
                "temporary_users_remaining": max(0, len(users) - deleted),
                "limits": {
                    "login_p95_ms": 5_000,
                    "websocket_p95_ms": 2_000,
                    "peak_cpu_percent": 90,
                    "peak_memory_percent": 90,
                },
            }
        ),
        flush=True,
    )
    return 0 if passed and deleted == len(users) else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--secret-id", required=True)
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    parser.add_argument("--cohorts", nargs="+", type=int, default=list(DEFAULT_COHORTS))
    args = parser.parse_args(argv)
    if any(cohort < 1 or cohort > 500 for cohort in args.cohorts):
        parser.error("cohorts must be between 1 and 500")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args(sys.argv[1:]))))
