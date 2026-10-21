"""Fidelity bond leases for a maker without a usable bond ([maker] lease_bond)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bitcointx.core.key import CKey
from jmcore.bond_calc import calculate_timelocked_fidelity_bond_value
from jmcore.credential_market import BondCredential, BondReference
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.settings import JoinMarketSettings

from maker import bond_lease_buyer
from maker.bond_lease_buyer import BondLeaseBuyer, needs_lease
from maker.fidelity import FidelityBondInfo

OWNER, CERTIFICATE = CKey(b"\x61" * 32), CKey(b"\x62" * 32)
LOCKTIME = int(time.time()) + 365 * 86_400


def _leased(expiry: int = 3) -> Any:
    credential = BondCredential.model_construct(
        bond=BondReference(
            network="regtest",
            outpoint=ExternalPoDLEOutpoint(txid="aa" * 32, vout=1),
            pubkey=bytes(OWNER.pub).hex(),
            locktime=LOCKTIME,
        ),
        cert_pubkey=bytes(CERTIFICATE.pub).hex(),
        cert_expiry=expiry,
        cert_signature="30" * 70,
    )
    return SimpleNamespace(
        credential=credential, certificate=CERTIFICATE, value=5_000_000, confirmations=10
    )


def _bond(*, owned: bool = False, cert_expiry: int | None = 2) -> FidelityBondInfo:
    return FidelityBondInfo(
        txid="bb" * 32,
        vout=0,
        value=1_000_000,
        locktime=LOCKTIME,
        confirmation_time=1_700_000_000,
        bond_value=1,
        pubkey=bytes(OWNER.pub),
        private_key=OWNER if owned else None,
        cert_pubkey=bytes(CERTIFICATE.pub),
        cert_privkey=CERTIFICATE,
        cert_signature=b"\x30" * 70,
        cert_expiry=cert_expiry,
    )


class _Bot:
    def __init__(self, bond: FidelityBondInfo | None, height: int) -> None:
        self.wallet: Any = None
        self.fidelity_bond = bond
        self.running = True
        self.backend = SimpleNamespace(
            get_block_height=AsyncMock(return_value=height),
            get_block_time=AsyncMock(side_effect=lambda h: 1_700_000_000 + h),
        )
        self.config = SimpleNamespace(lease_bond_max_sats=40_000, lease_bond_max_rate=0.03)
        self.released = 0
        self.adopted: list[tuple[FidelityBondInfo, int]] = []

    def release_expired_bond(self) -> None:
        self.released += 1
        self.fidelity_bond = None

    async def adopt_leased_bond(self, bond: FidelityBondInfo, height: int) -> None:
        assert self.fidelity_bond is None
        self.fidelity_bond = bond
        self.adopted.append((bond, height))


def _buyer(bot: _Bot, result: Any) -> tuple[BondLeaseBuyer, AsyncMock]:
    purchaser = SimpleNamespace(lease=AsyncMock(return_value=result))
    buyer = BondLeaseBuyer(JoinMarketSettings(), bot, purchaser)  # type: ignore[arg-type]
    return buyer, purchaser.lease


def test_only_a_missing_bond_or_an_expired_certificate_needs_a_lease() -> None:
    assert needs_lease(None, 1)
    # A certificate verifies through the last block of the period before its expiry.
    assert not needs_lease(_bond(cert_expiry=2), 4032)
    assert needs_lease(_bond(cert_expiry=2), 4033)
    # A bond advertised with its own key is never replaced.
    assert not needs_lease(_bond(owned=True, cert_expiry=2), 4033)
    assert not needs_lease(_bond(cert_expiry=None), 4033)


async def test_a_lease_is_switched_in_with_its_chain_value() -> None:
    bot = _Bot(None, 4040)
    buyer, lease = _buyer(bot, _leased())

    assert await buyer.check()

    assert lease.await_args is not None
    assert lease.await_args.kwargs == {"cap_sats": 40_000, "max_rate": 0.03}
    ((bond, height),) = bot.adopted
    assert height == 4040
    assert (bond.txid, bond.vout, bond.value, bond.locktime) == ("aa" * 32, 1, 5_000_000, LOCKTIME)
    # Confirmed 10 blocks deep: at height 4031.
    bot.backend.get_block_time.assert_awaited_with(4031)
    assert bond.confirmation_time == 1_700_004_031
    assert bond.bond_value == calculate_timelocked_fidelity_bond_value(
        utxo_value=5_000_000, confirmation_time=1_700_004_031, locktime=LOCKTIME
    )
    assert bond.private_key is None and bond.pubkey == bytes(OWNER.pub)
    assert bond.cert_privkey is CERTIFICATE and bond.cert_expiry == 3
    assert bond.cert_pubkey == bytes(CERTIFICATE.pub)


async def test_an_expired_lease_is_dropped_before_a_new_one_is_bought() -> None:
    bot = _Bot(_bond(cert_expiry=2), 4033)
    buyer, lease = _buyer(bot, None)

    # No seller now: the maker advertises no bond until a later attempt.
    assert not await buyer.check()
    assert bot.released == 1 and bot.fidelity_bond is None
    lease.return_value = _leased()
    assert await buyer.check()
    assert bot.fidelity_bond is not None and bot.fidelity_bond.txid == "aa" * 32
    # Current lease: nothing more is bought.
    assert await buyer.check()
    assert lease.await_count == 2


async def test_an_owned_bond_is_never_replaced() -> None:
    bot = _Bot(_bond(owned=True), 9_999)
    buyer, lease = _buyer(bot, _leased())
    assert await buyer.check()
    lease.assert_not_awaited()
    assert bot.adopted == []


async def test_leasing_needs_a_node_for_mixdepth_0() -> None:
    bot = _Bot(None, 1)
    bot.config.channel_ring = SimpleNamespace(mixdepth_nodes={1: "n1"}, nodes={})
    buyer = BondLeaseBuyer(JoinMarketSettings(), bot)  # type: ignore[arg-type]
    with pytest.raises(bond_lease_buyer.BondLeaseUnavailableError, match="mixdepth 0"):
        await buyer.check()


async def test_managed_buyer_can_track_original_payment_without_an_active_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jmswap.lnd_peer import PaymentResult, PaymentStatus
    from jmswap.managed_nodes import NodeState, allocate_node, new_registry
    from jmswap.managed_payments import ManagedPaymentEndpoint, ManagedPaymentSource
    from taker.podle_purchase import PaymentIdentity

    source = ManagedPaymentSource.for_wallet(tmp_path, "regtest", bytes(OWNER.pub), 0)
    registry = new_registry("regtest", source.wallet_identity)
    original = allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
    original.state = NodeState.RETIRING
    original.identity_pubkey = bytes(CERTIFICATE.pub).hex()
    source.store.update(lambda current: registry)
    bot = _Bot(None, 1)
    bot.wallet = SimpleNamespace(
        data_dir=tmp_path,
        network="regtest",
        wallet_fingerprint="deadbeef",
        master_key=SimpleNamespace(get_public_key_bytes=lambda: bytes(OWNER.pub)),
    )
    settings: Any = SimpleNamespace(
        lightning=SimpleNamespace(mode="managed"),
        get_data_dir=lambda: tmp_path,
        network_config=SimpleNamespace(
            bitcoin_network=None, network=SimpleNamespace(value="regtest")
        ),
    )
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.node_info = AsyncMock(
        return_value=SimpleNamespace(identity_pubkey=original.identity_pubkey, network="regtest")
    )
    client.track_payment = AsyncMock(
        return_value=PaymentResult(PaymentStatus.SUCCEEDED, b"\x42" * 32, None, 0)
    )
    monkeypatch.setattr(ManagedPaymentEndpoint, "client", lambda endpoint: client)
    purchaser = BondLeaseBuyer(settings, bot)._get_purchaser()  # type: ignore[arg-type]
    assert (
        await purchaser._payer.status(PaymentIdentity(original.identity_pubkey, "42" * 32))
        == "succeeded"
    )
    client.track_payment.assert_awaited_once_with(b"\x42" * 32)


async def test_run_waits_for_the_maker_and_keeps_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bond_lease_buyer, "RETRY_SECONDS", 0.0)
    monkeypatch.setattr(bond_lease_buyer, "CHECK_SECONDS", 0.0)
    bot = _Bot(None, 1)
    bot.running = False
    buyer, lease = _buyer(bot, None)
    lease.side_effect = [ValueError("boom"), None, _leased()]
    buyer.start()
    await asyncio.sleep(0.05)
    lease.assert_not_awaited()
    bot.running = True
    for _ in range(200):
        if bot.adopted:
            break
        await asyncio.sleep(0.01)
    await buyer.stop()
    assert len(bot.adopted) == 1
