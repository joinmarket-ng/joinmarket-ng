"""JWT token authority for the wallet daemon.

Implements HS256-based JWT tokens compatible with the reference JoinMarket
implementation.  Two token types are managed:

- **Access token**: short-lived (30 min), used in Authorization / x-jm-authorization headers.
- **Refresh token**: longer-lived (4 hr), used only to obtain new token pairs.

Signing keys are regenerated on daemon start and on each wallet unlock/create/lock cycle,
ensuring tokens from previous sessions are always invalidated.
"""

from __future__ import annotations

import base64
import secrets
import time
from dataclasses import dataclass, field

import jwt

ACCESS_TOKEN_EXPIRY_SECONDS = 1800  # 30 minutes
REFRESH_TOKEN_EXPIRY_SECONDS = 14400  # 4 hours
LEEWAY_SECONDS = 10
# After the refresh key rotates, the previous key stays acceptable for this
# long. Without a grace window, two clients refreshing near-simultaneously
# (or a browser retrying a refresh request) race each other: the first
# refresh rotates the key and the second fails with "Signature verification
# failed" even though its token was perfectly legitimate, spuriously logging
# the user out while the websocket (bound to the access key) keeps working.
REFRESH_ROTATION_GRACE_SECONDS = 120.0


@dataclass
class TokenPair:
    """A pair of access + refresh tokens with metadata."""

    token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = ACCESS_TOKEN_EXPIRY_SECONDS
    scope: str = ""


@dataclass
class JMTokenAuthority:
    """Manages JWT signing keys and token issuance/verification.

    Compatible with the reference implementation's auth semantics:
    - Access and refresh tokens use separate signing keys.
    - Refresh key is rotated on every token refresh.
    - All keys are regenerated on reset (wallet lock/unlock cycle).

    Two deliberate extensions over the reference behaviour make the refresh
    flow robust against legitimate concurrency without weakening the
    lock-invalidates-everything guarantee:

    - After a rotation, the previous refresh key remains acceptable for
      :data:`REFRESH_ROTATION_GRACE_SECONDS` so concurrent/retried refresh
      requests do not spuriously log the client out.
    - Re-issuing tokens for an already-unlocked wallet (a second client or a
      repeated unlock call) can skip the rotation entirely via
      ``issue(..., rotate_refresh=False)`` so it does not invalidate the
      refresh token held by the first client.
    """

    _access_key: str = field(default_factory=lambda: secrets.token_hex(32))
    _refresh_key: str = field(default_factory=lambda: secrets.token_hex(32))
    _previous_refresh_key: str | None = field(default=None)
    _previous_refresh_key_expiry: float = 0.0
    _wallet_name: str = ""

    @property
    def scope(self) -> str:
        """Return the current scope string for token payloads."""
        if not self._wallet_name:
            return "walletrpc"
        b64_name = base64.b64encode(self._wallet_name.encode()).decode()
        return f"walletrpc {b64_name}"

    def reset(self) -> None:
        """Regenerate all signing keys, invalidating all existing tokens."""
        self._access_key = secrets.token_hex(32)
        self._refresh_key = secrets.token_hex(32)
        self._previous_refresh_key = None
        self._previous_refresh_key_expiry = 0.0
        self._wallet_name = ""

    def issue(self, wallet_name: str, *, rotate_refresh: bool = True) -> TokenPair:
        """Issue a new access + refresh token pair for the given wallet.

        By default the refresh signing key is rotated, invalidating any
        previously issued refresh token (the previous key stays valid for
        :data:`REFRESH_ROTATION_GRACE_SECONDS` to tolerate concurrent
        refreshes). Pass ``rotate_refresh=False`` when re-issuing tokens for
        an already-authenticated session (e.g. a repeated unlock of the same
        wallet) so outstanding refresh tokens stay usable.
        """
        self._wallet_name = wallet_name
        if rotate_refresh:
            self._previous_refresh_key = self._refresh_key
            self._previous_refresh_key_expiry = time.time() + REFRESH_ROTATION_GRACE_SECONDS
            self._refresh_key = secrets.token_hex(32)

        now = time.time()
        scope = self.scope

        access_payload = {"exp": now + ACCESS_TOKEN_EXPIRY_SECONDS, "scope": scope}
        refresh_payload = {"exp": now + REFRESH_TOKEN_EXPIRY_SECONDS, "scope": scope}

        access_token = jwt.encode(access_payload, self._access_key, algorithm="HS256")
        refresh_token = jwt.encode(refresh_payload, self._refresh_key, algorithm="HS256")

        return TokenPair(
            token=access_token,
            refresh_token=refresh_token,
            scope=scope,
        )

    def verify_access(self, token: str, *, verify_exp: bool = True) -> dict[str, str]:
        """Verify an access token and return the decoded payload.

        Args:
            token: The raw JWT string.
            verify_exp: Whether to enforce expiration. Set to False for the
                token-refresh flow (the expired access token is still accepted).

        Raises:
            jwt.InvalidTokenError: On any verification failure.
        """
        options = {}
        if not verify_exp:
            options["verify_exp"] = False

        payload: dict[str, str] = jwt.decode(
            token,
            self._access_key,
            algorithms=["HS256"],
            leeway=LEEWAY_SECONDS,
            options=options,  # type: ignore[arg-type]
        )

        # Validate scope includes our expected scope.
        #
        # Scope is an OAuth2-style space-separated set of tokens. We must
        # compare token-by-token (set membership) and NOT use a naive
        # substring check: a substring check would accept a token issued
        # for wallet "ali" (scope "walletrpc YWxp") when the daemon is
        # currently serving wallet "alice" (scope "walletrpc YWxpY2U="),
        # because "walletrpc YWxp" is a substring of "walletrpc YWxpY2U=".
        token_scope = payload.get("scope", "")
        if not self.scope:
            return payload
        expected_tokens = set(self.scope.split())
        presented_tokens = set(token_scope.split())
        if not expected_tokens.issubset(presented_tokens):
            msg = f"Scope mismatch: expected '{self.scope}' in '{token_scope}'"
            raise jwt.InvalidTokenError(msg)

        return payload

    def verify_refresh(self, token: str) -> dict[str, str]:
        """Verify a refresh token and return the decoded payload.

        Tokens signed with the immediately-previous refresh key are accepted
        within :data:`REFRESH_ROTATION_GRACE_SECONDS` of the last rotation,
        so a concurrent second refresh (browser retry, second tab) does not
        spuriously fail. ``reset()`` still invalidates everything at once.

        Raises:
            jwt.InvalidTokenError: On any verification failure.
        """
        try:
            payload: dict[str, str] = jwt.decode(
                token,
                self._refresh_key,
                algorithms=["HS256"],
                leeway=LEEWAY_SECONDS,
            )
        except jwt.InvalidSignatureError:
            if (
                self._previous_refresh_key is None
                or time.time() > self._previous_refresh_key_expiry
            ):
                raise
            payload = jwt.decode(
                token,
                self._previous_refresh_key,
                algorithms=["HS256"],
                leeway=LEEWAY_SECONDS,
            )

        token_scope = payload.get("scope", "")
        if self.scope:
            expected_tokens = set(self.scope.split())
            presented_tokens = set(token_scope.split())
            if not expected_tokens.issubset(presented_tokens):
                msg = f"Scope mismatch: expected '{self.scope}' in '{token_scope}'"
                raise jwt.InvalidTokenError(msg)

        return payload
