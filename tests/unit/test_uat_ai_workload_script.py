from __future__ import annotations

import math
import sys

import pytest

from scripts.run_uat_ai_workload import (
    BACKEND,
    StageResult,
    parse_args,
    percentile,
    task_count_for,
    workload_kinds,
)


def test_runner_adds_project_root_when_started_by_ssm_from_another_directory():
    assert str(BACKEND) in sys.path


def test_active_population_is_converted_to_ai_burst_size():
    assert task_count_for(25, 0.20) == 5
    assert task_count_for(200, 0.20) == 40
    assert task_count_for(3, 0.01) == 1


def test_workload_mix_includes_real_marking_without_changing_total():
    kinds = workload_kinds(40, 0.20)
    assert len(kinds) == 40
    assert kinds.count("review") == 8
    assert kinds.count("learn") == 32


def test_zero_review_ratio_is_learn_only():
    assert workload_kinds(5, 0) == ["learn"] * 5


def test_percentile_is_nan_for_no_samples():
    assert math.isnan(percentile([], 0.95))


def test_stage_requires_success_grounding_latency_and_memory():
    base = dict(
        cohort=200,
        ai_requests=40,
        learn_requests=32,
        review_requests=8,
        ok=40,
        errors=0,
        error_types={},
        grounded=40,
        grounded_percent=100.0,
        end_to_end_p50_ms=20_000,
        end_to_end_p95_ms=59_999,
        end_to_end_p99_ms=60_000,
        worker_p50_ms=8_000,
        worker_p95_ms=12_000,
        queue_wait_p95_ms=48_000,
        peak_cpu_percent=80,
        peak_memory_percent=50,
        duration_seconds=60,
    )
    assert StageResult(**base).passed
    assert not StageResult(**{**base, "ok": 39, "errors": 1}).passed
    assert not StageResult(**{**base, "grounded_percent": 94.9}).passed
    assert not StageResult(**{**base, "end_to_end_p95_ms": 60_001}).passed
    assert not StageResult(**{**base, "peak_memory_percent": 90.1}).passed


@pytest.mark.parametrize(
    "argv",
    [
        ["--secret-id", "uat/test", "--active-ratio", "0"],
        ["--secret-id", "uat/test", "--active-ratio", "1.1"],
        ["--secret-id", "uat/test", "--review-ratio", "-0.1"],
        ["--secret-id", "uat/test", "--worker-concurrency", "0"],
        ["--secret-id", "uat/test", "--stage-timeout-seconds", "29"],
    ],
)
def test_invalid_safety_limits_are_rejected(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)
