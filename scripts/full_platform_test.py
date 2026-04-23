"""
full_platform_test.py — End-to-end platform test for Meraki AI.

Tests every significant endpoint in both the client and admin modules.
Measures response time, records pass/fail, and writes a markdown report.

Usage:
    python scripts/full_platform_test.py
"""
from __future__ import annotations

import sys
import time
import json
import statistics
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "http://localhost:8000"
EMAIL    = "craxybaboon6@gmail.com"
PASSWORD = "testuser123"

POLL_INTERVAL = 2
POLL_TIMEOUT  = 120

REPORT_PATH = "docs/test_report.md"


# ─────────────────────────────────────────────────────────────────────────────
# Result tracking
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TestResult:
    section: str
    name: str
    method: str
    path: str
    status_code: int | None
    elapsed_ms: float
    passed: bool
    note: str = ""


_results: list[TestResult] = []


def _record(section, name, method, path, status_code, elapsed_ms, passed, note=""):
    r = TestResult(section, name, method, path, status_code, elapsed_ms, passed, note)
    _results.append(r)
    icon = "PASS" if passed else "FAIL"
    print(f"  [{icon}] {name:<42} {elapsed_ms:7.0f}ms  HTTP {status_code or '---'}  {note}")
    return r


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def _req(client: httpx.Client, method: str, path: str, section: str, name: str, **kwargs) -> tuple[httpx.Response, float]:
    t0 = time.monotonic()
    try:
        r = client.request(method, path, timeout=30, **kwargs)
    except Exception as e:
        elapsed = (time.monotonic() - t0) * 1000
        _record(section, name, method, path, None, elapsed, False, str(e)[:80])
        return None, elapsed
    elapsed = (time.monotonic() - t0) * 1000
    return r, elapsed


def _expect(r: httpx.Response | None, elapsed: float, section, name, method, path, expected=200, note=""):
    if r is None:
        return None
    passed = r.status_code == expected
    actual_note = note
    if not passed:
        try:
            detail = r.json().get("detail", r.text[:80])
        except Exception:
            detail = r.text[:80]
        actual_note = f"{note} | {detail}".strip(" |")
    _record(section, name, method, path, r.status_code, elapsed, passed, actual_note)
    return r.json() if passed else None


def _poll(client: httpx.Client, status_path: str, label: str) -> tuple[dict | None, float]:
    start = time.monotonic()
    deadline = start + POLL_TIMEOUT
    while time.monotonic() < deadline:
        r = client.get(status_path, timeout=10)
        if r.status_code != 200:
            return None, (time.monotonic() - start) * 1000
        data = r.json()
        if data.get("status") == "processing":
            time.sleep(POLL_INTERVAL)
            continue
        return data, (time.monotonic() - start) * 1000
    return None, POLL_TIMEOUT * 1000


def _banner(text: str):
    print(f"\n{'═' * 62}")
    print(f"  {text}")
    print('═' * 62)


def _extract_text(result: dict) -> str:
    for key in ("response", "prompt", "next_prompt", "summary", "message"):
        val = result.get(key)
        if val:
            return str(val)[:120]
    if "evaluation" in result:
        ev = result["evaluation"]
        return str(ev.get("feedback", ev))[:120] if isinstance(ev, dict) else str(ev)[:120]
    return json.dumps(result)[:120]


# ─────────────────────────────────────────────────────────────────────────────
# Main test runner
# ─────────────────────────────────────────────────────────────────────────────

def run():
    run_ts = datetime.now(timezone.utc)
    token: str = ""
    user_id: str = ""
    user_role: str = ""

    # ── AUTH ──────────────────────────────────────────────────────────────────
    _banner("AUTH")

    t0 = time.monotonic()
    try:
        raw = httpx.post(f"{BASE_URL}/auth/login",
                         json={"email": EMAIL, "password": PASSWORD}, timeout=15)
        ms = (time.monotonic() - t0) * 1000
        passed = raw.status_code == 200
        if passed:
            body = raw.json()
            token = body["access_token"]
            user_id = body["user"]["id"]
        _record("Auth", "POST /auth/login", "POST", "/auth/login",
                raw.status_code, ms, passed)
        if not passed:
            print(f"  Cannot continue without token. Response: {raw.text[:200]}")
            return
    except Exception as e:
        _record("Auth", "POST /auth/login", "POST", "/auth/login",
                None, 0, False, str(e)[:80])
        return

    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(base_url=BASE_URL, headers=headers) as client:

        # ── USERS/ME ──────────────────────────────────────────────────────────
        r, ms = _req(client, "GET", "/users/me", "Auth", "GET /users/me")
        data = _expect(r, ms, "Auth", "GET /users/me", "GET", "/users/me")
        if data:
            user_role = data.get("role", "user")
            print(f"       Role: {user_role}  |  Avatar: {data.get('avatar_id', 'none')}")

        # ─────────────────────────────────────────────────────────────────────
        # CLIENT MODULE
        # ─────────────────────────────────────────────────────────────────────
        _banner("CLIENT — Sessions & Courses")

        # List courses
        r, ms = _req(client, "GET", "/sessions/courses", "Client", "GET /sessions/courses")
        data = _expect(r, ms, "Client", "GET /sessions/courses", "GET", "/sessions/courses")
        courses = data.get("courses", []) if data else []
        if courses:
            print(f"       Courses found: {[c['id'] for c in courses]}")

        first_course = courses[0]["id"] if courses else "default"

        # Create session
        r, ms = _req(client, "POST", "/sessions/",
                     "Client", "POST /sessions/ (create)",
                     json={"course_id": first_course, "mode": "learn", "prefers_video": False})
        data = _expect(r, ms, "Client", "POST /sessions/ (create)", "POST", "/sessions/")
        session_id = data.get("session_id") if data else None

        if session_id:
            # ── LEARN MODE ────────────────────────────────────────────────────
            _banner("CLIENT — Learn Mode (RAG)")

            r, ms = _req(client, "POST", "/rag/turn",
                         "Client", "POST /rag/turn (dispatch)",
                         json={"session_id": session_id, "mode": "learn",
                               "message": "Explain the core concepts of this course briefly."})
            data = _expect(r, ms, "Client", "POST /rag/turn (dispatch)", "POST", "/rag/turn")
            task_id = data.get("task_id") if data else None

            if task_id:
                t0 = time.monotonic()
                result, poll_ms = _poll(client, f"/rag/status/{task_id}", "learn")
                total_ms = poll_ms
                passed = result is not None and result.get("status") != "failed"
                preview = _extract_text(result) if result else ""
                _record("Client", "GET /rag/status (learn poll)", "GET",
                        f"/rag/status/{task_id}", 200 if passed else 500,
                        total_ms, passed, preview[:60])

            # ── REVIEW MODE ───────────────────────────────────────────────────
            _banner("CLIENT — Review Mode")

            r, ms = _req(client, "PATCH", f"/sessions/{session_id}/mode",
                         "Client", f"PATCH /sessions/{{id}}/mode → review",
                         json={"current_mode": "review"})
            _expect(r, ms, "Client", "PATCH /sessions/{id}/mode → review",
                    "PATCH", f"/sessions/{session_id}/mode")

            r, ms = _req(client, "POST", "/mode-sessions/start",
                         "Client", "POST /mode-sessions/start (review)",
                         json={"session_id": session_id, "mode": "review",
                               "session_type": "general", "difficulty": "Basic", "total_items": 2})
            data = _expect(r, ms, "Client", "POST /mode-sessions/start (review)",
                           "POST", "/mode-sessions/start")
            review_ms_id = review_task_id = None
            if data:
                review_ms_id = data.get("mode_session_id")
                review_task_id = data.get("task_id")

            first_q = None
            if review_task_id:
                result, poll_ms = _poll(client, f"/mode-sessions/status/{review_task_id}", "review/start")
                passed = result is not None and result.get("status") != "failed"
                first_q = _extract_text(result) if result else ""
                _record("Client", "GET /mode-sessions/status (review Q1)", "GET",
                        f"/mode-sessions/status/{review_task_id}",
                        200 if passed else 500, poll_ms, passed, first_q[:60])

            if review_ms_id and first_q:
                r, ms = _req(client, "POST", f"/mode-sessions/{review_ms_id}/turn",
                             "Client", "POST /mode-sessions/{{id}}/turn (review answer)",
                             json={"message": "photosynthesis converts light into chemical energy"})
                data = _expect(r, ms, "Client", "POST /mode-sessions/{id}/turn (review answer)",
                               "POST", f"/mode-sessions/{review_ms_id}/turn")
                if data:
                    turn_task = data.get("task_id")
                    result, poll_ms = _poll(client, f"/mode-sessions/status/{turn_task}", "review/turn")
                    passed = result is not None and result.get("status") != "failed"
                    _record("Client", "GET /mode-sessions/status (review eval)", "GET",
                            f"/mode-sessions/status/{turn_task}",
                            200 if passed else 500, poll_ms, passed,
                            _extract_text(result)[:60] if result else "")

                r, ms = _req(client, "POST", f"/mode-sessions/{review_ms_id}/end",
                             "Client", "POST /mode-sessions/{{id}}/end (review)")
                _expect(r, ms, "Client", "POST /mode-sessions/{id}/end (review)",
                        "POST", f"/mode-sessions/{review_ms_id}/end")

            # ── APPLICATION MODE ──────────────────────────────────────────────
            _banner("CLIENT — Application Mode")

            r, ms = _req(client, "PATCH", f"/sessions/{session_id}/mode",
                         "Client", "PATCH /sessions/{{id}}/mode → application",
                         json={"current_mode": "application"})
            _expect(r, ms, "Client", "PATCH /sessions/{id}/mode → application",
                    "PATCH", f"/sessions/{session_id}/mode")

            r, ms = _req(client, "POST", "/mode-sessions/start",
                         "Client", "POST /mode-sessions/start (application)",
                         json={"session_id": session_id, "mode": "application",
                               "session_type": "general", "difficulty": "Basic"})
            data = _expect(r, ms, "Client", "POST /mode-sessions/start (application)",
                           "POST", "/mode-sessions/start")
            app_ms_id = app_task_id = None
            if data:
                app_ms_id = data.get("mode_session_id")
                app_task_id = data.get("task_id")

            if app_task_id:
                result, poll_ms = _poll(client, f"/mode-sessions/status/{app_task_id}", "app/start")
                passed = result is not None and result.get("status") != "failed"
                scenario = _extract_text(result) if result else ""
                _record("Client", "GET /mode-sessions/status (app scenario)", "GET",
                        f"/mode-sessions/status/{app_task_id}",
                        200 if passed else 500, poll_ms, passed, scenario[:60])

            if app_ms_id:
                r, ms = _req(client, "POST", f"/mode-sessions/{app_ms_id}/turn",
                             "Client", "POST /mode-sessions/{{id}}/turn (app answer)",
                             json={"message": "I would apply the fundamental principles to solve this systematically."})
                data = _expect(r, ms, "Client", "POST /mode-sessions/{id}/turn (app answer)",
                               "POST", f"/mode-sessions/{app_ms_id}/turn")
                if data:
                    turn_task = data.get("task_id")
                    result, poll_ms = _poll(client, f"/mode-sessions/status/{turn_task}", "app/turn")
                    passed = result is not None and result.get("status") != "failed"
                    _record("Client", "GET /mode-sessions/status (app eval)", "GET",
                            f"/mode-sessions/status/{turn_task}",
                            200 if passed else 500, poll_ms, passed,
                            _extract_text(result)[:60] if result else "")

                r, ms = _req(client, "POST", f"/mode-sessions/{app_ms_id}/end",
                             "Client", "POST /mode-sessions/{{id}}/end (application)")
                _expect(r, ms, "Client", "POST /mode-sessions/{id}/end (application)",
                        "POST", f"/mode-sessions/{app_ms_id}/end")

            # ── FEEDBACK ──────────────────────────────────────────────────────
            _banner("CLIENT — Feedback")

            r, ms = _req(client, "POST", "/feedback/session-survey",
                         "Client", "POST /feedback/session-survey",
                         json={"session_id": session_id, "clarity_rating": 4,
                               "helpfulness_rating": 5, "confidence_rating": 4, "overall_rating": 5})
            _expect(r, ms, "Client", "POST /feedback/session-survey",
                    "POST", "/feedback/session-survey")

            r, ms = _req(client, "POST", "/feedback/user-feedback",
                         "Client", "POST /feedback/user-feedback",
                         json={"session_id": session_id, "feedback_type": "suggestion",
                               "message": "Automated test feedback — platform working well."})
            _expect(r, ms, "Client", "POST /feedback/user-feedback",
                    "POST", "/feedback/user-feedback")

            # End session
            r, ms = _req(client, "POST", f"/sessions/{session_id}/end",
                         "Client", "POST /sessions/{{id}}/end")
            _expect(r, ms, "Client", "POST /sessions/{id}/end",
                    "POST", f"/sessions/{session_id}/end")

        # ─────────────────────────────────────────────────────────────────────
        # ADMIN MODULE
        # ─────────────────────────────────────────────────────────────────────
        _banner(f"ADMIN MODULE  (user role: {user_role})")

        if user_role not in ("admin", "super_admin"):
            print("  NOTE: user is not admin — admin endpoints will return 403.")
            print("        Promote user in Supabase to get full admin coverage.\n")

        # ── Analytics ─────────────────────────────────────────────────────────
        for name, path in [
            ("GET /admin/analytics/overview",         "/admin/analytics/overview"),
            ("GET /admin/analytics/users",            "/admin/analytics/users?days=30"),
            ("GET /admin/analytics/sessions",         "/admin/analytics/sessions?days=30"),
            ("GET /admin/analytics/feedback",         "/admin/analytics/feedback?days=90"),
            ("GET /admin/analytics/learning-outcomes","/admin/analytics/learning-outcomes?days=30"),
            ("GET /admin/analytics/performance",      "/admin/analytics/performance?days=30"),
        ]:
            expected = 200 if user_role in ("admin", "super_admin") else 403
            r, ms = _req(client, "GET", path, "Admin – Analytics", name)
            _expect(r, ms, "Admin – Analytics", name, "GET", path, expected=expected)

        # ── User Management ───────────────────────────────────────────────────
        expected = 200 if user_role in ("admin", "super_admin") else 403

        r, ms = _req(client, "GET", "/admin/users?page=1&page_size=5",
                     "Admin – Users", "GET /admin/users (list)")
        data = _expect(r, ms, "Admin – Users", "GET /admin/users (list)",
                       "GET", "/admin/users", expected=expected)

        test_uid = user_id
        if data and data.get("users"):
            test_uid = data["users"][0]["id"]

        r, ms = _req(client, "GET", f"/admin/users/{test_uid}",
                     "Admin – Users", "GET /admin/users/{id} (detail)")
        _expect(r, ms, "Admin – Users", "GET /admin/users/{id} (detail)",
                "GET", f"/admin/users/{test_uid}", expected=expected)

        # Role update — only against a different user to avoid self-update block
        # We intentionally test with the same user to verify the guard fires (400)
        r, ms = _req(client, "PATCH", f"/admin/users/{user_id}/role",
                     "Admin – Users", "PATCH /admin/users/{id}/role (self → expect 400)",
                     json={"role": "admin"})
        # 400 = cannot change own role, 403 = not admin — both are "correct" guards
        if r is not None:
            passed = r.status_code in (400, 403)
            _record("Admin – Users", "PATCH /admin/users/{id}/role (self → guard)",
                    "PATCH", f"/admin/users/{user_id}/role",
                    r.status_code, ms, passed,
                    "guard fired correctly" if passed else "unexpected response")

        # ── Document Management ───────────────────────────────────────────────
        r, ms = _req(client, "GET", "/admin/documents?page=1&page_size=5",
                     "Admin – Documents", "GET /admin/documents (list)")
        data = _expect(r, ms, "Admin – Documents", "GET /admin/documents (list)",
                       "GET", "/admin/documents", expected=expected)

        doc_id = None
        if data and data.get("documents"):
            doc_id = data["documents"][0]["id"]
            r, ms = _req(client, "GET", f"/admin/documents/{doc_id}",
                         "Admin – Documents", "GET /admin/documents/{id} (detail)")
            _expect(r, ms, "Admin – Documents", "GET /admin/documents/{id} (detail)",
                    "GET", f"/admin/documents/{doc_id}", expected=expected)
        else:
            print("       No documents to detail-test.")

        # ── Course Management ─────────────────────────────────────────────────
        r, ms = _req(client, "GET", "/admin/courses",
                     "Admin – Courses", "GET /admin/courses (list)")
        data = _expect(r, ms, "Admin – Courses", "GET /admin/courses (list)",
                       "GET", "/admin/courses", expected=expected)

        course_id = None
        if data and data.get("courses"):
            course_id = data["courses"][0]["id"]
            r, ms = _req(client, "GET", f"/admin/courses/{course_id}",
                         "Admin – Courses", "GET /admin/courses/{id} (detail)")
            _expect(r, ms, "Admin – Courses", "GET /admin/courses/{id} (detail)",
                    "GET", f"/admin/courses/{course_id}", expected=expected)

        # ── LLM Management ────────────────────────────────────────────────────
        r, ms = _req(client, "GET", "/admin/llm/config",
                     "Admin – LLM", "GET /admin/llm/config")
        data = _expect(r, ms, "Admin – LLM", "GET /admin/llm/config",
                       "GET", "/admin/llm/config", expected=expected)
        if data:
            cfg = data.get("config", {})
            for mode, mcfg in cfg.items():
                print(f"       {mode:<20} → {mcfg.get('model', '?')}")

        r, ms = _req(client, "GET", "/admin/llm/usage?days=30",
                     "Admin – LLM", "GET /admin/llm/usage")
        _expect(r, ms, "Admin – LLM", "GET /admin/llm/usage",
                "GET", "/admin/llm/usage", expected=expected)

        # PATCH LLM config (non-destructive — same values)
        r, ms = _req(client, "PATCH", "/admin/llm/config/learn",
                     "Admin – LLM", "PATCH /admin/llm/config/learn (temperature)",
                     json={"temperature": 0.4})
        _expect(r, ms, "Admin – LLM", "PATCH /admin/llm/config/learn (temperature)",
                "PATCH", "/admin/llm/config/learn", expected=expected)

        # Reset guard (super_admin only)
        r, ms = _req(client, "POST", "/admin/llm/config/reset",
                     "Admin – LLM", "POST /admin/llm/config/reset (super_admin guard)")
        if r is not None:
            expected_reset = 200 if user_role == "super_admin" else 403
            _expect(r, ms, "Admin – LLM", "POST /admin/llm/config/reset (guard)",
                    "POST", "/admin/llm/config/reset", expected=expected_reset)

    # ─────────────────────────────────────────────────────────────────────────
    # REPORT
    # ─────────────────────────────────────────────────────────────────────────
    _banner("TEST SUMMARY")

    passed_count  = sum(1 for r in _results if r.passed)
    failed_count  = sum(1 for r in _results if not r.passed)
    total_count   = len(_results)
    all_ms        = [r.elapsed_ms for r in _results if r.elapsed_ms > 0]
    ai_poll_ms    = [r.elapsed_ms for r in _results if "poll" in r.name.lower() or "status" in r.name.lower()]

    print(f"  Passed : {passed_count}/{total_count}")
    print(f"  Failed : {failed_count}/{total_count}")
    if all_ms:
        print(f"  Fastest: {min(all_ms):.0f}ms")
        print(f"  Slowest: {max(all_ms):.0f}ms")
        print(f"  Median : {statistics.median(all_ms):.0f}ms")
    if ai_poll_ms:
        print(f"  AI poll median: {statistics.median(ai_poll_ms):.0f}ms")

    _write_report(run_ts, user_role, passed_count, failed_count, total_count, all_ms, ai_poll_ms)
    print(f"\n  Report written → {REPORT_PATH}")


def _write_report(
    run_ts, user_role, passed, failed, total,
    all_ms: list[float], ai_poll_ms: list[float]
):
    sections: dict[str, list[TestResult]] = {}
    for r in _results:
        sections.setdefault(r.section, []).append(r)

    def _ms(v):
        return f"{v:.0f} ms" if v is not None else "—"

    lines = [
        f"# Meraki AI — Full Platform Test Report",
        f"",
        f"**Run date:** {run_ts.strftime('%Y-%m-%d %H:%M:%S UTC')}  ",
        f"**Test user:** `{EMAIL}`  ",
        f"**User role:** `{user_role}`  ",
        f"**Server:** `{BASE_URL}`  ",
        f"",
        f"## Summary",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total tests | {total} |",
        f"| Passed | {passed} |",
        f"| Failed | {failed} |",
        f"| Pass rate | {passed/total*100:.1f}% |",
    ]

    if all_ms:
        lines += [
            f"| Fastest response | {_ms(min(all_ms))} |",
            f"| Slowest response | {_ms(max(all_ms))} |",
            f"| Median response | {_ms(statistics.median(all_ms))} |",
            f"| Mean response | {_ms(statistics.mean(all_ms))} |",
        ]
    if ai_poll_ms:
        lines += [
            f"| AI task median (poll) | {_ms(statistics.median(ai_poll_ms))} |",
            f"| AI task min | {_ms(min(ai_poll_ms))} |",
            f"| AI task max | {_ms(max(ai_poll_ms))} |",
        ]

    lines += ["", "---", ""]

    for section, results in sections.items():
        lines.append(f"## {section}")
        lines.append("")
        lines.append(f"| # | Test | Method | Path | HTTP | Time | Result | Note |")
        lines.append(f"|---|------|--------|------|------|------|--------|------|")
        for i, r in enumerate(results, 1):
            icon = "PASS" if r.passed else "FAIL"
            http = str(r.status_code) if r.status_code else "—"
            note = r.note.replace("|", "·")[:60]
            lines.append(
                f"| {i} | {r.name} | `{r.method}` | `{r.path[:55]}` "
                f"| {http} | {_ms(r.elapsed_ms)} | **{icon}** | {note} |"
            )
        lines.append("")

        # Per-section timing stats
        ms_vals = [r.elapsed_ms for r in results if r.elapsed_ms > 0]
        if ms_vals:
            lines.append(
                f"*Section timing — min: {_ms(min(ms_vals))}  "
                f"median: {_ms(statistics.median(ms_vals))}  "
                f"max: {_ms(max(ms_vals))}*"
            )
            lines.append("")

    lines += [
        "---",
        "",
        "## Performance Notes",
        "",
        "- **AI task latency** includes Celery queue dispatch + RAG retrieval + Claude API call.",
        "- Polling times above represent wall-clock time from dispatch to final result.",
        "- All non-AI endpoints should be <500 ms under normal load.",
        "- Video generation endpoints are excluded from this test run (text-only).",
        "",
        f"*Generated by `scripts/full_platform_test.py`*",
    ]

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    run()
