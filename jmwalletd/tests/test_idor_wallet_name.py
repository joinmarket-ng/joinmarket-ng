from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

REAL_WALLET = "test_wallet.jmdat"
EVIL_WALLET = "evil_wallet.jmdat"
NONEXISTENT = "does_not_exist.jmdat"

_AUTH_HEADER_KEY = "Authorization"


def _auth(token: str) -> dict[str, str]:
    return {_AUTH_HEADER_KEY: f"Bearer {token}"}


class TestIDORWalletNameDisplay:
    """GET /api/v1/wallet/{walletname}/display"""

    def test_idor_wrong_name_rejected(self, app_with_wallet: TestClient, auth_token: str) -> None:
        """Supplying a wrong walletname must return 404, not wallet data."""
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{EVIL_WALLET}/display",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 404, (
            f"IDOR: expected 404 when walletname='{EVIL_WALLET}' doesn't match "
            f"loaded wallet '{REAL_WALLET}', got {resp.status_code}: {resp.text}"
        )

    def test_correct_walletname_succeeds(
        self, app_with_wallet: TestClient, auth_token: str
    ) -> None:
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{REAL_WALLET}/display",
            headers=_auth(auth_token),
        )
        assert resp.status_code != 404


class TestIDORGetseed:
    """GET /api/v1/wallet/{walletname}/getseed — highest severity."""

    def test_idor_wrong_name_rejected(self, app_with_wallet: TestClient, auth_token: str) -> None:
        """Seed phrase must NOT be returned for a wrong walletname."""
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{EVIL_WALLET}/getseed",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 404, (
            f"IDOR: getseed returned {resp.status_code} for walletname='{EVIL_WALLET}'. "
            "This would expose the seed phrase to any valid token holder!"
        )

    def test_correct_walletname_returns_seed(
        self, app_with_wallet: TestClient, auth_token: str
    ) -> None:
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{REAL_WALLET}/getseed",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 200
        assert "seedphrase" in resp.json()


class TestIDORListUtxos:
    """GET /api/v1/wallet/{walletname}/utxos"""

    def test_idor_wrong_name_rejected(self, app_with_wallet: TestClient, auth_token: str) -> None:
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{EVIL_WALLET}/utxos",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 404, (
            f"IDOR: utxos returned {resp.status_code} for walletname='{EVIL_WALLET}'"
        )

    def test_correct_walletname_succeeds(
        self, app_with_wallet: TestClient, auth_token: str
    ) -> None:
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{REAL_WALLET}/utxos",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 200


class TestIDORGetRescanInfo:
    """GET /api/v1/wallet/{walletname}/getrescaninfo"""

    def test_idor_wrong_name_rejected(self, app_with_wallet: TestClient, auth_token: str) -> None:
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{EVIL_WALLET}/getrescaninfo",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 404, (
            f"IDOR: getrescaninfo returned {resp.status_code} for '{EVIL_WALLET}'"
        )

    def test_correct_walletname_succeeds(
        self, app_with_wallet: TestClient, auth_token: str
    ) -> None:
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{REAL_WALLET}/getrescaninfo",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 200


class TestIDORWalletLock:
    """GET /api/v1/wallet/{walletname}/lock"""

    def test_idor_wrong_name_rejected(self, app_with_wallet: TestClient, auth_token: str) -> None:
        """Locking via a wrong walletname must be rejected."""
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{EVIL_WALLET}/lock",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 404, (
            f"IDOR: lock returned {resp.status_code} for walletname='{EVIL_WALLET}'"
        )

    def test_correct_walletname_succeeds(
        self, app_with_wallet: TestClient, auth_token: str
    ) -> None:
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{REAL_WALLET}/lock",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 200


class TestIDORNewAddress:
    """GET /api/v1/wallet/{walletname}/address/new/{mixdepth}"""

    def test_idor_wrong_name_rejected(self, app_with_wallet: TestClient, auth_token: str) -> None:
        """In the earlier implementation, this returned 400 with 'Wallet X is
        not unlocked'. JoinMarket-NG now explicitly returns 404 via
        require_wallet_match (cleaner, consistent)."""
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{EVIL_WALLET}/address/new/0",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 404, (
            f"IDOR: address/new returned {resp.status_code} for '{EVIL_WALLET}'. "
            "Expected 404 from require_wallet_match."
        )

    def test_correct_walletname_succeeds(
        self, app_with_wallet: TestClient, auth_token: str
    ) -> None:
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{REAL_WALLET}/address/new/0",
            headers=_auth(auth_token),
        )
        assert resp.status_code != 404


class TestIDORFreeze:
    """POST /api/v1/wallet/{walletname}/freeze"""

    def test_idor_wrong_name_rejected(self, app_with_wallet: TestClient, auth_token: str) -> None:
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{EVIL_WALLET}/freeze",
            headers=_auth(auth_token),
            json={"utxo-string": "aa" * 32 + ":0", "freeze": True},
        )
        assert resp.status_code == 404, (
            f"IDOR: freeze returned {resp.status_code} for '{EVIL_WALLET}'"
        )

    def test_correct_walletname_any_response(
        self, app_with_wallet: TestClient, auth_token: str
    ) -> None:
        """Just confirm the request is not blocked by wallet-name check."""
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{REAL_WALLET}/freeze",
            headers=_auth(auth_token),
            json={"utxo-string": "aa" * 32 + ":0", "freeze": True},
        )
        assert resp.status_code != 404


class TestIDORFreezeBatch:
    """POST /api/v1/wallet/{walletname}/freeze-batch"""

    def test_idor_wrong_name_rejected(self, app_with_wallet: TestClient, auth_token: str) -> None:
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{EVIL_WALLET}/freeze-batch",
            headers=_auth(auth_token),
            json={"entries": [{"utxo-string": "aa" * 32 + ":0", "freeze": True}]},
        )
        assert resp.status_code == 404, (
            f"IDOR: freeze-batch returned {resp.status_code} for '{EVIL_WALLET}'"
        )

    def test_correct_walletname_any_response(
        self, app_with_wallet: TestClient, auth_token: str
    ) -> None:
        """Just confirm the request is not blocked by wallet-name check."""
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{REAL_WALLET}/freeze-batch",
            headers=_auth(auth_token),
            json={"entries": [{"utxo-string": "aa" * 32 + ":0", "freeze": True}]},
        )
        assert resp.status_code != 404


class TestIDORDirectSend:
    """POST /api/v1/wallet/{walletname}/taker/direct-send"""

    def test_idor_wrong_name_rejected(self, app_with_wallet: TestClient, auth_token: str) -> None:
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{EVIL_WALLET}/taker/direct-send",
            headers=_auth(auth_token),
            json={"mixdepth": 0, "amount_sats": 10000, "destination": "bcrt1qtest"},
        )
        assert resp.status_code == 404, (
            f"IDOR: direct-send returned {resp.status_code} for '{EVIL_WALLET}'. "
            "An attacker with a valid token could trigger sends via any wallet name!"
        )

    def test_correct_walletname_passes_wallet_check(
        self, app_with_wallet: TestClient, auth_token: str
    ) -> None:
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{REAL_WALLET}/taker/direct-send",
            headers=_auth(auth_token),
            json={"mixdepth": 0, "amount_sats": 10000, "destination": "bcrt1qtest"},
        )
        assert resp.status_code != 404


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/wallet/{EVIL_WALLET}/display",
        f"/api/v1/wallet/{EVIL_WALLET}/getseed",
        f"/api/v1/wallet/{EVIL_WALLET}/utxos",
        f"/api/v1/wallet/{EVIL_WALLET}/getrescaninfo",
        f"/api/v1/wallet/{EVIL_WALLET}/lock",
    ],
)
def test_unauthenticated_always_rejected(app_with_wallet: TestClient, path: str) -> None:
    """Without a token the request must be rejected even if walletname is wrong."""
    resp = app_with_wallet.get(path)
    assert resp.status_code in (401, 404), (
        f"Unauthenticated request to {path!r} returned {resp.status_code}"
    )
