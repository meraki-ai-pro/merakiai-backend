"""
Live integration test: Learn, Review, and Application modes across all courses.
Tests text responses only (no video). Measures and reports response times.

Usage:
    python scripts/live_test.py
"""
import sys
import time
import json
import httpx

# Force UTF-8 output so emoji in AI responses don't crash on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "http://localhost:8000"
EMAIL = "craxybaboon6@gmail.com"
PASSWORD = "testuser123"
POLL_INTERVAL = 2      # seconds between status polls
POLL_TIMEOUT = 120     # max seconds to wait for AI response
TEXT_QUESTION = "Give me a brief explanation of the core concepts in this course."
REVIEW_QUESTION = "photosynthesis converts light energy into chemical energy"
APP_ANSWER = "I would apply the fundamental principles systematically to solve this problem step by step."


def _poll(client: httpx.Client, status_path: str, label: str) -> tuple[dict, float]:
    """Poll a status endpoint until ready. Returns (result, elapsed_seconds)."""
    start = time.monotonic()
    deadline = start + POLL_TIMEOUT
    while time.monotonic() < deadline:
        r = client.get(status_path, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "processing":
            time.sleep(POLL_INTERVAL)
            continue
        elapsed = time.monotonic() - start
        return data, elapsed
    raise TimeoutError(f"{label}: no result after {POLL_TIMEOUT}s")


def _banner(text: str):
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print('=' * 60)


def _extract_text(result: dict) -> str:
    """Pull the main text from any task result regardless of key name."""
    for key in ("response", "prompt", "next_prompt", "summary"):
        val = result.get(key)
        if val:
            return str(val)
    if "evaluation" in result:
        ev = result["evaluation"]
        return ev.get("feedback", str(ev)) if isinstance(ev, dict) else str(ev)
    return ""


def _ok(label: str, elapsed: float, preview: str = ""):
    snippet = (preview[:80] + "...") if len(preview) > 80 else preview
    print(f"  [OK]  {label:<30} {elapsed:5.1f}s  {snippet}")


def _fail(label: str, detail: str):
    print(f"  [FAIL] {label:<30} {detail}")


def run():
    results = []  # (course_id, mode, status, elapsed)

    # ── 1. Login ─────────────────────────────────────────────────────────────
    _banner("LOGIN")
    t0 = time.monotonic()
    r = httpx.post(f"{BASE_URL}/auth/login",
                   json={"email": EMAIL, "password": PASSWORD}, timeout=15)
    if r.status_code != 200:
        print(f"  Login FAILED ({r.status_code}): {r.text}")
        return
    token = r.json()["access_token"]
    print(f"  Logged in as {EMAIL}  ({(time.monotonic()-t0):.1f}s)")

    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=30) as client:

        # ── 2. List courses ───────────────────────────────────────────────────
        _banner("COURSES")
        r = client.get("/sessions/courses")
        r.raise_for_status()
        courses = r.json().get("courses", [])
        if not courses:
            print("  No courses found — aborting.")
            return
        for c in courses:
            print(f"  • {c['id']:30s}  {c['name']}")

        # ── 3. Test each course ───────────────────────────────────────────────
        for course in courses:
            cid = course["id"]
            cname = course["name"]
            _banner(f"COURSE: {cname} ({cid})")

            # ── Create one session per course (text-only) ─────────────────────
            r = client.post("/sessions/", json={
                "course_id": cid,
                "mode": "learn",
                "prefers_video": False,
            })
            if r.status_code != 200:
                print(f"  [FAIL] Create session: {r.status_code} {r.text}")
                continue
            session_id = r.json()["session_id"]
            print(f"  Session: {session_id}")

            # ── LEARN MODE ────────────────────────────────────────────────────
            print("\n  [LEARN MODE]")
            try:
                t0 = time.monotonic()
                r = client.post("/rag/turn", json={
                    "session_id": session_id,
                    "mode": "learn",
                    "message": TEXT_QUESTION,
                })
                r.raise_for_status()
                task_id = r.json()["task_id"]
                dispatch_ms = (time.monotonic() - t0) * 1000
                print(f"    Task dispatched in {dispatch_ms:.0f}ms  (task_id={task_id})")

                result, elapsed = _poll(client, f"/rag/status/{task_id}", "learn")
                if result.get("status") == "failed":
                    _fail("learn", result.get("error", "unknown error"))
                    results.append((cid, "learn", "FAIL", elapsed))
                else:
                    response_text = _extract_text(result)
                    _ok("learn", elapsed, response_text)
                    results.append((cid, "learn", "OK", elapsed))
            except Exception as e:
                _fail("learn", str(e))
                results.append((cid, "learn", "FAIL", 0))

            # ── REVIEW MODE ───────────────────────────────────────────────────
            print("\n  [REVIEW MODE]")
            try:
                # Update session mode to review
                client.patch(f"/sessions/{session_id}/mode",
                             json={"current_mode": "review"})

                # Start review mode session
                t0 = time.monotonic()
                r = client.post("/mode-sessions/start", json={
                    "session_id": session_id,
                    "mode": "review",
                    "session_type": "general",
                    "difficulty": "Basic",
                    "total_items": 3,
                })
                r.raise_for_status()
                body = r.json()
                ms_id = body["mode_session_id"]
                task_id = body["task_id"]
                dispatch_ms = (time.monotonic() - t0) * 1000
                print(f"    Start dispatched in {dispatch_ms:.0f}ms  (ms_id={ms_id})")

                # Wait for first question
                start_result, elapsed = _poll(client, f"/mode-sessions/status/{task_id}", "review/start")
                if start_result.get("status") == "failed":
                    _fail("review/start", start_result.get("error", "unknown"))
                    results.append((cid, "review", "FAIL", elapsed))
                else:
                    q_text = _extract_text(start_result)
                    print(f"    Question received in {elapsed:.1f}s")
                    print(f"    Q: {(q_text[:100] + '...') if len(q_text) > 100 else q_text}")

                    # Submit an answer
                    t0 = time.monotonic()
                    r = client.post(f"/mode-sessions/{ms_id}/turn",
                                    json={"message": REVIEW_QUESTION})
                    r.raise_for_status()
                    turn_task_id = r.json()["task_id"]

                    turn_result, turn_elapsed = _poll(
                        client, f"/mode-sessions/status/{turn_task_id}", "review/turn"
                    )
                    if turn_result.get("status") == "failed":
                        _fail("review/turn", turn_result.get("error", "unknown"))
                        results.append((cid, "review", "FAIL", elapsed + turn_elapsed))
                    else:
                        feedback = _extract_text(turn_result)
                        total_elapsed = elapsed + turn_elapsed
                        _ok("review (start+turn)", total_elapsed, feedback)
                        results.append((cid, "review", "OK", total_elapsed))

                    # End mode session cleanly
                    try:
                        client.post(f"/mode-sessions/{ms_id}/end")
                    except Exception:
                        pass

            except Exception as e:
                _fail("review", str(e))
                results.append((cid, "review", "FAIL", 0))

            # ── APPLICATION MODE ──────────────────────────────────────────────
            print("\n  [APPLICATION MODE]")
            try:
                # Update session mode to application
                client.patch(f"/sessions/{session_id}/mode",
                             json={"current_mode": "application"})

                t0 = time.monotonic()
                r = client.post("/mode-sessions/start", json={
                    "session_id": session_id,
                    "mode": "application",
                    "session_type": "general",
                    "difficulty": "Basic",
                })
                r.raise_for_status()
                body = r.json()
                ms_id = body["mode_session_id"]
                task_id = body["task_id"]
                dispatch_ms = (time.monotonic() - t0) * 1000
                print(f"    Start dispatched in {dispatch_ms:.0f}ms  (ms_id={ms_id})")

                # Wait for scenario step 1
                start_result, elapsed = _poll(
                    client, f"/mode-sessions/status/{task_id}", "application/start"
                )
                if start_result.get("status") == "failed":
                    _fail("application/start", start_result.get("error", "unknown"))
                    results.append((cid, "application", "FAIL", elapsed))
                else:
                    scenario = _extract_text(start_result)
                    print(f"    Scenario received in {elapsed:.1f}s")
                    print(f"    Scenario: {(scenario[:100] + '...') if len(scenario) > 100 else scenario}")

                    # Submit student answer to step 1
                    r = client.post(f"/mode-sessions/{ms_id}/turn",
                                    json={"message": APP_ANSWER})
                    r.raise_for_status()
                    turn_task_id = r.json()["task_id"]

                    turn_result, turn_elapsed = _poll(
                        client, f"/mode-sessions/status/{turn_task_id}", "application/turn"
                    )
                    if turn_result.get("status") == "failed":
                        _fail("application/turn", turn_result.get("error", "unknown"))
                        results.append((cid, "application", "FAIL", elapsed + turn_elapsed))
                    else:
                        feedback = _extract_text(turn_result)
                        total_elapsed = elapsed + turn_elapsed
                        _ok("application (start+turn)", total_elapsed, feedback)
                        results.append((cid, "application", "OK", total_elapsed))

                    # End mode session cleanly
                    try:
                        client.post(f"/mode-sessions/{ms_id}/end")
                    except Exception:
                        pass

            except Exception as e:
                _fail("application", str(e))
                results.append((cid, "application", "FAIL", 0))

            # End main session
            try:
                client.post(f"/sessions/{session_id}/end")
            except Exception:
                pass

    # ── 4. Summary ────────────────────────────────────────────────────────────
    _banner("RESULTS SUMMARY")
    print(f"  {'Course':<35} {'Mode':<15} {'Status':<8} {'Time':>6}")
    print(f"  {'-'*35} {'-'*15} {'-'*8} {'-'*6}")
    passed = failed = 0
    for cid, mode, status, elapsed in results:
        icon = "OK  " if status == "OK" else "FAIL"
        t = f"{elapsed:.1f}s" if elapsed else "  -"
        print(f"  {cid:<35} {mode:<15} {icon:<8} {t:>6}")
        if status == "OK":
            passed += 1
        else:
            failed += 1
    print(f"\n  Passed: {passed}   Failed: {failed}   Total: {passed+failed}")


if __name__ == "__main__":
    run()
