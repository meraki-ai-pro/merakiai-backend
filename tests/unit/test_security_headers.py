from fastapi.testclient import TestClient

from app.main import app


def test_api_security_headers_are_set():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), geolocation=(), microphone=()"


def test_hsts_is_set_when_tls_was_terminated_by_the_proxy():
    with TestClient(app) as client:
        response = client.get("/health", headers={"x-forwarded-proto": "https"})

    assert response.headers["strict-transport-security"].startswith("max-age=63072000")
