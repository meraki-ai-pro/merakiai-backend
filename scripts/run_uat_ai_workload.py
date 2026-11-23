#!/usr/bin/env python3
"""Run a staged, paid AI workload check against the isolated UAT stack.

The test models 25/50/100/200 signed-in students with a configurable share
issuing an AI request at the same instant.  Requests use the real embedding,
Pinecone retrieval, Anthropic generation, and short-answer marking paths.  A
temporary Celery worker consumes a dedicated queue at the same concurrency as
the production text worker, so the measured end-to-end time includes queueing.

No application users, sessions, conversations, or course rows are created.
The temporary result records and RabbitMQ queue are removed in ``finally``.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import Any

import boto3
from celery import shared_task
import psutil


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


DEFAULT_COHORTS = (25, 50, 100, 200)
PROBE_QUEUE = "uat_ai_probe"
PROBE_TASK = "scripts.run_uat_ai_workload.probe"
WORKER_PREFIX = "uat-ai-probe"

LEARN_QUESTIONS = (
    "In no more than four sentences, explain what a derivative represents geometrically.",
    "Briefly explain the product rule and give one simple example.",
    "Briefly explain how the chain rule is used when differentiating a composite function.",
    "Briefly distinguish a definite integral from an indefinite integral.",
    "In no more than four sentences, explain what a Riemann sum estimates.",
)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return ordered[index]


def task_count_for(cohort: int, active_ratio: float) -> int:
    return max(1, math.ceil(cohort * active_ratio))


def workload_kinds(count: int, review_ratio: float) -> list[str]:
    review_count = min(count, max(1, round(count * review_ratio))) if review_ratio else 0
    # Interleave the slower two-call review jobs instead of placing them all at
    # the front or back of the queue.
    review_positions = {
        min(count - 1, math.floor((index + 0.5) * count / review_count))
        for index in range(review_count)
    } if review_count else set()
    return ["review" if index in review_positions else "learn" for index in range(count)]


def _load_secret(secret_id: str, region: str) -> dict[str, str]:
    client = boto3.client("secretsmanager", region_name=region)
    payload = client.get_secret_value(SecretId=secret_id)
    raw = json.loads(payload["SecretString"])
    return {str(key): str(value) for key, value in raw.items() if value is not None}


def _apply_secret(secret: dict[str, str], secret_id: str, region: str) -> None:
    # Values stay in the process environment and are never printed or placed
    # on the worker command line.
    # Explicit runtime overrides win.  This is also how operators can recover
    # from one malformed field without copying the whole JSON secret elsewhere.
    for key, value in secret.items():
        os.environ.setdefault(key, value)
    os.environ["AWS_SECRET_ARN"] = secret_id
    os.environ["AWS_REGION"] = region


def safe_error_detail(value: Any) -> str:
    detail = str(value or "").replace("\r", " ").replace("\n", " ")[:200]
    lowered = detail.lower()
    if any(marker in lowered for marker in ("private key", "begin rsa", "begin ec")):
        return "[redacted credential-like value]"
    if "illegal header value" in lowered:
        return "[redacted invalid header value]"
    return detail


async def _execute_probe(
    kind: str,
    course_id: str,
    course_name: str,
    question: str,
) -> dict[str, Any]:
    from app.ai.rag.service import query_rag
    from app.ai.rag.modes_sessions.service import (
        evaluate_review_answer,
        generate_review_item,
    )
    from app.db.supabase import reset_async_supabase

    reset_async_supabase()
    started = time.perf_counter()

    if kind == "learn":
        result = await query_rag(
            user_message=question,
            mode="learn",
            course_id=course_id,
            course_persona="A concise, patient university mathematics tutor.",
            course_domain_topics=[
                "functions and limits",
                "derivatives",
                "integrals",
                "Riemann sums",
            ],
            concise=True,
            board=False,
            academic_level="level_100",
        )
        response = str(result.get("response") or "").strip()
        source_count = len(result.get("sources") or [])
        ok = bool(response) and source_count > 0
    elif kind == "review":
        item, contexts = await generate_review_item(
            session_type="short_answer",
            difficulty="Basic",
            course_id=course_id,
            course_name=course_name,
            difficulty_descriptors={},
        )
        marking = await evaluate_review_answer(
            item=item,
            student_answer=(
                "A derivative is an instantaneous rate of change and the slope "
                "of the tangent to the graph at a point."
            ),
            contexts=contexts,
            course_name=course_name,
        )
        response = json.dumps(marking, default=str)
        source_count = len(contexts)
        ok = bool(item) and bool(marking) and source_count > 0
    else:
        raise ValueError(f"Unsupported workload kind: {kind}")

    return {
        "ok": ok,
        "kind": kind,
        "worker_ms": round((time.perf_counter() - started) * 1_000, 1),
        "source_count": source_count,
        "response_chars": len(response),
    }


@shared_task(name=PROBE_TASK)
def probe_task(kind: str, course_id: str, course_name: str, question: str) -> dict[str, Any]:
    """Dedicated UAT-only task registered by the temporary worker."""
    try:
        return asyncio.run(_execute_probe(kind, course_id, course_name, question))
    except Exception as exc:  # noqa: BLE001 - the runner needs the provider error class
        cause = exc.__cause__
        return {
            "ok": False,
            "kind": kind,
            "error_type": type(exc).__name__,
            "error_module": type(exc).__module__,
        "error": safe_error_detail(exc),
            "cause_type": type(cause).__name__ if cause else None,
        "cause": safe_error_detail(cause) if cause else None,
            "worker_ms": 0.0,
            "source_count": 0,
            "response_chars": 0,
        }


@dataclass
class StageResult:
    cohort: int
    ai_requests: int
    learn_requests: int
    review_requests: int
    ok: int
    errors: int
    error_types: dict[str, int]
    error_modules: dict[str, int]
    error_samples: list[str]
    grounded: int
    grounded_percent: float
    end_to_end_p50_ms: float
    end_to_end_p95_ms: float
    end_to_end_p99_ms: float
    worker_p50_ms: float
    worker_p95_ms: float
    queue_wait_p95_ms: float
    peak_cpu_percent: float
    peak_memory_percent: float
    duration_seconds: float

    @property
    def passed(self) -> bool:
        return (
            self.ok == self.ai_requests
            and self.errors == 0
            and self.grounded_percent >= 95
            and self.end_to_end_p95_ms <= 60_000
            and self.peak_memory_percent <= 90
        )


def _celery_app():
    from app.core.celery_app import celery_app

    return celery_app


def _start_worker(concurrency: int, log_path: Path) -> tuple[subprocess.Popen, str]:
    hostname = f"{WORKER_PREFIX}-{secrets.token_hex(3)}@%h"
    env = os.environ.copy()
    env["CELERY_INCLUDE"] = "scripts.run_uat_ai_workload"
    command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "app.core.celery_app.celery_app",
        "worker",
        f"--queues={PROBE_QUEUE}",
        f"--concurrency={concurrency}",
        "--prefetch-multiplier=1",
        "--loglevel=WARNING",
        f"--hostname={hostname}",
        "--without-gossip",
        "--without-mingle",
        "--without-heartbeat",
    ]
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(  # noqa: S603 - fixed executable and arguments
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    process._uat_log_handle = log_handle  # type: ignore[attr-defined]
    return process, hostname.split("@", 1)[0]


def _wait_for_worker(process: subprocess.Popen, worker_prefix: str, timeout: float = 45) -> None:
    app = _celery_app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Temporary AI worker exited with code {process.returncode}")
        replies = app.control.inspect(timeout=1.0).active_queues() or {}
        if any(name.startswith(worker_prefix) for name in replies):
            return
        time.sleep(1)
    raise TimeoutError("Temporary AI worker did not become ready")


def _stop_worker(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    handle = getattr(process, "_uat_log_handle", None)
    if handle:
        handle.close()


def _delete_probe_queue() -> bool:
    try:
        app = _celery_app()
        with app.connection_for_write() as connection:
            connection.default_channel.queue_delete(queue=PROBE_QUEUE)
        return True
    except Exception:
        # Cleanup is reported by the final summary; it must not hide the test
        # results when the broker is already unavailable.
        return False


def _run_stage(args: argparse.Namespace, cohort: int) -> StageResult:
    app = _celery_app()
    request_count = task_count_for(cohort, args.active_ratio)
    kinds = workload_kinds(request_count, args.review_ratio)
    submitted: list[tuple[Any, float, str]] = []
    started = time.perf_counter()
    samples: list[tuple[float, float]] = []

    for index, kind in enumerate(kinds):
        question = LEARN_QUESTIONS[index % len(LEARN_QUESTIONS)]
        enqueued = time.perf_counter()
        result = app.send_task(
            PROBE_TASK,
            args=[kind, args.course_id, args.course_name, question],
            queue=PROBE_QUEUE,
        )
        submitted.append((result, enqueued, kind))

    deadline = time.monotonic() + args.stage_timeout_seconds
    pending = {result.id for result, _enqueued, _kind in submitted}
    completed_at: dict[str, float] = {}
    while pending and time.monotonic() < deadline:
        samples.append((psutil.cpu_percent(interval=None), psutil.virtual_memory().percent))
        still_pending: set[str] = set()
        for result, _enqueued, _kind in submitted:
            if result.id not in pending:
                continue
            if result.ready():
                completed_at[result.id] = time.perf_counter()
            else:
                still_pending.add(result.id)
        pending = still_pending
        if pending:
            time.sleep(0.25)

    outcomes: list[dict[str, Any]] = []
    end_to_end: list[float] = []
    worker_times: list[float] = []
    queue_waits: list[float] = []
    error_types: Counter[str] = Counter()
    error_modules: Counter[str] = Counter()
    error_samples: list[str] = []
    grounded = 0

    for result, enqueued, kind in submitted:
        elapsed = (completed_at.get(result.id, time.perf_counter()) - enqueued) * 1_000
        try:
            if not result.ready():
                app.control.revoke(result.id, terminate=False)
                payload = {"ok": False, "kind": kind, "error_type": "StageTimeout"}
            else:
                payload = result.get(timeout=1, propagate=False)
                if not isinstance(payload, dict):
                    payload = {"ok": False, "kind": kind, "error_type": "InvalidResult"}
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "kind": kind, "error_type": type(exc).__name__}
        finally:
            result.forget()

        outcomes.append(payload)
        end_to_end.append(elapsed)
        worker_ms = float(payload.get("worker_ms") or 0)
        worker_times.append(worker_ms)
        queue_waits.append(max(0.0, elapsed - worker_ms))
        if int(payload.get("source_count") or 0) > 0:
            grounded += 1
        if not payload.get("ok"):
            error_types[str(payload.get("error_type") or "UnknownError")] += 1
            error_modules[str(payload.get("error_module") or "unknown")] += 1
            if len(error_samples) < 3:
                detail = str(payload.get("cause") or payload.get("error") or "")[:200]
                error_samples.append(
                    ":".join(
                        part
                        for part in (
                            str(payload.get("error_module") or "unknown"),
                            str(payload.get("error_type") or "UnknownError"),
                            str(payload.get("cause_type") or ""),
                            detail,
                        )
                        if part
                    )
                )

    duration = time.perf_counter() - started
    cpu = [value for value, _memory in samples] or [0.0]
    memory = [value for _cpu, value in samples] or [0.0]
    ok = sum(bool(outcome.get("ok")) for outcome in outcomes)
    stage = StageResult(
        cohort=cohort,
        ai_requests=request_count,
        learn_requests=kinds.count("learn"),
        review_requests=kinds.count("review"),
        ok=ok,
        errors=request_count - ok,
        error_types=dict(sorted(error_types.items())),
        error_modules=dict(sorted(error_modules.items())),
        error_samples=error_samples,
        grounded=grounded,
        grounded_percent=round(100 * grounded / request_count, 1),
        end_to_end_p50_ms=round(percentile(end_to_end, 0.50), 1),
        end_to_end_p95_ms=round(percentile(end_to_end, 0.95), 1),
        end_to_end_p99_ms=round(percentile(end_to_end, 0.99), 1),
        worker_p50_ms=round(percentile(worker_times, 0.50), 1),
        worker_p95_ms=round(percentile(worker_times, 0.95), 1),
        queue_wait_p95_ms=round(percentile(queue_waits, 0.95), 1),
        peak_cpu_percent=round(max(cpu), 1),
        peak_memory_percent=round(max(memory), 1),
        duration_seconds=round(duration, 2),
    )
    print(json.dumps({"event": "ai_stage", **asdict(stage), "passed": stage.passed}), flush=True)
    return stage


def run(args: argparse.Namespace) -> int:
    secret = _load_secret(args.secret_id, args.region)
    required = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PINECONE_API_KEY", "PINECONE_INDEX")
    missing = [key for key in required if not secret.get(key)]
    if missing:
        raise RuntimeError("UAT AI configuration is incomplete: " + ", ".join(missing))
    _apply_secret(secret, args.secret_id, args.region)

    results: list[StageResult] = []
    worker: subprocess.Popen | None = None
    queue_deleted = False
    with tempfile.TemporaryDirectory(prefix="meraki-uat-ai-") as temp_dir:
        log_path = Path(temp_dir) / "worker.log"
        try:
            print(
                json.dumps(
                    {
                        "event": "ai_setup",
                        "cohorts": args.cohorts,
                        "active_ratio": args.active_ratio,
                        "review_ratio": args.review_ratio,
                        "worker_concurrency": args.worker_concurrency,
                        "course_id": args.course_id,
                    }
                ),
                flush=True,
            )
            worker, prefix = _start_worker(args.worker_concurrency, log_path)
            _wait_for_worker(worker, prefix)
            for cohort in sorted(set(args.cohorts)):
                stage = _run_stage(args, cohort)
                results.append(stage)
                if args.stop_on_failure and not stage.passed:
                    break
        finally:
            if worker is not None:
                _stop_worker(worker)
            queue_deleted = _delete_probe_queue()

    passed = len(results) == len(set(args.cohorts)) and all(stage.passed for stage in results)
    print(
        json.dumps(
            {
                "event": "ai_summary",
                "passed": passed,
                "completed_cohorts": [stage.cohort for stage in results],
                "queue_deleted": queue_deleted,
                "limits": {
                    "success_percent": 100,
                    "grounded_percent": 95,
                    "end_to_end_p95_ms": 60_000,
                    "peak_memory_percent": 90,
                },
            }
        ),
        flush=True,
    )
    return 0 if passed and queue_deleted else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-id", required=True)
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    parser.add_argument("--cohorts", nargs="+", type=int, default=list(DEFAULT_COHORTS))
    parser.add_argument("--active-ratio", type=float, default=0.20)
    parser.add_argument("--review-ratio", type=float, default=0.20)
    parser.add_argument("--worker-concurrency", type=int, default=8)
    parser.add_argument("--stage-timeout-seconds", type=float, default=900)
    parser.add_argument("--course-id", default="calculus-100")
    parser.add_argument("--course-name", default="Introduction to Calculus")
    parser.add_argument("--stop-on-failure", action="store_true")
    args = parser.parse_args(argv)
    if any(cohort < 1 or cohort > 500 for cohort in args.cohorts):
        parser.error("cohorts must be between 1 and 500")
    if not 0 < args.active_ratio <= 1:
        parser.error("active-ratio must be greater than 0 and at most 1")
    if not 0 <= args.review_ratio <= 1:
        parser.error("review-ratio must be between 0 and 1")
    if not 1 <= args.worker_concurrency <= 16:
        parser.error("worker-concurrency must be between 1 and 16")
    if args.stage_timeout_seconds < 30:
        parser.error("stage-timeout-seconds must be at least 30")
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
