from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, create_autospec

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    get_txid,
    parse_transaction_bytes,
    serialize_transaction,
)
from jmcore.musig2 import nonce_agg, sign_partial

from jmswap.bitcoin_escrow import (
    escrow_nonce,
    escrow_session,
    finalize_key_path_spend,
    verify_signed_claim,
    verify_signed_cooperative_sweep,
    verify_signed_split,
)
from jmswap.buyout_chain import BuyoutChain, ChainTransaction
from jmswap.buyout_messages import (
    BuyoutMessage,
    BuyoutParent,
    BuyoutPropose,
    BuyoutStatus,
    Outpoint,
    Prevout,
    accept_hash,
)
from jmswap.buyout_settlement import BuyoutSettlement, SettlementPolicy
from jmswap.buyout_signing import BuyoutBuyer, BuyoutPolicy, CounterpartySigner
from jmswap.buyout_store import BuyoutStore, ConflictError
from jmswap.buyout_terms import SPLIT_SAFETY_MARGIN_BLOCKS, ProtocolError, validate_parent
from jmswap.lnd_escrow import FrozenChannel, LndEscrowClient
from jmswap.lnd_peer import (
    InvoiceInfo,
    InvoiceState,
    InvoiceStatus,
    LndPeerClient,
    PaymentResult,
    PaymentStatus,
)

BUYER_KEY = bytes(CKey(b"\x11" * 32).pub)
COUNTERPARTY_KEY = bytes(CKey(b"\x22" * 32).pub)
POINT = Outpoint(txid="33" * 32, vout=0)
SCRIPT = b"\x51\x20" + bytes(CKey(b"\x44" * 32).xonly_pub)


async def height() -> int:
    return 200


def test_policy_default_cltv_limit_fits_the_csv_delay() -> None:
    policy = BuyoutPolicy()

    assert policy.cltv_limit == 360
    assert policy.csv_delay >= (
        policy.buyer_settlement_depth + policy.cltv_limit + SPLIT_SAFETY_MARGIN_BLOCKS
    )


@dataclass
class Runtime:
    buyer: BuyoutBuyer
    counterparty: CounterpartySigner
    buyer_backend: Any
    counterparty_backend: Any

    async def request(self, peer: str, message: BuyoutMessage) -> BuyoutMessage:
        assert peer == COUNTERPARTY_KEY.hex()
        return await self.counterparty.handle(BUYER_KEY.hex(), message)

    async def prepare(self) -> str:
        return await self.buyer.prepare(COUNTERPARTY_KEY.hex(), [POINT], SCRIPT.hex())

    def restart(self) -> None:
        b, c = self.buyer, self.counterparty
        self.counterparty = CounterpartySigner(
            c.store, c.escrow, c.peer, height, c.policy, c.payout_script
        )
        self.buyer = BuyoutBuyer(b.store, b.escrow, b.peer, self.request, height, b.policy)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    backends, peers = [], []
    for local, remote, amount in [
        (BUYER_KEY, COUNTERPARTY_KEY, 700_000),
        (COUNTERPARTY_KEY, BUYER_KEY, 300_000),
    ]:
        backend = create_autospec(LndEscrowClient, instance=True)
        backend.freeze.return_value = FrozenChannel(
            point=POINT,
            capacity_sat=1_000_000,
            local_claim_sat=amount,
            remote_claim_sat=1_000_000 - amount,
            peer_pubkey=remote,
            funding_script=SCRIPT,
            local_funding_pubkey=local,
            remote_funding_pubkey=remote,
        )
        peer = create_autospec(LndPeerClient, instance=True)
        peer.node_info.return_value = SimpleNamespace(synced_to_chain=True, network="regtest")
        peer.channels.return_value = [
            SimpleNamespace(
                point=POINT,
                peer_pubkey=remote.hex(),
                active=True,
                private=True,
                pending_htlcs=0,
                commitment_type="TAPROOT",
            )
        ]
        backends.append(backend)
        peers.append(peer)
    policy = BuyoutPolicy()
    with (
        BuyoutStore(tmp_path / "buyer" / "sessions.sqlite") as buyer_store,
        BuyoutStore(tmp_path / "counterparty" / "sessions.sqlite") as cp_store,
    ):
        counterparty = CounterpartySigner(
            cp_store, backends[1], peers[1], height, policy, SCRIPT.hex()
        )
        buyer = BuyoutBuyer(buyer_store, backends[0], peers[0], AsyncMock(), height, policy)
        result = Runtime(buyer, counterparty, *backends)
        buyer.request = result.request
        yield result


async def test_runtime_binding_is_durable_before_freezing(runtime: Runtime) -> None:
    for signer, backend, identity in (
        (runtime.buyer, runtime.buyer_backend, BUYER_KEY),
        (runtime.counterparty, runtime.counterparty_backend, COUNTERPARTY_KEY),
    ):
        signer.runtime_binding = {
            "network": "regtest",
            "lnd_identity": identity.hex(),
            "mixdepth": 2,
        }

        async def freeze(
            point: Outpoint,
            session: bytes,
            *,
            owner: Any = signer,
            result: Any = backend.freeze.return_value,
        ) -> FrozenChannel:
            assert owner.store.get(session.hex()).data["runtime_binding"] == owner.runtime_binding
            return result

        backend.freeze.side_effect = freeze
    sid = await runtime.prepare()
    runtime.buyer.runtime_binding["mixdepth"] = 3
    assert runtime.buyer.store.get(sid).data["runtime_binding"]["mixdepth"] == 2


async def test_explicit_three_block_payment_policy_survives_config_change(
    runtime: Runtime,
) -> None:
    runtime.buyer.policy = replace(
        runtime.buyer.policy, buyer_settlement_depth=3, settlement_depth=1
    )
    runtime.counterparty.policy = replace(
        runtime.counterparty.policy, buyer_settlement_depth=3, settlement_depth=1
    )
    sid = await runtime.prepare()
    assert runtime.buyer.terms(sid).proposal.buyer_settlement_depth == 3
    assert runtime.buyer.terms(sid).acceptance.settlement_depth == 1

    # An existing installation's negotiated session must not acquire new
    # confirmation thresholds from the current config on restart.
    runtime.buyer.policy = BuyoutPolicy()
    runtime.counterparty.policy = BuyoutPolicy()
    runtime.restart()
    assert runtime.buyer.terms(sid).proposal.buyer_settlement_depth == 3
    assert runtime.buyer.terms(sid).acceptance.settlement_depth == 1
    assert runtime.counterparty.store.get(sid).data["settlement_authorized"] is False


@pytest.mark.parametrize(
    "configured_mixdepth,stored_binding",
    [
        (0, None),
        (0, {}),
        (0, {"network": "regtest"}),
        (0, {"network": "mainnet", "mixdepth": 2}),
        (0, {"network": "regtest", "lnd_identity": BUYER_KEY.hex(), "mixdepth": False}),
        (1, {"network": "regtest", "lnd_identity": BUYER_KEY.hex(), "mixdepth": True}),
    ],
)
async def test_bound_signers_reject_unowned_sessions_at_public_entry_points(
    runtime: Runtime,
    configured_mixdepth: int,
    stored_binding: Any,
) -> None:
    sid = await runtime.prepare()
    for signer in (runtime.buyer, runtime.counterparty):
        record = signer.store.get(sid)
        signer.store.update(
            record, state=record.state, data={**record.data, "runtime_binding": stored_binding}
        )
        signer.runtime_binding = {
            "network": "regtest",
            "lnd_identity": BUYER_KEY.hex(),
            "mixdepth": configured_mixdepth,
        }
    before = runtime.buyer.store.get(sid)
    runtime.buyer_backend.reset_mock()
    runtime.counterparty_backend.reset_mock()
    for operation in (runtime.buyer.terms, runtime.buyer.reserve_coinjoin):
        with pytest.raises(ProtocolError, match="not bound"):
            operation(sid)
    for operation in (
        runtime.buyer.resume_prepare(sid),
        runtime.buyer.cancel(sid),
        runtime.buyer.sign_parent(sid, b"", [], [], 0),
    ):
        with pytest.raises(ProtocolError, match="not bound"):
            await operation
    with pytest.raises(ProtocolError, match="not bound"):
        await runtime.counterparty.handle(
            BUYER_KEY.hex(), BuyoutPropose.model_validate(before.data["proposal"])
        )
    assert runtime.buyer.store.get(sid) == before
    assert not runtime.buyer_backend.mock_calls
    assert not runtime.counterparty_backend.mock_calls


@pytest.mark.parametrize("overrides", [{"csv_delay": 433}, {"freeze_ttl_blocks": 7}])
async def test_counterparty_rejects_excessive_lock_duration_before_freezing(
    runtime: Runtime,
    overrides: dict[str, int],
) -> None:
    runtime.buyer.policy = replace(runtime.buyer.policy, **overrides)
    with pytest.raises(ExceptionGroup):
        await runtime.prepare()
    runtime.counterparty_backend.freeze.assert_not_awaited()
    assert runtime.counterparty.store.list() == []


async def test_interrupted_freeze_resumes_same_proposal_and_keys(runtime: Runtime) -> None:
    backend = runtime.counterparty_backend
    backend.freeze.side_effect = [RuntimeError("interrupted"), backend.freeze.return_value]
    with pytest.raises(ExceptionGroup):
        await runtime.prepare()
    before = runtime.buyer.store.list()[0]
    assert before.state == "FREEZING"
    assert runtime.counterparty.store.get(before.session_id).state == "FREEZING"
    runtime.restart()
    assert await runtime.buyer.resume_prepare(before.session_id) == before.session_id
    after = runtime.buyer.store.get(before.session_id)
    assert after.state == "ACCEPTED"
    assert after.data["proposal"] == before.data["proposal"]
    assert after.data["escrow_secret"] == before.data["escrow_secret"]
    assert after.data["claim_secret"] == before.data["claim_secret"]


async def test_lost_acceptance_can_be_canceled_by_proposal_hash(runtime: Runtime) -> None:
    async def lose_reply(peer: str, message: BuyoutMessage) -> BuyoutMessage:
        await runtime.request(peer, message)
        raise TimeoutError("lost reply")

    runtime.buyer.request = lose_reply
    with pytest.raises(ExceptionGroup):
        await runtime.prepare()
    record = runtime.buyer.store.list()[0]
    assert "acceptance" not in record.data
    runtime.restart()
    await runtime.buyer.cancel(record.session_id)
    assert runtime.buyer.store.get(record.session_id).state == "CANCELED"
    assert runtime.counterparty.store.get(record.session_id).state == "CANCELED"
    runtime.buyer_backend.cancel.assert_awaited_once()
    runtime.counterparty_backend.cancel.assert_awaited_once()
    await runtime.buyer.cancel(record.session_id)
    runtime.buyer_backend.cancel.assert_awaited_once()
    with pytest.raises(ProtocolError, match="canceled"):
        await runtime.counterparty.handle(
            BUYER_KEY.hex(), BuyoutPropose.model_validate(record.data["proposal"])
        )


async def test_lost_cancel_ack_retries_without_repeating_counterparty_side_effect(
    runtime: Runtime,
) -> None:
    sid = await runtime.prepare()

    async def lose_reply(peer: str, message: BuyoutMessage) -> BuyoutMessage:
        await runtime.request(peer, message)
        raise TimeoutError("lost reply")

    runtime.buyer.request = lose_reply
    with pytest.raises(TimeoutError):
        await runtime.buyer.cancel(sid)
    assert runtime.buyer.store.get(sid).state == "CANCELING"
    runtime.buyer_backend.cancel.assert_not_awaited()
    runtime.restart()
    await runtime.buyer.cancel(sid)
    runtime.counterparty_backend.cancel.assert_awaited_once()
    runtime.buyer_backend.cancel.assert_awaited_once()
    assert runtime.buyer.store.get(sid).state == "CANCELED"


async def test_nonce_loss_before_authorization_can_cancel(runtime: Runtime) -> None:
    sid = await runtime.prepare()
    for store in (runtime.buyer.store, runtime.counterparty.store):
        record = store.get(sid)
        store.update(record, state="NONCES", data=record.data)
    runtime.restart()
    await runtime.buyer.cancel(sid)
    assert runtime.buyer.store.get(sid).state == "CANCELED"
    assert runtime.counterparty.store.get(sid).state == "CANCELED"


async def test_authorization_marker_forbids_cancel_after_restart(runtime: Runtime) -> None:
    sid = await runtime.prepare()
    record = runtime.buyer.store.get(sid)
    runtime.buyer.store.update(
        record, state="PARENT_SIGNING", data=record.data, parent_signing_started=True
    )
    runtime.restart()
    with pytest.raises(ProtocolError, match="cannot be canceled"):
        await runtime.buyer.cancel(sid)
    runtime.buyer_backend.cancel.assert_not_awaited()
    runtime.counterparty_backend.cancel.assert_not_awaited()


async def test_another_runtime_operation_cannot_start_rpc(runtime: Runtime) -> None:
    with runtime.buyer.store.exclusive_operation(), pytest.raises(ConflictError):
        await runtime.prepare()
    runtime.buyer_backend.freeze.assert_not_awaited()


@dataclass
class SettlementRuntime:
    signing: Runtime
    buyer: BuyoutSettlement
    counterparty: BuyoutSettlement
    chain: Any
    sid: str
    parent: Any
    transactions: dict[str, ChainTransaction]
    invoice: InvoiceInfo
    preimage: bytes

    async def request(self, peer: str, message: BuyoutMessage) -> BuyoutMessage:
        return await self.counterparty.handle(BUYER_KEY.hex(), message)


@pytest.fixture
async def settlement(runtime: Runtime, request: pytest.FixtureRequest) -> SettlementRuntime:
    if getattr(request, "param", None) == "three_block_payment":
        runtime.buyer.policy = replace(
            runtime.buyer.policy, buyer_settlement_depth=3, settlement_depth=1
        )
        runtime.counterparty.policy = replace(
            runtime.counterparty.policy, buyer_settlement_depth=3, settlement_depth=1
        )
    sid = await runtime.prepare()
    terms = runtime.buyer.terms(sid)
    raw = serialize_transaction(
        2,
        [TxInput.from_hex(POINT.txid, POINT.vout), TxInput.from_hex("77" * 32, 0)],
        [
            TxOutput(value=500_000, script=SCRIPT),
            TxOutput(value=699_000, script=terms.escrow.output_script()),
        ],
        0,
    )
    prevouts = [
        Prevout(value=1_000_000, script_pubkey=SCRIPT.hex()),
        Prevout(value=200_000, script_pubkey=SCRIPT.hex()),
    ]
    parent = validate_parent(terms, raw, prevouts, [0], 1, 200)
    keys = [
        bytes.fromhex(store.get(sid).data["escrow_secret"])
        for store in (runtime.buyer.store, runtime.counterparty.store)
    ]
    nonces = [
        escrow_nonce(bytes(CKey(key).pub), privkey=key, sighash=parent.split.sighash)
        for key in keys
    ]
    context = escrow_session(
        terms.escrow, nonce_agg([item[1] for item in nonces]), parent.split.sighash
    )
    partials = [
        sign_partial(nonce[0], key, context) for nonce, key in zip(nonces, keys, strict=True)
    ]
    split = finalize_key_path_spend(
        terms.escrow, parent.split, nonces[0][1], nonces[1][1], *partials
    )
    message = BuyoutParent(
        v=1,
        type="buyout_parent",
        epoch_id=sid,
        attempt=0,
        accept_hash=accept_hash(terms.acceptance),
        unsigned_parent_tx=raw.hex(),
        prevouts=prevouts,
        escrow_output_index=1,
        channel_input_indices=[0],
        split_nonce_B=nonces[0][1].hex(),
        parent_nonces_B=[BUYER_KEY.hex() * 2],
    )
    for store in (runtime.buyer.store, runtime.counterparty.store):
        record = store.get(sid)
        store.update(
            record,
            state="PARENT_SIGNED",
            parent_signing_started=True,
            data={
                **record.data,
                "parent": message.model_dump(),
                "raw_parent": raw.hex(),
                "split_tx": split.hex(),
                "settlement_authorized": True,
            },
        )
    transactions = {parent.txid: ChainTransaction(parent.txid, raw, 6, 200, "88" * 32)}
    chain = create_autospec(BuyoutChain, instance=True)
    chain.height.return_value = 205
    chain.tip.return_value = (205, "99" * 32)
    chain.block_hash.return_value = "99" * 32
    chain.transaction.side_effect = transactions.get
    chain.unspent.return_value = True
    chain.fee_rates.return_value = (2, 1)

    async def broadcast(raw: bytes) -> str:
        txid = get_txid(raw.hex())
        transactions[txid] = ChainTransaction(txid, raw, 0, None, None)
        return txid

    chain.broadcast.side_effect = broadcast
    preimage = bytes.fromhex(runtime.counterparty.store.get(sid).data["preimage"])
    invoice = InvoiceInfo(
        bytes.fromhex(terms.acceptance.payment_hash),
        COUNTERPARTY_KEY,
        terms.claim_sat,
        int(time.time()),
        3600,
        40,
    )
    runtime.buyer.peer.inspect_invoice.return_value = invoice
    runtime.counterparty.peer.create_invoice.return_value = "lnbcrt10u1pjtestinvoice"
    runtime.counterparty.peer.invoice_status.return_value = InvoiceStatus(
        InvoiceState.OPEN, 0, None
    )

    async def pay(*args: Any, **kwargs: Any) -> PaymentResult:
        runtime.counterparty.peer.invoice_status.return_value = InvoiceStatus(
            InvoiceState.SETTLED, terms.claim_sat, preimage
        )
        return PaymentResult(PaymentStatus.SUCCEEDED, invoice.payment_hash, preimage, 0)

    runtime.buyer.peer.pay.side_effect = pay
    buyer = BuyoutSettlement(runtime.buyer.store, runtime.buyer.peer, chain)
    cp = BuyoutSettlement(runtime.counterparty.store, runtime.counterparty.peer, chain)
    result = SettlementRuntime(
        runtime, buyer, cp, chain, sid, parent, transactions, invoice, preimage
    )
    buyer.request = result.request
    return result


async def test_payment_then_cooperative_sweep(settlement: SettlementRuntime) -> None:
    h = settlement
    assert await h.buyer.poll(h.sid) == "SETTLED"
    assert h.signing.buyer.store.get(h.sid).data["payment_started"] is True
    assert await h.buyer.poll(h.sid) == "SPEND_BROADCAST"
    raw = h.chain.broadcast.call_args.args[0]
    terms = h.signing.buyer.terms(h.sid)
    assert verify_signed_cooperative_sweep(terms.escrow, h.parent.escrow_outpoint, SCRIPT, 222, raw)
    h.signing.buyer.peer.pay.assert_awaited_once()
    assert h.signing.counterparty.peer.create_invoice.call_args.kwargs["include_private_routes"]


async def test_bound_settlement_cannot_bypass_runtime_ownership(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    binding = {"network": "regtest", "lnd_identity": BUYER_KEY.hex(), "mixdepth": 0}
    h.buyer.runtime_binding = binding
    h.counterparty.runtime_binding = binding
    before = h.signing.buyer.store.get(h.sid)
    h.chain.reset_mock()
    with pytest.raises(ProtocolError, match="not bound"):
        await h.buyer.poll(h.sid)
    terms = h.signing.buyer.terms(h.sid)
    message = BuyoutStatus(
        v=1,
        type="buyout_status",
        epoch_id=h.sid,
        attempt=0,
        accept_hash=accept_hash(terms.acceptance),
        stage="parent_signed",
        parent_hash=h.parent.parent_hash,
        txid=h.parent.txid,
    )
    with pytest.raises(ProtocolError, match="not bound"):
        await h.counterparty.handle(BUYER_KEY.hex(), message)
    assert h.signing.buyer.store.get(h.sid) == before
    assert not h.chain.mock_calls
    h.signing.buyer.peer.pay.assert_not_awaited()
    h.signing.counterparty.peer.create_invoice.assert_not_awaited()


async def test_paid_parent_eviction_rebroadcasts_exact_artifact_after_restart(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    parsed = parse_transaction_bytes(h.parent.raw)
    signed = serialize_transaction(
        parsed.version,
        parsed.inputs,
        parsed.outputs,
        parsed.locktime,
        witnesses=[[b"\x01" * 64], [b"\x02" * 64]],
    )
    h.transactions[h.parent.txid] = replace(h.transactions[h.parent.txid], raw=signed)
    assert await h.buyer.poll(h.sid) == "SETTLED"
    assert h.signing.buyer.store.get(h.sid).data["finalized_parent"] == signed.hex()
    del h.transactions[h.parent.txid]
    h.buyer = BuyoutSettlement(
        h.signing.buyer.store, h.signing.buyer.peer, h.chain, request=h.request
    )
    assert await h.buyer.poll(h.sid) == "PARENT_REBROADCAST"
    h.chain.broadcast.assert_awaited_once_with(signed)
    assert await h.buyer.poll(h.sid) == "SPEND_BROADCAST"
    h.signing.buyer.peer.pay.assert_awaited_once()


async def test_paid_legacy_record_without_parent_requires_recovery(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    await h.buyer.poll(h.sid)
    record = h.signing.buyer.store.get(h.sid)
    data = dict(record.data)
    del data["finalized_parent"]
    h.signing.buyer.store.update(record, state=record.state, data=data)
    del h.transactions[h.parent.txid]
    assert await h.buyer.poll(h.sid) == "PARENT_RECOVERY_REQUIRED"
    h.chain.broadcast.assert_not_awaited()
    h.signing.buyer.peer.pay.assert_awaited_once()


@pytest.mark.parametrize(
    "field,value",
    [
        ("payment_hash", b"\x00" * 32),
        ("destination", BUYER_KEY),
        ("amount_sat", 1),
        ("expiry_seconds", 1),
        ("min_final_cltv", 1_000),
    ],
)
async def test_invalid_invoice_never_starts_payment(
    settlement: SettlementRuntime, field: str, value: Any
) -> None:
    h = settlement
    h.signing.buyer.peer.inspect_invoice.return_value = replace(h.invoice, **{field: value})
    with pytest.raises(ProtocolError, match="settlement bounds"):
        await h.buyer.poll(h.sid)
    h.signing.buyer.peer.pay.assert_not_awaited()
    assert "payment_started" not in h.signing.buyer.store.get(h.sid).data


async def test_lost_payment_reply_only_tracks_existing_payment(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    h.signing.buyer.peer.pay.side_effect = TimeoutError("lost payment reply")
    with pytest.raises(TimeoutError):
        await h.buyer.poll(h.sid)
    h.signing.buyer.peer.track_payment.return_value = None
    h.buyer = BuyoutSettlement(
        h.signing.buyer.store, h.signing.buyer.peer, h.chain, request=h.request
    )
    assert await h.buyer.poll(h.sid) == "PAYMENT_STARTED"
    h.signing.buyer.peer.pay.assert_awaited_once()
    h.signing.buyer.peer.track_payment.return_value = PaymentResult(
        PaymentStatus.SUCCEEDED, h.invoice.payment_hash, h.preimage, 3
    )
    h.buyer.request = AsyncMock(side_effect=TimeoutError("counterparty offline"))
    assert await h.buyer.poll(h.sid) == "SPEND_BROADCAST"
    terms = h.signing.buyer.terms(h.sid)
    assert verify_signed_claim(
        terms.escrow, h.parent.escrow_outpoint, SCRIPT, 292, h.chain.broadcast.call_args.args[0]
    )
    h.signing.buyer.peer.pay.assert_awaited_once()


async def test_reorg_between_invoice_and_payment_does_not_pay(
    settlement: SettlementRuntime,
) -> None:
    h = settlement

    async def inspect(invoice: str) -> InvoiceInfo:
        h.transactions.pop(h.parent.txid)
        return h.invoice

    h.signing.buyer.peer.inspect_invoice.side_effect = inspect
    await h.buyer.poll(h.sid)
    h.signing.buyer.peer.pay.assert_not_awaited()
    assert await h.buyer.poll(h.sid) == "PARENT_MISSING"


async def test_observed_parent_replaces_stale_missing_status_without_paying_early(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    parent = h.transactions.pop(h.parent.txid)
    assert await h.buyer.poll(h.sid) == "PARENT_MISSING"
    missing = h.signing.buyer.store.get(h.sid)

    h.transactions[h.parent.txid] = replace(parent, confirmations=0, height=None, block_hash=None)
    assert await h.buyer.poll(h.sid) == "PARENT_OBSERVED"
    observed = h.signing.buyer.store.get(h.sid)
    assert observed.data == missing.data
    assert observed.parent_signing_started
    h.signing.buyer.peer.pay.assert_not_awaited()
    h.signing.counterparty.peer.create_invoice.assert_not_awaited()
    h.chain.broadcast.assert_not_awaited()

    h.transactions[h.parent.txid] = replace(parent, confirmations=2)
    assert await h.buyer.poll(h.sid) == "PARENT_OBSERVED"
    assert h.signing.buyer.store.get(h.sid).revision == observed.revision
    h.signing.buyer.peer.pay.assert_not_awaited()

    h.transactions[h.parent.txid] = parent
    assert await h.buyer.poll(h.sid) == "SETTLED"
    h.signing.buyer.peer.pay.assert_awaited_once()


async def test_observed_parent_can_disappear_and_be_observed_again(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    parent = h.transactions[h.parent.txid]
    h.transactions[h.parent.txid] = replace(parent, confirmations=1)
    assert await h.buyer.poll(h.sid) == "PARENT_OBSERVED"
    h.transactions.pop(h.parent.txid)
    assert await h.buyer.poll(h.sid) == "PARENT_MISSING"
    h.transactions[h.parent.txid] = replace(parent, confirmations=2)
    assert await h.buyer.poll(h.sid) == "PARENT_OBSERVED"
    h.signing.buyer.peer.pay.assert_not_awaited()
    h.chain.broadcast.assert_not_awaited()


async def test_parent_observation_does_not_downgrade_existing_invoice(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    parent = h.transactions[h.parent.txid]
    h.transactions[h.parent.txid] = replace(parent, confirmations=3)
    assert await h.counterparty.poll(h.sid) == "INVOICE_READY"
    h.transactions[h.parent.txid] = replace(parent, confirmations=2)
    assert await h.counterparty.poll(h.sid) == "INVOICE_READY"
    h.signing.counterparty.peer.create_invoice.assert_awaited_once()
    h.signing.buyer.peer.pay.assert_not_awaited()


@pytest.mark.parametrize("depth,height", [(2, 201), (300, 600)])
async def test_depth_and_cltv_window_gate_payments(
    settlement: SettlementRuntime, depth: int, height: int
) -> None:
    h = settlement
    h.transactions[h.parent.txid] = replace(h.transactions[h.parent.txid], confirmations=depth)
    h.chain.height.return_value = height
    h.chain.tip.return_value = (height, "99" * 32)
    await h.buyer.poll(h.sid)
    h.signing.buyer.peer.pay.assert_not_awaited()
    h.signing.counterparty.peer.create_invoice.assert_not_awaited()


@pytest.mark.parametrize("settlement", ["three_block_payment"], indirect=True)
async def test_opt_in_invoice_at_one_and_pay_at_three(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    assert h.signing.buyer.terms(h.sid).proposal.buyer_settlement_depth == 3
    assert h.signing.buyer.terms(h.sid).acceptance.settlement_depth == 1

    h.transactions[h.parent.txid] = replace(h.transactions[h.parent.txid], confirmations=0)
    await h.counterparty.poll(h.sid)
    h.signing.counterparty.peer.create_invoice.assert_not_awaited()

    h.transactions[h.parent.txid] = replace(h.transactions[h.parent.txid], confirmations=1)
    assert await h.counterparty.poll(h.sid) == "INVOICE_READY"
    h.signing.counterparty.peer.create_invoice.assert_awaited_once()

    h.transactions[h.parent.txid] = replace(h.transactions[h.parent.txid], confirmations=2)
    await h.buyer.poll(h.sid)
    h.signing.buyer.peer.pay.assert_not_awaited()

    h.transactions[h.parent.txid] = replace(h.transactions[h.parent.txid], confirmations=3)
    assert await h.buyer.poll(h.sid) == "SETTLED"
    h.signing.buyer.peer.pay.assert_awaited_once()


async def test_old_journal_without_settlement_authorization_has_no_side_effects(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    store = h.signing.buyer.store
    record = store.get(h.sid)
    data = dict(record.data)
    data.pop("settlement_authorized")
    store.update(record, state=record.state, data=data)
    assert await h.buyer.poll(h.sid) == "PARENT_SIGNED"
    h.chain.tip.assert_not_awaited()
    h.signing.buyer.peer.pay.assert_not_awaited()


async def test_interrupted_invoice_creation_recovers_without_recreating(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    h.signing.counterparty.peer.create_invoice.side_effect = TimeoutError("lost invoice reply")
    with pytest.raises(TimeoutError):
        await h.buyer.poll(h.sid)
    h.signing.counterparty.peer.invoice_request.return_value = "lnbcrt10u1pjtestinvoice"
    assert await h.buyer.poll(h.sid) == "SETTLED"
    h.signing.counterparty.peer.create_invoice.assert_awaited_once()
    h.signing.counterparty.peer.invoice_request.assert_awaited_once()


async def test_claim_replacement_and_confirmation(settlement: SettlementRuntime) -> None:
    h = settlement
    await h.buyer.poll(h.sid)
    h.buyer.request = AsyncMock(side_effect=TimeoutError("offline"))
    await h.buyer.poll(h.sid)
    first = h.chain.broadcast.call_args.args[0]
    h.chain.tip.return_value = (206, "99" * 32)
    h.chain.height.return_value = 206
    await h.buyer.poll(h.sid)
    second = h.chain.broadcast.call_args.args[0]
    assert first != second
    terms = h.signing.buyer.terms(h.sid)
    assert verify_signed_claim(terms.escrow, h.parent.escrow_outpoint, SCRIPT, 438, second)
    txid = get_txid(second.hex())
    h.transactions[txid] = replace(h.transactions[txid], confirmations=6, height=206)
    assert await h.buyer.poll(h.sid) == "COMPLETED"
    h.signing.buyer.peer.pay.assert_awaited_once()


@pytest.mark.parametrize("cooperative", [False, True])
async def test_rejected_paid_sweep_can_raise_fee_after_restart(
    settlement: SettlementRuntime, cooperative: bool
) -> None:
    h = settlement
    await h.buyer.poll(h.sid)
    if not cooperative:
        h.buyer.request = AsyncMock(side_effect=TimeoutError("offline"))

    async def reject_low_fee(raw: bytes) -> str:
        parsed = parse_transaction_bytes(raw)
        fee = h.parent.escrow_outpoint.value - sum(output.value for output in parsed.outputs)
        if fee < 1_000:
            raise RuntimeError("mempool min fee not met")
        txid = get_txid(raw.hex())
        h.transactions[txid] = ChainTransaction(txid, raw, 0, None, None)
        return txid

    h.chain.broadcast.side_effect = reject_low_fee
    with pytest.raises(RuntimeError, match="mempool min fee"):
        await h.buyer.poll(h.sid)
    first = h.chain.broadcast.call_args.args[0]
    h.buyer = BuyoutSettlement(h.signing.buyer.store, h.signing.buyer.peer, h.chain)
    # Before the configured bump interval, replay exactly the saved intent.
    with pytest.raises(RuntimeError, match="mempool min fee"):
        await h.buyer.poll(h.sid)
    assert h.chain.broadcast.call_args.args[0] == first
    h.chain.tip.return_value = (206, "99" * 32)
    h.chain.height.return_value = 206
    h.chain.fee_rates.return_value = (10, 1)
    assert await h.buyer.poll(h.sid) == "SPEND_BROADCAST"
    replacement = h.chain.broadcast.call_args.args[0]
    terms = h.signing.buyer.terms(h.sid)
    assert verify_signed_claim(terms.escrow, h.parent.escrow_outpoint, SCRIPT, 1_460, replacement)
    h.signing.buyer.peer.pay.assert_awaited_once()


async def test_unpaid_split_waits_for_csv_boundary(settlement: SettlementRuntime) -> None:
    h = settlement
    for height in (630, 631):
        h.chain.tip.return_value = (height, "99" * 32)
        h.chain.height.return_value = height
        await h.counterparty.poll(h.sid)
        if height == 630:
            h.chain.broadcast.assert_not_awaited()
    raw = h.chain.broadcast.call_args.args[0]
    terms = h.signing.buyer.terms(h.sid)
    assert verify_signed_split(
        terms.escrow,
        h.parent.escrow_outpoint,
        SCRIPT,
        SCRIPT,
        terms.counterparty_split_sat,
        terms.proposal.split_fee,
        terms.proposal.csv_delay,
        raw,
    )


async def test_claim_fee_budget_is_enforced(settlement: SettlementRuntime) -> None:
    h = settlement
    await h.buyer.poll(h.sid)
    h.buyer = BuyoutSettlement(
        h.signing.buyer.store,
        h.signing.buyer.peer,
        h.chain,
        policy=SettlementPolicy(max_chain_fee_sat=100),
    )
    with pytest.raises(ProtocolError, match="fee budget"):
        await h.buyer.poll(h.sid)
    h.chain.broadcast.assert_not_awaited()
