from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import get_txid, parse_transaction, sha256

from jmswap.client import SwapClient
from jmswap.contract import SwapDirection, SwapLeaf, TaprootSwapContract
from jmswap.errors import ProtocolError
from jmswap.interfaces import DirectOnionAdapter
from jmswap.models import FundingOutput, InvoiceDetails
from jmswap.persistence import SwapStore
from jmswap.service import (
    MUSIG_SESSION_MAX_ENTRIES,
    CooperativeSessionRegistry,
    ProviderConfig,
    SwapService,
    _SessionEntry,
)
from jmswap.transaction import build_script_path_spend, validate_cooperative_transaction


@dataclass
class FakeLightning:
    invoices: dict[str, InvoiceDetails] = field(default_factory=dict)
    counter: int = 1
    blocked: set[str] = field(default_factory=set)

    async def create_hold_invoice(self, amount_sat: int, payment_hash: str) -> str:
        invoice = f"ln-hold-{payment_hash}"
        self.invoices[invoice] = InvoiceDetails(payment_hash=payment_hash, amount_sat=amount_sat)
        return invoice

    async def create_invoice(self, amount_sat: int) -> tuple[str, InvoiceDetails]:
        payment_hash = self.counter.to_bytes(32, "big").hex()
        self.counter += 1
        invoice = f"ln-normal-{payment_hash}"
        details = InvoiceDetails(payment_hash=payment_hash, amount_sat=amount_sat)
        self.invoices[invoice] = details
        return invoice, details

    async def decode_invoice(self, invoice: str) -> InvoiceDetails:
        return self.invoices[invoice]

    async def payment_in_flight_or_paid(self, payment_hash: str) -> bool:
        return payment_hash in self.blocked


@dataclass
class FakeChain:
    height: int = 799_930
    outputs: dict[tuple[str, int], FundingOutput] = field(default_factory=dict)
    broadcasts: list[str] = field(default_factory=list)

    async def current_height(self) -> int:
        return self.height

    async def funding_output(self, txid: str, vout: int) -> FundingOutput | None:
        return self.outputs.get((txid, vout))

    async def broadcast(self, raw_transaction: str) -> str:
        self.broadcasts.append(raw_transaction)
        return get_txid(raw_transaction)


def make_service(tmp_path: object) -> tuple[SwapService, FakeLightning, FakeChain]:
    lightning = FakeLightning()
    chain = FakeChain()
    service = SwapService(
        provider_seckey=PrivateKey.from_int(1).secret,
        config=ProviderConfig(network="regtest", percentage_fee=Decimal("0.5"), mining_fee=500),
        lightning=lightning,
        chain=chain,
        store=SwapStore(tmp_path),  # type: ignore[arg-type]
    )
    return service, lightning, chain


async def test_exact_creation_dtos_and_separate_fee_invoice(tmp_path: object) -> None:
    service, lightning, chain = make_service(tmp_path)
    client_store = SwapStore(Path(str(tmp_path)) / "client")
    client = SwapClient(
        DirectOnionAdapter(service.dispatch),
        network="regtest",
        lightning=lightning,
        store=client_store,
    )
    user_key = PrivateKey.from_int(2).secret
    forward = await client.createnormalswap(
        lightning_amount=1_000_000,
        expected_onchain_amount=1_005_500,
        current_height=chain.height,
        user_seckey=user_key,
    )
    hold_invoice = "client-hold"
    lightning.invoices[hold_invoice] = InvoiceDetails(
        payment_hash=forward.id, amount_sat=forward.lightning_amount
    )
    await client.addswapinvoice(forward, hold_invoice)
    assert service.store.load(forward.id).invoice == hold_invoice  # type: ignore[union-attr]

    reverse = await client.createswap(
        lightning_amount=1_001_000,
        expected_onchain_amount=995_495,
        maximum_miner_fee=1_000,
        current_height=chain.height,
        user_seckey=user_key,
        preimage=b"\xaa" * 32,
    )
    stored = service.store.load(reverse.id)
    assert stored is not None
    main = await lightning.decode_invoice(stored.invoice or "")
    miner = await lightning.decode_invoice(stored.miner_fee_invoice or "")
    assert main.payment_hash == reverse.id
    assert miner.payment_hash != reverse.id
    assert (main.amount_sat, miner.amount_sat) == (1_000_000, 1_000)


async def test_reverse_cooperative_claim_roundtrip_and_persistence(tmp_path: object) -> None:
    service, lightning, chain = make_service(tmp_path)
    client_store = SwapStore(Path(str(tmp_path)) / "client")
    client = SwapClient(
        DirectOnionAdapter(service.dispatch),
        network="regtest",
        lightning=lightning,
        store=client_store,
    )
    record = await client.createswap(
        lightning_amount=1_001_000,
        expected_onchain_amount=995_495,
        maximum_miner_fee=1_000,
        current_height=chain.height,
        user_seckey=PrivateKey.from_int(2).secret,
        preimage=b"\xaa" * 32,
    )
    funding = FundingOutput(
        txid="33" * 32,
        vout=0,
        value=record.onchain_amount,
        script_pubkey=record.output_script,
        confirmations=1,
    )
    chain.outputs[(funding.txid, funding.vout)] = funding
    await service.register_funding(record.id, funding.txid, funding.vout)
    destination_script = b"\x00\x14" + b"\x42" * 20
    raw = await client.claimtaprootswap(
        record,
        funding,
        destination_script=destination_script,
        output_value=funding.value - 500,
    )
    persisted = service.store.load(record.id)
    assert persisted is not None
    assert persisted.cooperative_transaction == raw
    assert persisted.spending_txid == get_txid(raw)
    assert persisted.preimage == (b"\xaa" * 32).hex()
    client_persisted = client_store.load(record.id)
    assert client_persisted is not None
    assert client_persisted.cooperative_transaction == raw
    assert client_persisted.local_seckey == PrivateKey.from_int(2).secret.hex()
    assert len(service.sessions) == 0


async def test_client_rejects_wrong_cooperative_funding_before_nonce_exchange(
    tmp_path: object,
) -> None:
    service, lightning, chain = make_service(tmp_path)
    client = SwapClient(
        DirectOnionAdapter(service.dispatch), network="regtest", lightning=lightning
    )
    record = await client.createswap(
        lightning_amount=1_001_000,
        expected_onchain_amount=995_495,
        maximum_miner_fee=1_000,
        current_height=chain.height,
        user_seckey=PrivateKey.from_int(2).secret,
        preimage=b"\xaa" * 32,
    )
    wrong = FundingOutput(
        txid="35" * 32,
        vout=0,
        value=record.onchain_amount,
        script_pubkey="5120" + "99" * 32,
        confirmations=1,
    )
    with pytest.raises(ProtocolError, match="confirmed swap"):
        await client.claimtaprootswap(
            record,
            wrong,
            destination_script=b"\x00\x14" + b"\x42" * 20,
            output_value=wrong.value - 500,
        )


async def test_provider_rejects_funding_outpoint_substitution(tmp_path: object) -> None:
    service, _lightning, chain = make_service(tmp_path)
    record = await service.createnormalswap(
        {
            "invoiceAmount": 1_000_000,
            "refundPublicKey": PrivateKey.from_int(2).public_key.format().hex(),
            "protocol": "taproot-v1",
        }
    )
    swap = service.store.load(record["id"])
    assert swap is not None
    first = FundingOutput(
        txid="36" * 32,
        vout=0,
        value=swap.onchain_amount,
        script_pubkey=swap.output_script,
    )
    second = first.model_copy(update={"txid": "37" * 32})
    chain.outputs[(first.txid, first.vout)] = first
    chain.outputs[(second.txid, second.vout)] = second
    await service.register_funding(swap.id, first.txid, first.vout)
    with pytest.raises(ProtocolError, match="already registered"):
        await service.register_funding(swap.id, second.txid, second.vout)


async def test_cooperative_finish_blocks_a_competing_start(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _lightning, _chain = make_service(tmp_path)
    finish_entered = asyncio.Event()
    release_finish = asyncio.Event()

    async def finish(_payload: object, _direction: object) -> dict[str, bool]:
        finish_entered.set()
        await release_finish.wait()
        return {"finished": True}

    async def start(_payload: object, _direction: object) -> dict[str, bool]:
        return {"started": True}

    monkeypatch.setattr(service, "_finish_cooperative", finish)
    monkeypatch.setattr(service, "_start_cooperative", start)
    finishing = asyncio.create_task(
        service._cooperative({"pubNonce": "value"}, SwapDirection.REVERSE)
    )
    await finish_entered.wait()
    starting = asyncio.create_task(service._cooperative({}, SwapDirection.REVERSE))
    await asyncio.sleep(0)
    assert not starting.done()
    release_finish.set()
    assert await finishing == {"finished": True}
    assert await starting == {"started": True}


async def test_forward_cooperative_refund_after_timeout(tmp_path: object) -> None:
    service, lightning, chain = make_service(tmp_path)
    client = SwapClient(
        DirectOnionAdapter(service.dispatch), network="regtest", lightning=lightning
    )
    record = await client.createnormalswap(
        lightning_amount=1_000_000,
        expected_onchain_amount=1_005_500,
        current_height=chain.height,
        user_seckey=PrivateKey.from_int(2).secret,
    )
    funding = FundingOutput(
        txid="34" * 32,
        vout=0,
        value=record.onchain_amount,
        script_pubkey=record.output_script,
        confirmations=1,
    )
    chain.outputs[(funding.txid, funding.vout)] = funding
    await service.register_funding(record.id, funding.txid, funding.vout)
    chain.height = record.locktime
    raw = await client.refundtaprootswap(
        record,
        funding,
        destination_script=b"\x00\x14" + b"\x43" * 20,
        output_value=funding.value - 500,
        current_height=chain.height,
        locktime=chain.height,
    )
    persisted = service.store.load(record.id)
    assert persisted is not None
    assert persisted.cooperative_transaction == raw


async def test_cooperative_transaction_policy_rejects_witness_and_bad_fee(tmp_path: object) -> None:
    service, _lightning, chain = make_service(tmp_path)
    client = SwapClient(DirectOnionAdapter(service.dispatch), network="regtest")
    record = await client.createnormalswap(
        lightning_amount=1_000_000,
        expected_onchain_amount=1_005_500,
        current_height=chain.height,
        user_seckey=PrivateKey.from_int(2).secret,
    )
    funding = FundingOutput(
        txid="44" * 32,
        vout=0,
        value=record.onchain_amount,
        script_pubkey=record.output_script,
        confirmations=1,
    )
    from jmswap.transaction import build_unsigned_cooperative_transaction

    raw = build_unsigned_cooperative_transaction(
        funding,
        destination_script=b"\x00\x14" + bytes(20),
        value=funding.value - 111,
    )
    checked = validate_cooperative_transaction(raw, funding=funding, network="regtest")
    assert len(checked.message) == 32
    from jmcore.bitcoin import parse_transaction, serialize_transaction

    parsed = parse_transaction(raw)
    witnessed = serialize_transaction(
        parsed.version, parsed.inputs, parsed.outputs, parsed.locktime, [[b"signed"]]
    ).hex()
    with pytest.raises(ValueError, match="unsigned"):
        validate_cooperative_transaction(witnessed, funding=funding, network="regtest")
    parsed.outputs[0].value = funding.value
    zero_fee = serialize_transaction(
        parsed.version, parsed.inputs, parsed.outputs, parsed.locktime
    ).hex()
    with pytest.raises(ValueError, match="fee"):
        validate_cooperative_transaction(zero_fee, funding=funding, network="regtest")


def test_trustless_script_path_claim_and_refund_fallbacks() -> None:
    provider_key = PrivateKey.from_int(1)
    user_key = PrivateKey.from_int(2)
    preimage = b"\xaa" * 32
    destination = b"\x00\x14" + b"\x42" * 20
    for direction, leaf, key in (
        (SwapDirection.REVERSE, SwapLeaf.CLAIM, user_key),
        (SwapDirection.FORWARD, SwapLeaf.REFUND, user_key),
    ):
        contract = TaprootSwapContract(
            direction,
            sha256(preimage),
            800_000,
            provider_key.public_key.format(compressed=True),
            user_key.public_key.format(compressed=True),
        )
        funding = FundingOutput(
            txid="66" * 32,
            vout=0,
            value=100_000,
            script_pubkey=contract.output_script.hex(),
            confirmations=1,
        )
        raw = build_script_path_spend(
            contract,
            funding,
            destination_script=destination,
            output_value=99_000,
            local_seckey=key.secret,
            leaf=leaf,
            preimage=preimage if leaf is SwapLeaf.CLAIM else None,
        )
        transaction = parse_transaction(raw)
        script, control = contract.script_path(leaf)
        expected = (
            [transaction.witnesses[0][0], preimage, script, control]
            if leaf is SwapLeaf.CLAIM
            else [transaction.witnesses[0][0], script, control]
        )
        assert transaction.witnesses == [expected]
        assert transaction.locktime == (800_000 if leaf is SwapLeaf.REFUND else 0)
        with pytest.raises(ValueError, match="does not own"):
            build_script_path_spend(
                contract,
                funding,
                destination_script=destination,
                output_value=99_000,
                local_seckey=provider_key.secret if key is user_key else user_key.secret,
                leaf=leaf,
                preimage=preimage,
            )


def test_cooperative_registry_is_bounded_one_per_swap_and_single_use() -> None:
    import time

    registry = CooperativeSessionRegistry()

    def entry(index: int, *, expires: float | None = None) -> _SessionEntry:
        return _SessionEntry(
            expires or time.monotonic() + 90,
            SwapDirection.REVERSE,
            index.to_bytes(32, "big").hex(),
            "00",
            bytes(32),
            object(),  # type: ignore[arg-type]
            bytes(32),
        )

    registry.add("01" * 32, entry(1))
    with pytest.raises(ProtocolError, match="active"):
        registry.add("02" * 32, entry(1))
    assert registry.consume("01" * 32).swap_id == entry(1).swap_id
    with pytest.raises(ProtocolError, match="already used"):
        registry.consume("01" * 32)

    for index in range(MUSIG_SESSION_MAX_ENTRIES):
        registry.add(index.to_bytes(32, "big").hex(), entry(index + 10))
    with pytest.raises(ProtocolError, match="capacity"):
        registry.add("ff" * 32, entry(100))

    expired = CooperativeSessionRegistry()
    expired.add("aa" * 32, entry(200, expires=time.monotonic() + 1))
    from unittest.mock import patch

    with (
        patch("jmswap.service.time.monotonic", return_value=time.monotonic() + 2),
        pytest.raises(ProtocolError, match="expired"),
    ):
        expired.consume("aa" * 32)
