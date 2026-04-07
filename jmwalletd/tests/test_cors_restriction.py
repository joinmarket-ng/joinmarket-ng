from __future__ import annotations

from fastapi.testclient import TestClient


def test_cors_untrusted_origin_rejected(app: TestClient) -> None:
    """A request from an untrusted origin must NOT have its origin echoed back."""
    origin = "http://evil.com"
    resp = app.get("/api/v1/getinfo", headers={"Origin": origin})
    # FastAPI's CORSMiddleware does NOT add the header if the origin is not allowed.
    assert "access-control-allow-origin" not in resp.headers


def test_cors_trusted_localhost_accepted(app: TestClient) -> None:
    """Requests from localhost (any port) must have their origin echoed back."""
    origins = [
        "http://localhost",
        "http://localhost:3000",
        "http://127.0.0.1",
        "http://127.0.0.1:8080",
        "http://[::1]",
        "http://[::1]:1234",
    ]
    for origin in origins:
        resp = app.get("/api/v1/getinfo", headers={"Origin": origin})
        assert resp.headers.get("access-control-allow-origin") == origin


def test_cors_root_preflight_handler(app: TestClient) -> None:
    """The manual OPTIONS handler for / must also respect the origin policy."""
    # Untrusted
    resp = app.options("/", headers={"Origin": "http://malicious.org"})
    # It should fallback to a safe default or match the origin if allowed.
    # In my implementation, it falls back to http://localhost:28183
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:28183"

    # Trusted
    origin = "http://localhost:29183"
    resp = app.options("/", headers={"Origin": origin})
    assert resp.headers.get("access-control-allow-origin") == origin
