from pathlib import Path

from scripts.run_uat_capacity import StageResult, percentile


ROOT = Path(__file__).resolve().parents[2]


def test_percentile_uses_the_requested_rank():
    values = [100.0, 20.0, 60.0, 80.0, 40.0]

    assert percentile(values, 0.50) == 60.0
    assert percentile(values, 0.95) == 100.0


def test_capacity_stage_requires_success_latency_and_headroom():
    healthy = StageResult(
        cohort=200,
        new_logins=100,
        login_ok=200,
        login_errors=0,
        login_statuses={"200": 200},
        login_p50_ms=800,
        login_p95_ms=2_000,
        login_p99_ms=2_500,
        websocket_ok=200,
        websocket_errors=0,
        websocket_p50_ms=300,
        websocket_p95_ms=900,
        websocket_p99_ms=1_100,
        cpu_p95_percent=60,
        peak_cpu_percent=70,
        peak_memory_percent=65,
        duration_seconds=8,
    )

    assert healthy.passed
    overloaded = StageResult(**{**healthy.__dict__, "login_ok": 199, "login_errors": 1})
    assert not overloaded.passed


def test_live_runner_uses_the_deployed_auth_route():
    runner = (ROOT / "scripts" / "run_uat_capacity.py").read_text()

    assert '"/auth/login"' in runner
    assert '"/api/v1/auth/login"' not in runner
