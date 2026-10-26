"""FastAPI dependency injection helpers.

Provides ``get_daemon_state``, ``get_bearer_token``, ``require_auth``, and
``require_wallet_match`` which are used by route handlers to access the daemon
state and enforce authentication and resource ownership.
"""

from __future__ import annotations

from typing import Any

import jwt
from fastapi import Depends, Request
from loguru import logger

from jmwalletd.errors import InvalidToken, NoWalletFound, WalletNotFound
from jmwalletd.state import DaemonState

# Module-level singleton -- set by ``create_app()``.
_daemon_state: DaemonState | None = None


def set_daemon_state(state: DaemonState) -> None:
    """Set the global daemon state singleton (called once at startup)."""
    global _daemon_state
    _daemon_state = state


def get_daemon_state() -> DaemonState:
    """FastAPI dependency that returns the daemon state."""
    if _daemon_state is None:
        msg = "Daemon state not initialized"
        raise RuntimeError(msg)
    return _daemon_state


def _extract_bearer_token(request: Request) -> str | None:
    """Extract a bearer token from the request.

    Checks both the standard ``Authorization`` header and the custom
    ``x-jm-authorization`` header that JAM uses.
    """
    for header_name in ("authorization", "x-jm-authorization"):
        value = request.headers.get(header_name)
        if value and value.lower().startswith("bearer "):
            return value[7:].strip()
    return None


def get_optional_token(request: Request) -> str | None:
    """Extract bearer token if present, or return None."""
    return _extract_bearer_token(request)


def require_auth(
    request: Request,
    state: DaemonState = Depends(get_daemon_state),
) -> dict[str, Any]:
    """FastAPI dependency that enforces bearer token authentication.

    Returns the decoded JWT payload on success.

    Raises:
        InvalidToken: If the token is missing, invalid, or expired.
        NoWalletFound: If no wallet is currently loaded.
    """
    if not state.wallet_loaded:
        raise NoWalletFound()

    token = _extract_bearer_token(request)
    if not token:
        raise InvalidToken("No authorization token provided.")

    try:
        payload = state.token_authority.verify_access(token)
    except jwt.InvalidTokenError as exc:
        logger.debug("Token verification failed: {}", exc)
        raise InvalidToken(str(exc)) from exc

    return payload


def require_current_session(state: DaemonState, token: str | None) -> None:
    """Re-verify a request's token right before it starts wallet-bound work.

    ``require_auth`` runs before the handler body, with awaits in between, so a
    wallet lock may have started since. Locking resets the token keys before its
    first await but only stops work that already exists. Call this with no await
    between it and starting the work: a token that still verifies here means a
    later lock will see, and stop, that work.

    Raises:
        InvalidToken: If the session that authorized the request has ended.
    """
    try:
        state.token_authority.verify_access(token or "")
    except jwt.InvalidTokenError as exc:
        raise InvalidToken(str(exc)) from exc


def require_auth_allow_expired(
    request: Request,
    state: DaemonState = Depends(get_daemon_state),
) -> dict[str, Any]:
    """Like ``require_auth`` but accepts expired access tokens.

    Used for the token-refresh endpoint where the access token may be expired
    but still needs to be structurally valid.
    """
    if not state.wallet_loaded:
        raise NoWalletFound()

    token = _extract_bearer_token(request)
    if not token:
        raise InvalidToken("No authorization token provided.")

    try:
        payload = state.token_authority.verify_access(token, verify_exp=False)
    except jwt.InvalidTokenError as exc:
        logger.debug("Token verification failed: {}", exc)
        raise InvalidToken(str(exc)) from exc

    return payload


def require_wallet_match(
    walletname: str,
    state: DaemonState = Depends(get_daemon_state),
) -> None:
    """FastAPI path-dependency that validates the ``walletname`` URL parameter.

    Every route whose URL contains ``{walletname}`` must include this
    dependency to prevent IDOR: an authenticated client with a valid token
    must only be able to act on the wallet that is actually loaded, not any
    arbitrary name they pass in the path.

    Raises:
        WalletNotFound: If no wallet is loaded, or if the requested walletname
            does not match the currently loaded wallet.
    """
    if not state.wallet_loaded or state.wallet_name != walletname:
        raise WalletNotFound()
