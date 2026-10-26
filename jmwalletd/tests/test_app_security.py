"""Security-focused tests for app routing behavior."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_spa_catch_all_blocks_path_traversal(app_with_jam_assets: TestClient) -> None:
    """Path traversal attempts must not escape the JAM static directory."""
    resp = app_with_jam_assets.get("/../../etc/passwd")
    assert resp.status_code == 200
    assert "root:" not in resp.text
    assert "<html>jam</html>" in resp.text


def test_docs_and_openapi_remain_available_without_auth(app_with_jam_assets: TestClient) -> None:
    """Operational docs should be readable without wallet auth."""
    docs = app_with_jam_assets.get("/docs")
    assert docs.status_code == 200

    openapi = app_with_jam_assets.get("/openapi.json")
    assert openapi.status_code == 200


def test_invalid_token_rejected_on_session_endpoint(app_with_jam_assets: TestClient) -> None:
    """Invalid bearer tokens must still be rejected."""
    resp = app_with_jam_assets.get(
        "/api/v1/session",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith('Bearer, error="invalid_token"')


def test_root_serves_spa_index(app_with_jam_assets: TestClient) -> None:
    """Root path should serve JAM SPA entrypoint when assets exist."""
    resp = app_with_jam_assets.get("/")
    assert resp.status_code == 200
    assert "<html>jam</html>" in resp.text


def test_oversized_request_body_rejected(app: TestClient) -> None:
    body = b'{"password": "' + b"x" * (2 * 1024 * 1024) + b'"}'
    url = "/api/v1/wallet/w.jmdat/unlock"
    headers = {"content-type": "application/json"}

    assert app.post(url, content=body, headers=headers).status_code == 413
    # Without a Content-Length header the body arrives chunked.
    chunked = app.post(url, content=iter([body[:1024], body[1024:]]), headers=headers)
    assert chunked.status_code == 413


def test_large_legitimate_request_body_accepted(app: TestClient) -> None:
    entries = [{"utxo-string": f"{'ab' * 32}:{i}", "freeze": True} for i in range(500)]
    resp = app.post("/api/v1/wallet/w.jmdat/freeze-batch", json={"entries": entries})
    assert resp.status_code != 413
