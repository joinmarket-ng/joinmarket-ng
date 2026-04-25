"""End-to-end tests for jmwalletd API daemon.

These tests exercise the jmwalletd HTTP API against a real running container
backed by a Bitcoin Core regtest node, validating the full wallet lifecycle
and data endpoints that JAM relies on.

Requires: ``docker compose --profile e2e up -d``
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest

pytestmark = pytest.mark.e2e

JMWALLETD_URL = os.environ.get("JMWALLETD_URL", "https://127.0.0.1:28183")
API = f"{JMWALLETD_URL}/api/v1"

# Self-signed cert on the e2e jmwalletd; clients must skip verification.
TLS_VERIFY = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wallet_name() -> str:
    """Return a unique wallet filename for test isolation."""
    return f"test-{uuid.uuid4().hex[:8]}.jmdat"


async def _wait_for_jmwalletd(timeout: float = 60.0) -> None:
    """Block until jmwalletd is responding on its HTTP port."""
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(verify=TLS_VERIFY) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await client.get(f"{API}/getinfo", timeout=5)
                if r.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(1.0)
    pytest.fail(f"jmwalletd did not become ready within {timeout}s")


def _auth(token: str) -> dict[str, str]:
    """Build Authorization header dict."""
    return {"Authorization": f"Bearer {token}"}


async def _ensure_no_wallet(client: httpx.AsyncClient) -> None:
    """Lock any currently loaded wallet so the daemon is clean.

    Tolerates all error codes -- we just want a best-effort cleanup.
    """
    r = await client.get(f"{API}/session")
    if r.status_code != 200:
        return
    body = r.json()
    if not body.get("session"):
        return  # No wallet loaded.

    wallet_name = body.get("wallet_name", "")
    if not wallet_name:
        return

    # We don't have a valid token, but the reference implementation allows
    # locking without a valid token in some states.  Try with a dummy token.
    await client.get(
        f"{API}/wallet/{wallet_name}/lock",
        headers=_auth("dummy"),
    )

    # If that didn't work, the wallet is stuck open.  That's OK -- the test
    # that created it should have locked it.  We'll get a clear error.


async def _create_wallet(
    client: httpx.AsyncClient,
    name: str | None = None,
    password: str = "testpass",
) -> tuple[str, str, str]:
    """Create a wallet and return (walletname, access_token, refresh_token)."""
    name = name or _wallet_name()
    r = await client.post(
        f"{API}/wallet/create",
        json={
            "walletname": name,
            "password": password,
            "wallettype": "sw-fb",
        },
    )
    assert r.status_code == 201, f"wallet/create failed: {r.status_code} {r.text}"
    body = r.json()
    return name, body["token"], body["refresh_token"]


async def _lock_wallet(
    client: httpx.AsyncClient,
    name: str,
    token: str,
) -> None:
    """Lock a wallet, asserting success."""
    r = await client.get(f"{API}/wallet/{name}/lock", headers=_auth(token))
    assert r.status_code == 200, f"wallet/lock failed: {r.status_code} {r.text}"


async def _unlock_wallet(
    client: httpx.AsyncClient,
    name: str,
    password: str = "testpass",
) -> tuple[str, str]:
    """Unlock a wallet and return (access_token, refresh_token)."""
    r = await client.post(
        f"{API}/wallet/{name}/unlock",
        json={"password": password},
    )
    assert r.status_code == 200, f"wallet/unlock failed: {r.status_code} {r.text}"
    body = r.json()
    return body["token"], body["refresh_token"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def jmwalletd_ready() -> None:
    """Ensure jmwalletd is reachable before running any tests in this module."""
    await _wait_for_jmwalletd()


@pytest.fixture()
async def client(jmwalletd_ready: None) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Shared async HTTP client that also ensures jmwalletd is up."""
    async with httpx.AsyncClient(timeout=30, verify=TLS_VERIFY) as c:
        yield c


@pytest.fixture()
async def clean_client(
    client: httpx.AsyncClient,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Client with guarantee that no wallet is loaded before and after."""
    await _ensure_no_wallet(client)
    yield client
    await _ensure_no_wallet(client)


@pytest.fixture()
async def wallet(
    clean_client: httpx.AsyncClient,
) -> AsyncGenerator[tuple[str, str, str, httpx.AsyncClient], None]:
    """Create a fresh wallet, yield (name, token, refresh, client), lock after.

    Uses ``clean_client`` to ensure no wallet is loaded before creating.
    """
    name, token, refresh = await _create_wallet(clean_client)
    yield name, token, refresh, clean_client
    # Cleanup: lock wallet so the next test starts clean.
    try:
        await _lock_wallet(clean_client, name, token)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests -- Server Health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_getinfo(client: httpx.AsyncClient) -> None:
    """GET /api/v1/getinfo returns 200 with a version string."""
    r = await client.get(f"{API}/getinfo")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body


@pytest.mark.asyncio
async def test_session_no_wallet(clean_client: httpx.AsyncClient) -> None:
    """GET /api/v1/session with no wallet loaded."""
    r = await clean_client.get(f"{API}/session")
    assert r.status_code == 200
    body = r.json()
    # The reference API uses ``session`` (bool) to indicate whether a wallet
    # is loaded, not ``wallet_loaded``.
    assert body["session"] is False


# ---------------------------------------------------------------------------
# Tests -- Wallet Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_wallet(clean_client: httpx.AsyncClient) -> None:
    """POST /api/v1/wallet/create returns 201 with tokens and seedphrase."""
    name = _wallet_name()
    r = await clean_client.post(
        f"{API}/wallet/create",
        json={"walletname": name, "password": "pw123", "wallettype": "sw-fb"},
    )
    assert r.status_code == 201
    body = r.json()
    assert "token" in body
    assert "refresh_token" in body
    assert "seedphrase" in body
    assert len(body["seedphrase"].split()) == 12

    # Lock to clean up.
    await _lock_wallet(clean_client, name, body["token"])


@pytest.mark.asyncio
async def test_unlock_and_lock_wallet(clean_client: httpx.AsyncClient) -> None:
    """Create, lock, then re-unlock a wallet."""
    name, token, _ = await _create_wallet(clean_client)
    await _lock_wallet(clean_client, name, token)

    token, _ = await _unlock_wallet(clean_client, name)

    # Session should show wallet loaded.
    r = await clean_client.get(f"{API}/session", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["session"] is True

    await _lock_wallet(clean_client, name, token)


@pytest.mark.asyncio
async def test_list_wallets(
    wallet: tuple[str, str, str, httpx.AsyncClient],
) -> None:
    """GET /api/v1/wallet/all includes our wallet."""
    name, _, _, client = wallet
    r = await client.get(f"{API}/wallet/all")
    assert r.status_code == 200
    assert name in r.json()["wallets"]


@pytest.mark.asyncio
async def test_token_refresh(
    wallet: tuple[str, str, str, httpx.AsyncClient],
) -> None:
    """POST /api/v1/token with refresh_token returns new token pair."""
    _, token, refresh, client = wallet
    r = await client.post(
        f"{API}/token",
        json={"grant_type": "refresh_token", "refresh_token": refresh},
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert "token" in body
    assert "refresh_token" in body


# ---------------------------------------------------------------------------
# Tests -- Wallet Data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wallet_display(
    wallet: tuple[str, str, str, httpx.AsyncClient],
) -> None:
    """GET /api/v1/wallet/{name}/display returns wallet structure."""
    name, token, _, client = wallet
    r = await client.get(f"{API}/wallet/{name}/display", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert "walletinfo" in body


@pytest.mark.asyncio
async def test_wallet_utxos(
    wallet: tuple[str, str, str, httpx.AsyncClient],
) -> None:
    """GET /api/v1/wallet/{name}/utxos returns (possibly empty) UTXO list."""
    name, token, _, client = wallet
    r = await client.get(f"{API}/wallet/{name}/utxos", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert "utxos" in body
    assert isinstance(body["utxos"], list)


@pytest.mark.asyncio
async def test_new_address(
    wallet: tuple[str, str, str, httpx.AsyncClient],
) -> None:
    """GET /api/v1/wallet/{name}/address/new/0 returns a bcrt1 address."""
    name, token, _, client = wallet
    r = await client.get(
        f"{API}/wallet/{name}/address/new/0",
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert "address" in body
    # Accept bech32 addresses for any network:
    #   bcrt1 (regtest), tb1 (testnet/signet), bc1 (mainnet).
    addr = body["address"]
    assert any(addr.startswith(p) for p in ("bcrt1", "tb1", "bc1")), (
        f"unexpected address prefix: {addr}"
    )


@pytest.mark.asyncio
async def test_get_seed(
    wallet: tuple[str, str, str, httpx.AsyncClient],
) -> None:
    """GET /api/v1/wallet/{name}/getseed returns 12-word mnemonic."""
    name, token, _, client = wallet
    r = await client.get(f"{API}/wallet/{name}/getseed", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert "seedphrase" in body
    assert len(body["seedphrase"].split()) == 12


# ---------------------------------------------------------------------------
# Tests -- Auth Enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_required_without_token(
    wallet: tuple[str, str, str, httpx.AsyncClient],
) -> None:
    """Authenticated endpoints reject requests without a token."""
    name, _, _, client = wallet
    r = await client.get(f"{API}/wallet/{name}/display")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_auth_required_bad_token(
    wallet: tuple[str, str, str, httpx.AsyncClient],
) -> None:
    """Authenticated endpoints reject requests with an invalid token."""
    name, _, _, client = wallet
    r = await client.get(
        f"{API}/wallet/{name}/display",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Tests -- Session with Wallet Loaded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_with_wallet(
    wallet: tuple[str, str, str, httpx.AsyncClient],
) -> None:
    """GET /api/v1/session with auth returns full session info."""
    name, token, _, client = wallet
    r = await client.get(f"{API}/session", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["session"] is True
    assert body["wallet_name"] == name
    assert body["maker_running"] is False
    assert body["coinjoin_in_process"] is False


# ---------------------------------------------------------------------------
# Tests -- Error Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_duplicate_wallet(
    wallet: tuple[str, str, str, httpx.AsyncClient],
) -> None:
    """Cannot create a wallet while another is already loaded."""
    _, _, _, client = wallet
    r = await client.post(
        f"{API}/wallet/create",
        json={"walletname": _wallet_name(), "password": "pw", "wallettype": "sw-fb"},
    )
    # Should reject because a wallet is already loaded.
    assert r.status_code in (401, 409)


@pytest.mark.asyncio
async def test_unlock_nonexistent_wallet(clean_client: httpx.AsyncClient) -> None:
    """Unlocking a wallet that doesn't exist returns an error."""
    r = await clean_client.post(
        f"{API}/wallet/no-such-wallet.jmdat/unlock",
        json={"password": "pw"},
    )
    assert r.status_code in (404, 500)


@pytest.mark.asyncio
async def test_lock_wrong_wallet_name(
    wallet: tuple[str, str, str, httpx.AsyncClient],
) -> None:
    """Locking with a different wallet name than the loaded one must be rejected.

    Previously, the implementation would lock whatever wallet was loaded regardless
    of the name in the URL (matching reference jmwalletd behavior). We now
    enforce a match in JoinMarket-NG for security (IDOR prevention).
    """
    _, token, _, client = wallet
    r = await client.get(
        f"{API}/wallet/wrong-name.jmdat/lock",
        headers=_auth(token),
    )
    # JoinMarket-NG returns 404 when the URL name doesn't match the loaded one.
    assert r.status_code == 404
