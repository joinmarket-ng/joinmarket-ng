"""Tests for jmwalletd.deps — FastAPI dependency injection."""

from __future__ import annotations

from unittest.mock import MagicMock

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from jmwalletd.deps import (
    _extract_bearer_token,
    get_daemon_state,
    require_auth,
    require_auth_allow_expired,
    require_wallet_session,
    set_daemon_state,
)
from jmwalletd.errors import InvalidToken, JMWalletDaemonError, WalletNotFound
from jmwalletd.state import DaemonState


def _make_test_app_with_handler() -> FastAPI:
    """Create a minimal FastAPI app with the same exception handler as the real app."""
    app = FastAPI()

    @app.exception_handler(JMWalletDaemonError)
    async def handle_daemon_error(request, exc: JMWalletDaemonError):  # type: ignore[no-untyped-def]
        headers = {}
        if exc.status_code in (401, 403):
            headers["WWW-Authenticate"] = 'Bearer realm="JoinMarket"'
        return JSONResponse(
            status_code=exc.status_code,
            content={"message": exc.detail},
            headers=headers,
        )

    return app


class TestSetGetDaemonState:
    def test_get_before_set_raises(self) -> None:
        import jmwalletd.deps as deps_mod

        old = deps_mod._daemon_state
        try:
            deps_mod._daemon_state = None
            with pytest.raises(RuntimeError, match="not initialized"):
                get_daemon_state()
        finally:
            deps_mod._daemon_state = old

    def test_round_trip(self, daemon_state: DaemonState) -> None:
        set_daemon_state(daemon_state)
        assert get_daemon_state() is daemon_state


class TestExtractBearerToken:
    def test_standard_header(self) -> None:
        request = MagicMock()
        request.headers = {"authorization": "Bearer tok123"}
        assert _extract_bearer_token(request) == "tok123"

    def test_custom_jm_header(self) -> None:
        request = MagicMock()
        request.headers = {"x-jm-authorization": "Bearer tok456"}
        assert _extract_bearer_token(request) == "tok456"

    def test_standard_takes_precedence(self) -> None:
        request = MagicMock()
        request.headers = {
            "authorization": "Bearer standard",
            "x-jm-authorization": "Bearer custom",
        }
        assert _extract_bearer_token(request) == "standard"

    def test_no_header(self) -> None:
        request = MagicMock()
        request.headers = {}
        assert _extract_bearer_token(request) is None

    def test_non_bearer(self) -> None:
        request = MagicMock()
        request.headers = {"authorization": "Basic abc123"}
        assert _extract_bearer_token(request) is None

    def test_case_insensitive(self) -> None:
        request = MagicMock()
        request.headers = {"authorization": "BEARER tok789"}
        assert _extract_bearer_token(request) == "tok789"


def test_wallet_session_captures_and_revalidates(
    daemon_state_with_wallet: DaemonState, auth_token: str
) -> None:
    state = daemon_state_with_wallet
    service, generation = require_wallet_session(state, auth_token, state.wallet_name)
    assert service is state.wallet_service
    assert generation == state.wallet_generation
    with pytest.raises(WalletNotFound):
        require_wallet_session(state, auth_token, "another-wallet.jmdat")
    state.wallet_service = None
    with pytest.raises(WalletNotFound):
        require_wallet_session(state, auth_token, state.wallet_name)
    state.token_authority.reset()
    with pytest.raises(InvalidToken):
        require_wallet_session(state, auth_token, state.wallet_name)


class TestRequireAuth:
    """Integration tests using a minimal FastAPI app with exception handler."""

    @pytest.fixture
    def _app(self, daemon_state_with_wallet: DaemonState) -> TestClient:
        app = _make_test_app_with_handler()

        @app.get("/protected")
        async def protected(auth: dict = pytest.importorskip("fastapi").Depends(require_auth)):  # type: ignore[assignment]
            return {"ok": True, "scope": auth.get("scope")}

        return TestClient(app)

    def test_no_token_returns_401(self, _app: TestClient) -> None:
        resp = _app.get("/protected")
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self, _app: TestClient) -> None:
        resp = _app.get("/protected", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401

    def test_valid_token_succeeds(self, _app: TestClient, auth_token: str) -> None:
        resp = _app.get("/protected", headers={"Authorization": f"Bearer {auth_token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_x_jm_authorization_header(self, _app: TestClient, auth_token: str) -> None:
        resp = _app.get(
            "/protected",
            headers={"x-jm-authorization": f"Bearer {auth_token}"},
        )
        assert resp.status_code == 200


class TestRequireAuthAllowExpired:
    """Test that expired tokens are accepted by require_auth_allow_expired."""

    @pytest.fixture
    def _app(self, daemon_state_with_wallet: DaemonState) -> TestClient:
        app = _make_test_app_with_handler()

        @app.get("/refresh-check")
        async def refresh_check(
            auth: dict = pytest.importorskip("fastapi").Depends(require_auth_allow_expired),  # type: ignore[assignment]
        ):
            return {"ok": True}

        return TestClient(app)

    def test_no_token_returns_401(self, _app: TestClient) -> None:
        resp = _app.get("/refresh-check")
        assert resp.status_code == 401

    def test_expired_token_accepted(
        self, _app: TestClient, daemon_state_with_wallet: DaemonState
    ) -> None:
        import time

        authority = daemon_state_with_wallet.token_authority
        # Create a token that expired in the past
        payload = {
            "exp": int(time.time()) - 100,
            "scope": authority.scope,
        }
        expired_token = jwt.encode(payload, authority._access_key, algorithm="HS256")
        resp = _app.get(
            "/refresh-check",
            headers={"Authorization": f"Bearer {expired_token}"},
        )
        assert resp.status_code == 200
