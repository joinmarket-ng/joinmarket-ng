"""End-to-end test: fidelity bonds appear in the jmwalletd UTXO API.

Reproduces and guards the regression reported by the JAM maintainer: after
creating (and funding) a fidelity bond with joinmarket-ng as the backend, the
bond UTXO was missing from ``GET /wallet/{name}/utxos`` (and the funds appeared
to "disappear"), because the daemon never imported/scanned the bond's
timelock-branch descriptor.

The fix makes the wallet-data endpoints use a bond-aware sync that loads the
per-wallet bond registry, imports the bond descriptor, and rescans, then
surfaces the bond with a ``locktime`` field (as legacy joinmarket-clientserver
does) so JAM can recognize it.

Requires: ``docker compose --profile e2e up -d``
"""

from __future__ import annotations

import asyncio
import datetime
import os
import uuid

import httpx
import pytest
from loguru import logger

pytestmark = pytest.mark.e2e

JMWALLETD_URL = os.environ.get("JMWALLETD_URL", "https://127.0.0.1:28183")
API = f"{JMWALLETD_URL}/api/v1"

# Self-signed cert on the e2e jmwalletd; clients must skip verification.
TLS_VERIFY = False


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _wallet_name() -> str:
    return f"fbtest-{uuid.uuid4().hex[:8]}.jmdat"


def _next_year_lockdate() -> tuple[str, int]:
    """Return (``YYYY-mm`` lockdate, unix locktime) for Jan 1 next year, UTC.

    A future, 1st-of-month locktime mirrors how JAM creates bonds and keeps the
    bond timelocked (so it would be auto-frozen), which is the scenario the
    maintainer hit.
    """
    year = datetime.datetime.now(datetime.UTC).year + 1
    dt = datetime.datetime(year, 1, 1, tzinfo=datetime.UTC)
    return f"{year}-01", int(dt.timestamp())


async def _wait_for_jmwalletd(timeout: float = 60.0) -> None:
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


async def _create_wallet(client: httpx.AsyncClient) -> tuple[str, str]:
    name = _wallet_name()
    r = await client.post(
        f"{API}/wallet/create",
        json={"walletname": name, "password": "testpass", "wallettype": "sw-fb"},
    )
    assert r.status_code == 201, f"wallet/create failed: {r.status_code} {r.text}"
    return name, r.json()["token"]


async def _lock(client: httpx.AsyncClient, name: str, token: str) -> None:
    try:
        await client.get(f"{API}/wallet/{name}/lock", headers=_auth(token))
    except Exception:
        pass


@pytest.mark.asyncio
async def test_funded_fidelity_bond_appears_in_utxos(
    ensure_blockchain_ready: None,
) -> None:
    """A funded fidelity bond is returned by /utxos with a locktime field."""
    await _wait_for_jmwalletd()
    lockdate, locktime = _next_year_lockdate()

    async with httpx.AsyncClient(timeout=60, verify=TLS_VERIFY) as client:
        name, token = await _create_wallet(client)
        try:
            # 1) Generate a timelocked fidelity bond address. This also records
            #    the bond in the daemon's per-wallet registry.
            r = await client.get(
                f"{API}/wallet/{name}/address/timelock/new/{lockdate}",
                headers=_auth(token),
            )
            assert r.status_code == 200, (
                f"timelock address failed: {r.status_code} {r.text}"
            )
            bond_address = r.json()["address"]
            logger.info(f"Fidelity bond address ({lockdate}): {bond_address}")

            # 2) Fund the bond address on-chain.
            #    Prefer sendtoaddress from test-funder (subsidy-independent),
            #    fall back to coinbase mining when test-funder is unavailable.
            from tests.e2e.rpc_utils import mine_blocks, send_from_test_funder

            dummy = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
            funded = await send_from_test_funder(bond_address, 0.01, confirmations=1)
            if not funded:
                await mine_blocks(1, bond_address)
                await mine_blocks(110, dummy)

            # 3) The bond UTXO must now appear in /utxos, carrying a locktime.
            #    Retry briefly to allow the rescan-on-sync to settle.
            bond_entry = None
            for _ in range(10):
                r = await client.get(f"{API}/wallet/{name}/utxos", headers=_auth(token))
                assert r.status_code == 200, f"utxos failed: {r.status_code} {r.text}"
                utxos = r.json()["utxos"]
                bond_entry = next(
                    (u for u in utxos if u["address"] == bond_address), None
                )
                if bond_entry is not None:
                    break
                await asyncio.sleep(2.0)

            assert bond_entry is not None, (
                "Funded fidelity bond is missing from /utxos (funds disappeared). "
                "The bond-aware sync did not import/scan the timelock branch."
            )

            # The bond must be tagged the way JAM expects.
            assert bond_entry["locktime"], (
                f"Bond UTXO has no locktime field: {bond_entry}"
            )
            assert bond_entry["mixdepth"] == 0, "Fidelity bonds live in mixdepth 0"
            # JAM parses the unix timestamp out of the path's ``:locktime`` suffix.
            assert bond_entry["path"].endswith(f":{locktime}"), (
                f"Bond path missing :locktime suffix: {bond_entry['path']}"
            )
            # The coinbase subsidy (depends on regtest height/halvings) funded it.
            assert bond_entry["value"] > 0

            logger.info(
                f"Bond UTXO surfaced: {bond_entry['utxo']} "
                f"locktime={bond_entry['locktime']} path={bond_entry['path']}"
            )
        finally:
            await _lock(client, name, token)
