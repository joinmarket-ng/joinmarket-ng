"""Tests for jmwalletd.auth — JWT token authority."""

from __future__ import annotations

import base64
import time

import jwt
import pytest

from jmwalletd.auth import JMTokenAuthority


class TestJMTokenAuthority:
    """Tests for JMTokenAuthority."""

    def test_initial_state(self, token_authority: JMTokenAuthority) -> None:
        assert token_authority._wallet_name == ""
        assert len(token_authority._access_key) == 64  # hex of 32 bytes
        assert len(token_authority._refresh_key) == 64

    def test_scope_empty_when_no_wallet(self, token_authority: JMTokenAuthority) -> None:
        # With empty wallet name, scope is just "walletrpc" (base64 of "")
        assert token_authority.scope.startswith("walletrpc")

    def test_scope_format(self, token_authority: JMTokenAuthority) -> None:
        token_authority._wallet_name = "test.jmdat"
        expected = "walletrpc " + base64.b64encode(b"test.jmdat").decode()
        assert token_authority.scope == expected

    def test_issue_returns_token_pair(self, token_authority: JMTokenAuthority) -> None:
        pair = token_authority.issue("my_wallet.jmdat")
        assert pair.token
        assert pair.refresh_token
        assert pair.token_type == "bearer"
        assert pair.expires_in == 1800
        assert "walletrpc" in pair.scope

    def test_issue_sets_wallet_name(self, token_authority: JMTokenAuthority) -> None:
        token_authority.issue("my_wallet.jmdat")
        assert token_authority._wallet_name == "my_wallet.jmdat"

    def test_issue_rotates_refresh_key(self, token_authority: JMTokenAuthority) -> None:
        old_refresh_key = token_authority._refresh_key
        token_authority.issue("w.jmdat")
        assert token_authority._refresh_key != old_refresh_key

    def test_issue_preserves_access_key(self, token_authority: JMTokenAuthority) -> None:
        old_access_key = token_authority._access_key
        token_authority.issue("w.jmdat")
        assert token_authority._access_key == old_access_key

    def test_verify_access_valid(self, token_authority: JMTokenAuthority) -> None:
        pair = token_authority.issue("w.jmdat")
        payload = token_authority.verify_access(pair.token)
        assert "scope" in payload
        assert "exp" in payload

    def test_verify_access_wrong_key_raises(self, token_authority: JMTokenAuthority) -> None:
        pair = token_authority.issue("w.jmdat")
        # Reset keys to invalidate the token
        token_authority.reset()
        with pytest.raises(jwt.InvalidTokenError):
            token_authority.verify_access(pair.token)

    def test_verify_access_expired_raises(self, token_authority: JMTokenAuthority) -> None:
        # Issue a token with a past expiry
        token_authority._wallet_name = "w.jmdat"
        scope = token_authority.scope
        expired_token = jwt.encode(
            {"exp": int(time.time()) - 100, "scope": scope},
            token_authority._access_key,
            algorithm="HS256",
        )
        with pytest.raises(jwt.ExpiredSignatureError):
            token_authority.verify_access(expired_token)

    def test_verify_access_skip_exp_check(self, token_authority: JMTokenAuthority) -> None:
        token_authority._wallet_name = "w.jmdat"
        scope = token_authority.scope
        expired_token = jwt.encode(
            {"exp": int(time.time()) - 100, "scope": scope},
            token_authority._access_key,
            algorithm="HS256",
        )
        # Should succeed with verify_exp=False
        payload = token_authority.verify_access(expired_token, verify_exp=False)
        assert payload["scope"] == scope

    def test_verify_refresh_valid(self, token_authority: JMTokenAuthority) -> None:
        pair = token_authority.issue("w.jmdat")
        payload = token_authority.verify_refresh(pair.refresh_token)
        assert "scope" in payload

    def test_verify_refresh_wrong_key_raises_after_grace(
        self, token_authority: JMTokenAuthority
    ) -> None:
        pair = token_authority.issue("w.jmdat")
        # Issue again rotates the refresh key; once the grace window for the
        # previous key has elapsed, the old refresh token is invalid.
        token_authority.issue("w.jmdat")
        token_authority._previous_refresh_key_expiry = time.time() - 1
        with pytest.raises(jwt.InvalidTokenError):
            token_authority.verify_refresh(pair.refresh_token)

    def test_verify_refresh_accepts_previous_key_within_grace(
        self, token_authority: JMTokenAuthority
    ) -> None:
        """Regression: two clients refreshing near-simultaneously must not
        spuriously log each other out. The token signed with the
        immediately-previous key stays valid for the rotation grace window."""
        pair = token_authority.issue("w.jmdat")
        token_authority.issue("w.jmdat")  # rotates; previous key in grace
        payload = token_authority.verify_refresh(pair.refresh_token)
        assert "scope" in payload

    def test_issue_without_rotation_keeps_outstanding_refresh_tokens(
        self, token_authority: JMTokenAuthority
    ) -> None:
        """Regression: a second unlock of an already-unlocked wallet re-issues
        tokens with ``rotate_refresh=False`` so the first client's refresh
        token stays valid indefinitely (until expiry or wallet lock)."""
        pair1 = token_authority.issue("w.jmdat")
        pair2 = token_authority.issue("w.jmdat", rotate_refresh=False)
        # Simulate the grace window having elapsed; without rotation there is
        # no previous key to depend on, both tokens verify with the live key.
        token_authority._previous_refresh_key_expiry = time.time() - 1
        token_authority.verify_refresh(pair1.refresh_token)
        token_authority.verify_refresh(pair2.refresh_token)

    def test_verify_access_token_as_refresh_fails(self, token_authority: JMTokenAuthority) -> None:
        pair = token_authority.issue("w.jmdat")
        # Access token signed with access key should fail refresh verification
        with pytest.raises(jwt.InvalidTokenError):
            token_authority.verify_refresh(pair.token)

    def test_reset_clears_wallet_and_regenerates_keys(
        self, token_authority: JMTokenAuthority
    ) -> None:
        token_authority.issue("w.jmdat")
        old_access = token_authority._access_key
        old_refresh = token_authority._refresh_key
        token_authority.reset()
        assert token_authority._wallet_name == ""
        assert token_authority._access_key != old_access
        assert token_authority._refresh_key != old_refresh

    def test_reset_invalidates_all_tokens(self, token_authority: JMTokenAuthority) -> None:
        pair = token_authority.issue("w.jmdat")
        token_authority.reset()
        with pytest.raises(jwt.InvalidTokenError):
            token_authority.verify_access(pair.token)
        with pytest.raises(jwt.InvalidTokenError):
            token_authority.verify_refresh(pair.refresh_token)

    def test_reset_clears_rotation_grace(self, token_authority: JMTokenAuthority) -> None:
        """Locking the wallet (reset) must invalidate refresh tokens even if
        a rotation grace window would otherwise still cover them."""
        pair = token_authority.issue("w.jmdat")
        token_authority.issue("w.jmdat")  # previous key now inside grace
        token_authority.reset()
        with pytest.raises(jwt.InvalidTokenError):
            token_authority.verify_refresh(pair.refresh_token)

    def test_multiple_issues_same_wallet(self, token_authority: JMTokenAuthority) -> None:
        pair1 = token_authority.issue("w.jmdat")
        pair2 = token_authority.issue("w.jmdat")
        # Both access tokens should still be valid (same access key)
        token_authority.verify_access(pair1.token)
        token_authority.verify_access(pair2.token)
        # The latest refresh token is valid; the previous one only survives
        # inside the rotation grace window.
        token_authority.verify_refresh(pair2.refresh_token)
        token_authority.verify_refresh(pair1.refresh_token)
        token_authority._previous_refresh_key_expiry = time.time() - 1
        with pytest.raises(jwt.InvalidTokenError):
            token_authority.verify_refresh(pair1.refresh_token)

    def test_verify_access_scope_mismatch_raises(self, token_authority: JMTokenAuthority) -> None:
        """verify_access rejects a token whose scope doesn't match current wallet."""
        # Issue a token for wallet A
        pair = token_authority.issue("wallet_a.jmdat")
        # Change the wallet (scope changes) without resetting keys
        token_authority._wallet_name = "wallet_b.jmdat"
        # The access token from wallet_a should now have wrong scope
        with pytest.raises(jwt.InvalidTokenError, match="Scope mismatch"):
            token_authority.verify_access(pair.token)

    def test_verify_access_returns_early_when_no_scope(
        self, token_authority: JMTokenAuthority
    ) -> None:
        """verify_access returns payload without scope check when scope is empty."""
        # Token authority with no wallet name -> scope is "walletrpc"
        # Issue a token manually with matching scope
        payload = {"exp": time.time() + 1800, "scope": "walletrpc"}
        token = jwt.encode(payload, token_authority._access_key, algorithm="HS256")
        # With no wallet set, scope is "walletrpc" which should match
        result = token_authority.verify_access(token)
        assert "scope" in result

    def test_verify_refresh_scope_mismatch_raises(self, token_authority: JMTokenAuthority) -> None:
        """verify_refresh rejects a token whose scope doesn't match current wallet."""
        # Issue refresh token for wallet A
        token_authority.issue("wallet_a.jmdat")
        refresh_key_a = token_authority._refresh_key

        # Manually change wallet name without rotating refresh key
        # so we can test scope mismatch specifically
        token_authority._wallet_name = "wallet_b.jmdat"
        # Create a token with wallet_a scope but signed with current refresh key
        scope_a = "walletrpc " + base64.b64encode(b"wallet_a.jmdat").decode()
        forged_token = jwt.encode(
            {"exp": time.time() + 14400, "scope": scope_a},
            refresh_key_a,
            algorithm="HS256",
        )
        with pytest.raises(jwt.InvalidTokenError, match="Scope mismatch"):
            token_authority.verify_refresh(forged_token)

    def test_verify_access_rejects_wallet_name_prefix(
        self, token_authority: JMTokenAuthority
    ) -> None:
        """A token issued for a longer wallet name must not validate for a prefix name.

        The base64 encoding of a short wallet name is a prefix of the base64
        encoding of a longer wallet name that starts with the same bytes, so a
        naive substring scope check (``self.scope in token_scope``) accepts a
        token issued for the longer name when the daemon is currently serving
        the shorter, prefix name. Verify the set-membership check we now use
        rejects this.

        Example: b64("ali") == "YWxp" is a prefix of b64("alice") == "YWxpY2U=".
        With the old substring check, expected scope ``"walletrpc YWxp"`` was
        a substring of presented scope ``"walletrpc YWxpY2U="``, so a token
        for ``alice`` was accepted when the daemon was serving ``ali``.
        """
        # Sanity check: the base64 prefix relationship holds.
        assert base64.b64encode(b"alice").decode().startswith(base64.b64encode(b"ali").decode())

        # Issue a token for the longer wallet name.
        pair = token_authority.issue("alice")
        # Switch to the shorter, prefix wallet name without rotating signing
        # keys so the token still verifies cryptographically. Only the scope
        # check protects against cross-wallet reuse here.
        token_authority._wallet_name = "ali"

        with pytest.raises(jwt.InvalidTokenError, match="Scope mismatch"):
            token_authority.verify_access(pair.token)

    def test_verify_refresh_rejects_wallet_name_prefix(
        self, token_authority: JMTokenAuthority
    ) -> None:
        """Same prefix-collision check as the access path, on the refresh path."""
        token_authority.issue("alice")
        refresh_key_alice = token_authority._refresh_key
        scope_alice = "walletrpc " + base64.b64encode(b"alice").decode()
        token_for_alice = jwt.encode(
            {"exp": time.time() + 14400, "scope": scope_alice},
            refresh_key_alice,
            algorithm="HS256",
        )

        token_authority._wallet_name = "ali"

        with pytest.raises(jwt.InvalidTokenError, match="Scope mismatch"):
            token_authority.verify_refresh(token_for_alice)

    def test_verify_access_accepts_superset_scope(self, token_authority: JMTokenAuthority) -> None:
        """A token whose scope is a superset of the expected scope must verify.

        Future-proofing: if a token ever carries extra scope tokens (e.g. a
        feature scope alongside the wallet binding), the set-membership check
        should still accept it as long as the expected scope tokens are all
        present.
        """
        token_authority._wallet_name = "w.jmdat"
        expected = token_authority.scope
        extra_scope = expected + " extra-feature"
        token = jwt.encode(
            {"exp": time.time() + 1800, "scope": extra_scope},
            token_authority._access_key,
            algorithm="HS256",
        )
        payload = token_authority.verify_access(token)
        assert payload["scope"] == extra_scope
