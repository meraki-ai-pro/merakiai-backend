"""
Comprehensive endpoint test — all non-video, non-admin endpoints.
Tests correctness and measures response time for each call.

Skipped (by design):
  POST /auth/signup        — would create a real user
  POST /auth/forgot-password — would send a real email
  POST /auth/update-password — would change real credentials
  GET  /auth/google/callback — requires browser OAuth code
  /ingestion/*             — admin-only
  /rag/turn/voice          — STT (audio upload)
  /mode-sessions/*/turn/voice — STT (audio upload)

Usage:
    python scripts/endpoint_test.py
"""
import sys
import time
import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "http://localhost:8000"
EMAIL    = "craxybaboon6@gmail.com"
PASSWORD = "testuser123"

POLL_INTERVAL = 2
POLL_TIMEOUT  = 120

results: list[tuple[str, str, str, float]] = []   # (group, endpoint, status, elapsed)


# ── helpers ──────────────────────────────────────────────────────────────────

def _t(label: str):
    print(f"\n  {label}")

def _ok(endpoint: str, elapsed: float, note: str = ""):
    tag = f"[OK]  {elapsed:.1f}s"
    print(f"    {tag:<18} {note[:80]}")
    results.append(("", endpoint, "OK", elapsed))

def _fail(endpoint: str, elapsed: float, detail: str):
    print(f"    [FAIL]            {detail[:100]}")
    results.append(("", endpoint, "FAIL", elapsed))

def _banner(title: str):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")

def _poll(client: httpx.Client, path: str) -> tuple[dict, float]:
    start = time.monotonic()
    deadline = start + POLL_TIMEOUT
    while time.monotonic() < deadline:
        r = client.get(path, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "processing":
            return data, time.monotonic() - start
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"No result within {POLL_TIMEOUT}s")

def _check(r: httpx.Response, endpoint: str, t0: float, note: str = "") -> dict | None:
    elapsed = time.monotonic() - t0
    if r.is_success:
        _ok(endpoint, elapsed, note or str(r.json())[:80])
        return r.json()
    else:
        _fail(endpoint, elapsed, f"{r.status_code} {r.text[:120]}")
        return None


# ── run ───────────────────────────────────────────────────────────────────────

def run():
    # ── HEALTH ───────────────────────────────────────────────────────────────
    _banner("HEALTH")
    _t("GET /health")
    t0 = time.monotonic()
    r = httpx.get(f"{BASE_URL}/health", timeout=5)
    _check(r, "GET /health", t0)

    # ── AUTH ─────────────────────────────────────────────────────────────────
    _banner("AUTH")

    _t("POST /auth/login")
    t0 = time.monotonic()
    r = httpx.post(f"{BASE_URL}/auth/login",
                   json={"email": EMAIL, "password": PASSWORD}, timeout=15)
    login_data = _check(r, "POST /auth/login", t0, f"user={r.json().get('user',{}).get('email')}" if r.is_success else "")
    if not login_data:
        print("  Cannot continue without a token.")
        return

    token         = login_data["access_token"]
    refresh_token = login_data["refresh_token"]
    headers       = {"Authorization": f"Bearer {token}"}

    _t("GET /auth/google/url")
    t0 = time.monotonic()
    r = httpx.get(f"{BASE_URL}/auth/google/url", timeout=10)
    _check(r, "GET /auth/google/url", t0, "url=" + (r.json().get("url","")[:60] if r.is_success else ""))

    _t("POST /auth/refresh")
    t0 = time.monotonic()
    r = httpx.post(f"{BASE_URL}/auth/refresh",
                   json={"refresh_token": refresh_token}, timeout=15)
    refresh_data = _check(r, "POST /auth/refresh", t0)
    if refresh_data:
        # Use the new token going forward
        token   = refresh_data["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        refresh_token = refresh_data["refresh_token"]

    # ── USERS ─────────────────────────────────────────────────────────────────
    _banner("USERS")

    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=15) as client:

        _t("GET /users/me")
        t0 = time.monotonic()
        r = client.get("/users/me")
        me = _check(r, "GET /users/me", t0, f"role={r.json().get('role')} avatar={r.json().get('avatar_id')}" if r.is_success else "")

        # Find an available avatar to test selection
        _t("GET avatar_voice_bundles (via sessions/courses path — anon read)")
        # We'll query via the service and pick first active avatar for the select test
        avatar_id_to_test = None
        try:
            import os, sys
            sys.path.insert(0, ".")
            from app.config import load_env; load_env()
            from app.db.supabase import get_supabase
            bundles = get_supabase().table("avatar_voice_bundles").select("avatar_id").eq("is_active", True).execute()
            if bundles.data:
                avatar_id_to_test = bundles.data[0]["avatar_id"]
                print(f"    Available avatars: {[b['avatar_id'] for b in bundles.data]}")
        except Exception as e:
            print(f"    Could not fetch bundles: {e}")

        if avatar_id_to_test:
            _t(f"POST /users/avatar (avatar_id={avatar_id_to_test})")
            t0 = time.monotonic()
            r = client.post("/users/avatar", json={"avatar_id": avatar_id_to_test})
            _check(r, "POST /users/avatar", t0,
                   f"voice_id={r.json().get('voice_id')}" if r.is_success else "")

            _t(f"PATCH /users/me/avatar (avatar_id={avatar_id_to_test})")
            t0 = time.monotonic()
            r = client.patch("/users/me/avatar", json={"avatar_id": avatar_id_to_test})
            _check(r, "PATCH /users/me/avatar", t0)
        else:
            print("    Skipping avatar select — no active bundles found")

        # ── SESSIONS ─────────────────────────────────────────────────────────
        _banner("SESSIONS")

        _t("GET /sessions/courses")
        t0 = time.monotonic()
        r = client.get("/sessions/courses")
        courses_data = _check(r, "GET /sessions/courses", t0,
                              f"{len(r.json().get('courses',[]))} courses" if r.is_success else "")
        courses = (courses_data or {}).get("courses", [])
        course_id = courses[0]["id"] if courses else "froth-flotation"

        _t("POST /sessions/  (create session)")
        t0 = time.monotonic()
        r = client.post("/sessions/", json={"course_id": course_id, "mode": "learn", "prefers_video": False})
        sess_data = _check(r, "POST /sessions/", t0, f"session_id={r.json().get('session_id')}" if r.is_success else "")
        session_id = (sess_data or {}).get("session_id")

        if not session_id:
            print("  Cannot continue session tests without session_id")
        else:
            _t(f"GET /sessions/{{session_id}}")
            t0 = time.monotonic()
            r = client.get(f"/sessions/{session_id}")
            _check(r, "GET /sessions/{id}", t0, f"mode={r.json().get('current_mode')}" if r.is_success else "")

            _t("PATCH /sessions/{session_id}/mode")
            t0 = time.monotonic()
            r = client.patch(f"/sessions/{session_id}/mode", json={"current_mode": "review"})
            _check(r, "PATCH /sessions/{id}/mode", t0, f"mode={r.json().get('current_mode')}" if r.is_success else "")

            # Reset back to learn
            client.patch(f"/sessions/{session_id}/mode", json={"current_mode": "learn"})

            _t("PATCH /sessions/{session_id}/video (prefers_video=false)")
            t0 = time.monotonic()
            r = client.patch(f"/sessions/{session_id}/video", json={"prefers_video": False})
            _check(r, "PATCH /sessions/{id}/video", t0)

            # ── RAG / LEARN ───────────────────────────────────────────────────
            _banner("RAG — LEARN MODE")

            _t("POST /rag/turn")
            t0 = time.monotonic()
            r = client.post("/rag/turn", json={
                "session_id": session_id, "mode": "learn",
                "message": "What is the purpose of frothers in flotation?",
            })
            turn_data = _check(r, "POST /rag/turn", t0, f"task_id={r.json().get('task_id')}" if r.is_success else "")
            task_id = (turn_data or {}).get("task_id")

            if task_id:
                _t(f"GET /rag/status/{{task_id}} (polling...)")
                t0 = time.monotonic()
                try:
                    result, elapsed = _poll(client, f"/rag/status/{task_id}")
                    if result.get("status") == "failed":
                        _fail("GET /rag/status/{id}", elapsed, result.get("error", "failed"))
                    else:
                        _ok("GET /rag/status/{id}", elapsed,
                            (result.get("response","")[:80]))
                except TimeoutError as e:
                    _fail("GET /rag/status/{id}", POLL_TIMEOUT, str(e))

            _t("GET /sessions/{session_id}/conversations")
            t0 = time.monotonic()
            r = client.get(f"/sessions/{session_id}/conversations")
            _check(r, "GET /sessions/{id}/conversations", t0,
                   f"{len(r.json().get('conversations',[]))} conversations" if r.is_success else "")

            # ── MODE SESSIONS — REVIEW ────────────────────────────────────────
            _banner("MODE SESSIONS — REVIEW")

            _t("POST /mode-sessions/start (review)")
            t0 = time.monotonic()
            r = client.post("/mode-sessions/start", json={
                "session_id": session_id,
                "mode": "review",
                "session_type": "general",
                "difficulty": "Basic",
                "total_items": 3,
            })
            ms_data = _check(r, "POST /mode-sessions/start (review)", t0,
                             f"ms_id={r.json().get('mode_session_id')}" if r.is_success else "")
            ms_id   = (ms_data or {}).get("mode_session_id")
            ms_task = (ms_data or {}).get("task_id")

            if ms_id and ms_task:
                _t("GET /mode-sessions/status/{task_id} (polling start...)")
                t0 = time.monotonic()
                try:
                    result, elapsed = _poll(client, f"/mode-sessions/status/{ms_task}")
                    if result.get("status") == "failed":
                        _fail("GET /mode-sessions/status/{id}", elapsed, result.get("error",""))
                    else:
                        _ok("GET /mode-sessions/status/{id}", elapsed,
                            (result.get("prompt","")[:80]))
                except TimeoutError as e:
                    _fail("GET /mode-sessions/status/{id}", POLL_TIMEOUT, str(e))

                _t("GET /mode-sessions/ (list)")
                t0 = time.monotonic()
                r = client.get(f"/mode-sessions/?session_id={session_id}")
                _check(r, "GET /mode-sessions/", t0,
                       f"{len(r.json().get('mode_sessions',[]))} sessions" if r.is_success else "")

                _t("POST /mode-sessions/{ms_id}/turn (review answer)")
                t0 = time.monotonic()
                r = client.post(f"/mode-sessions/{ms_id}/turn",
                                json={"message": "Frothers reduce surface tension to stabilise air bubbles."})
                turn_data = _check(r, "POST /mode-sessions/{id}/turn (review)", t0,
                                   f"task_id={r.json().get('task_id')}" if r.is_success else "")
                turn_task = (turn_data or {}).get("task_id")

                if turn_task:
                    _t("GET /mode-sessions/status/{task_id} (polling turn...)")
                    t0 = time.monotonic()
                    try:
                        result, elapsed = _poll(client, f"/mode-sessions/status/{turn_task}")
                        if result.get("status") == "failed":
                            _fail("GET /mode-sessions/status/{id} (turn)", elapsed, result.get("error",""))
                        else:
                            ev = result.get("evaluation") or {}
                            note = f"verdict={ev.get('verdict')} score={ev.get('score')}" if isinstance(ev,dict) else str(ev)[:80]
                            _ok("GET /mode-sessions/status/{id} (turn)", elapsed, note)
                    except TimeoutError as e:
                        _fail("GET /mode-sessions/status/{id} (turn)", POLL_TIMEOUT, str(e))

                _t("POST /mode-sessions/{ms_id}/end")
                t0 = time.monotonic()
                r = client.post(f"/mode-sessions/{ms_id}/end")
                _check(r, "POST /mode-sessions/{id}/end", t0)

            # ── MODE SESSIONS — APPLICATION ───────────────────────────────────
            _banner("MODE SESSIONS — APPLICATION")

            _t("POST /mode-sessions/start (application)")
            t0 = time.monotonic()
            r = client.post("/mode-sessions/start", json={
                "session_id": session_id,
                "mode": "application",
                "session_type": "general",
                "difficulty": "Basic",
            })
            ms_data = _check(r, "POST /mode-sessions/start (application)", t0,
                             f"ms_id={r.json().get('mode_session_id')}" if r.is_success else "")
            ms_id   = (ms_data or {}).get("mode_session_id")
            ms_task = (ms_data or {}).get("task_id")

            if ms_id and ms_task:
                _t("GET /mode-sessions/status/{task_id} (polling start...)")
                t0 = time.monotonic()
                try:
                    result, elapsed = _poll(client, f"/mode-sessions/status/{ms_task}")
                    if result.get("status") == "failed":
                        _fail("GET /mode-sessions/status/{id} (app start)", elapsed, result.get("error",""))
                    else:
                        _ok("GET /mode-sessions/status/{id} (app start)", elapsed,
                            (result.get("prompt","")[:80]))
                except TimeoutError as e:
                    _fail("GET /mode-sessions/status/{id} (app start)", POLL_TIMEOUT, str(e))

                _t("POST /mode-sessions/{ms_id}/turn (application answer)")
                t0 = time.monotonic()
                r = client.post(f"/mode-sessions/{ms_id}/turn",
                                json={"message": "I would first analyse the ore properties then adjust collector dosage accordingly."})
                turn_data = _check(r, "POST /mode-sessions/{id}/turn (application)", t0,
                                   f"task_id={r.json().get('task_id')}" if r.is_success else "")
                turn_task = (turn_data or {}).get("task_id")

                if turn_task:
                    _t("GET /mode-sessions/status/{task_id} (polling turn...)")
                    t0 = time.monotonic()
                    try:
                        result, elapsed = _poll(client, f"/mode-sessions/status/{turn_task}")
                        if result.get("status") == "failed":
                            _fail("GET /mode-sessions/status/{id} (app turn)", elapsed, result.get("error",""))
                        else:
                            ev = result.get("evaluation") or {}
                            note = f"type={result.get('type')} score={ev.get('score') if isinstance(ev,dict) else '?'}"
                            _ok("GET /mode-sessions/status/{id} (app turn)", elapsed, note)
                    except TimeoutError as e:
                        _fail("GET /mode-sessions/status/{id} (app turn)", POLL_TIMEOUT, str(e))

                _t("POST /mode-sessions/{ms_id}/end")
                t0 = time.monotonic()
                r = client.post(f"/mode-sessions/{ms_id}/end")
                _check(r, "POST /mode-sessions/{id}/end (app)", t0)

            # ── FEEDBACK ──────────────────────────────────────────────────────
            _banner("FEEDBACK")

            _t("POST /feedback/session-survey")
            t0 = time.monotonic()
            r = client.post("/feedback/session-survey", json={
                "session_id": session_id,
                "clarity_rating": 4,
                "helpfulness_rating": 5,
                "confidence_rating": 4,
                "overall_rating": 4,
            })
            _check(r, "POST /feedback/session-survey", t0,
                   f"id={r.json().get('id')}" if r.is_success else "")

            _t("POST /feedback/user-feedback")
            t0 = time.monotonic()
            r = client.post("/feedback/user-feedback", json={
                "session_id": session_id,
                "feedback_type": "suggestion",
                "message": "Automated endpoint test — please ignore.",
            })
            _check(r, "POST /feedback/user-feedback", t0,
                   f"id={r.json().get('id')}" if r.is_success else "")

            # ── END SESSION ───────────────────────────────────────────────────
            _banner("SESSION CLEANUP")
            _t("POST /sessions/{session_id}/end")
            t0 = time.monotonic()
            r = client.post(f"/sessions/{session_id}/end")
            _check(r, "POST /sessions/{id}/end", t0)

    # ── AUTH LOGOUT ───────────────────────────────────────────────────────────
    _banner("AUTH — LOGOUT")
    _t("POST /auth/logout")
    t0 = time.monotonic()
    r = httpx.post(f"{BASE_URL}/auth/logout", headers=headers, timeout=10)
    _check(r, "POST /auth/logout", t0)

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    _banner("RESULTS SUMMARY")
    passed = [x for x in results if x[2] == "OK"]
    failed = [x for x in results if x[2] == "FAIL"]

    print(f"  {'Endpoint':<45} {'Status':<8} {'Time':>6}")
    print(f"  {'-'*45} {'-'*8} {'-'*6}")
    for _, ep, status, elapsed in results:
        icon = "OK  " if status == "OK" else "FAIL"
        t = f"{elapsed:.1f}s" if elapsed else "  -"
        print(f"  {ep:<45} {icon:<8} {t:>6}")

    print(f"\n  Passed: {len(passed)}   Failed: {len(failed)}   Total: {len(results)}")


if __name__ == "__main__":
    run()
