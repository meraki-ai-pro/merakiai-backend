"""
Shared fixtures available to all test modules.

Environment variables (override for different environments):
    TEST_BASE_URL   default: http://localhost:8000
    TEST_WS_URL     default: ws://localhost:8000
    TEST_EMAIL      default: tarape1801@flownue.com
    TEST_PASSWORD   default: root1234
"""
import os
import pytest
import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8000")
WS_BASE_URL = os.getenv("TEST_WS_URL", "ws://localhost:8000")
TEST_EMAIL = os.getenv("TEST_EMAIL", "tarape1801@flownue.com")
TEST_PASSWORD = os.getenv("TEST_PASSWORD", "root1234")

AI_RESPONSE_TIMEOUT = int(os.getenv("AI_RESPONSE_TIMEOUT", "90"))  # seconds


# ---------------------------------------------------------------------------
# Server availability guard
# ---------------------------------------------------------------------------

def is_server_running() -> bool:
    try:
        httpx.get(f"{BASE_URL}/health", timeout=3)
        return True
    except Exception:
        return False


requires_server = pytest.mark.skipif(
    not is_server_running(),
    reason="Live server not running — skipping e2e/adversarial tests",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def ws_base_url() -> str:
    return WS_BASE_URL


@pytest.fixture(scope="session")
def auth_token(base_url) -> str:
    """Login once per test session and return a valid JWT."""
    resp = httpx.post(
        f"{base_url}/auth/login",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        timeout=15,
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture(scope="session")
def auth_headers(auth_token) -> dict:
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture
def http_client(base_url, auth_headers):
    """Authenticated synchronous HTTP client (function-scoped)."""
    with httpx.Client(base_url=base_url, headers=auth_headers, timeout=30) as client:
        yield client


@pytest.fixture
def session_id(http_client) -> str:
    """
    Create a fresh chat session via the API and return its ID.
    Cleaned up (ended) after the test.
    """
    resp = http_client.post("/sessions/")
    assert resp.status_code == 200, f"Session creation failed: {resp.text}"
    sid = resp.json()["session_id"]
    yield sid
    # Cleanup: end the session
    try:
        http_client.post(f"/sessions/{sid}/end")
    except Exception:
        pass


@pytest.fixture
def text_session_id(http_client) -> str:
    """Session with video turned OFF (text responses only)."""
    resp = http_client.post("/sessions/")
    assert resp.status_code == 200
    sid = resp.json()["session_id"]

    http_client.patch(f"/sessions/{sid}/video", json={"prefers_video": False})
    yield sid
    try:
        http_client.post(f"/sessions/{sid}/end")
    except Exception:
        pass


@pytest.fixture
def video_session_id(http_client) -> str:
    """Session with video turned ON."""
    resp = http_client.post("/sessions/")
    assert resp.status_code == 200
    sid = resp.json()["session_id"]

    http_client.patch(f"/sessions/{sid}/video", json={"prefers_video": True})
    yield sid
    try:
        http_client.post(f"/sessions/{sid}/end")
    except Exception:
        pass
