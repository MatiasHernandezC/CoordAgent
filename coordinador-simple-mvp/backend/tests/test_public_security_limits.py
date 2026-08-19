from fastapi.testclient import TestClient

from app.main import app
from app.security import request_limiter
from app.settings import settings


def test_auth_requests_are_rate_limited(monkeypatch):
    request_limiter.clear()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 100)
    monkeypatch.setattr(settings, "rate_limit_global_per_minute", 100)
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 2)
    with TestClient(app) as client:
        assert client.get("/api/auth/me").status_code != 429
        assert client.get("/api/auth/me").status_code != 429
        blocked = client.get("/api/auth/me")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    request_limiter.clear()


def test_oversized_request_is_rejected(monkeypatch):
    request_limiter.clear()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "max_request_body_bytes", 16)
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/register",
            content=b"x" * 17,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    request_limiter.clear()
