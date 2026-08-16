"""Focused maker-side channel-input escrow tests using synthetic SegWit transactions."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from jmcore.bitcoin import TxInput, TxOutput, create_p2tr_scriptpubkey, serialize_transaction
from jmcore.encryption import CryptoSession
from jmcore.models import NetworkType, Offer, OfferType
from jmswap.channel_escrow import (
    ChannelInputEscrowRecord,
    ChannelInputEscrowTerms,
    ChannelSnapshot,
    EscrowLifecycleState,
    EscrowOutpoint,
    EscrowTimeoutEvidence,
    ParentAuthorization,
    PreparedChannelInputEscrow,
    PrepareTimeoutRequest,
)
from jmwallet.backends.base import UTXO
from jmwallet.wallet.models import UTXOInfo

from maker.channel_input_escrow import ChannelInputEscrowPlan, format_channel_input_witness
from maker.coinjoin import CoinJoinSession, CoinJoinState
from maker.maker_session import MakerSession
from maker.tx_verification import verify_unsigned_transaction

FUNDING_TXID = "11" * 32
WALLET_TXID = "22" * 32
ESCROW_SCRIPT = create_p2tr_scriptpubkey(b"\x33" * 32).hex()
FUNDING_SCRIPT = create_p2tr_scriptpubkey(b"\x44" * 32).hex()
WALLET_SCRIPT = "0014" + "55" * 20
CJ_SCRIPT = "0014" + "66" * 20
CHANGE_SCRIPT = "0014" + "77" * 20


def make_terms() -> ChannelInputEscrowTerms:
    return ChannelInputEscrowTerms(
        snapshot=ChannelSnapshot(
            session_id="escrow-session",
            channel_id="channel-id",
            funding_outpoint=EscrowOutpoint(txid=FUNDING_TXID, vout=1),
            funding_scriptpubkey=FUNDING_SCRIPT,
            local_funding_pubkey="02" + "46" * 32,
            remote_funding_pubkey="03" + "47" * 32,
            capacity_sats=100_000,
            maker_claim_sats=70_000,
            peer_claim_sats=30_000,
            quiescent=True,
            active_htlcs=0,
        ),
        authorizer_pubkey="02" + "66" * 32,
        payment_hash="01" * 32,
        invoice="lnbcrt1privateinvoice",
        invoice_payee_pubkey="02" + "34" * 32,
        invoice_payment_secret="35" * 32,
        invoice_amount_msat=32_000_000,
        invoice_expiry_unix=2_000_000_000,
        invoice_min_final_cltv_delta=18,
        invoice_verified=True,
        premium_sats=2_000,
        timeout_compensation_sats=500,
        escrow_value_sats=80_000,
        timeout_fee_sats=500,
        timeout_height=1_000,
        peer_settle_deadline=970,
        success_broadcast_deadline=980,
        parent_confirmation_depth=1,
        success_confirmation_depth=1,
        success_confirmation_budget_blocks=10,
        reorg_margin_blocks=5,
        safety_delta_blocks=3,
        preimage_budget_blocks=2,
    )


def make_tx(*, include_channel: bool = True, escrow_value: int = 80_000) -> str:
    inputs = [TxInput.from_hex(WALLET_TXID, 0)]
    if include_channel:
        inputs.append(TxInput.from_hex(FUNDING_TXID, 1))
    return serialize_transaction(
        version=2,
        inputs=inputs,
        outputs=[
            TxOutput.from_hex(CJ_SCRIPT, 50_000),
            TxOutput.from_hex(CHANGE_SCRIPT, 31_000),
            TxOutput.from_hex(ESCROW_SCRIPT, escrow_value),
        ],
        locktime=0,
    ).hex()


def wallet_utxo() -> UTXOInfo:
    return UTXOInfo(
        txid=WALLET_TXID,
        vout=0,
        value=60_000,
        address="bcrt1qwallet",
        confirmations=3,
        scriptpubkey=WALLET_SCRIPT,
        path="m/84'/1'/0'/0/0",
        mixdepth=0,
        height=100,
    )


class FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.request: PrepareTimeoutRequest | None = None
        self.prepared: PreparedChannelInputEscrow | None = None

    async def prepare_timeout(self, request: PrepareTimeoutRequest) -> ChannelInputEscrowRecord:
        self.calls.append("prepare")
        self.request = request
        evidence = request.parent_evidence
        self.prepared = PreparedChannelInputEscrow(
            terms=request.terms,
            parent_evidence=evidence,
            timeout_evidence=EscrowTimeoutEvidence(
                timeout_tx_hex="0200",
                timeout_txid="aa" * 32,
                spent_escrow_outpoint=evidence.escrow_outpoint,
                locktime=request.terms.timeout_height,
                maker_output_sats=49_000,
                peer_output_sats=30_500,
                fee_sats=500,
                fully_signed=True,
                signature_verified=True,
            ),
        )
        return ChannelInputEscrowRecord(
            terms=request.terms,
            negotiated_parent_evidence=evidence,
            state=EscrowLifecycleState.TIMEOUT_SIGNED,
            prepared=self.prepared,
        )

    async def authorize_parent(self, session_id: str) -> ChannelInputEscrowRecord:
        self.calls.append("authorize")
        prepared = self.prepared
        assert prepared is not None
        authorization = ParentAuthorization(
            session_id=session_id,
            parent_txid=prepared.parent_evidence.parent_txid,
            prepared_parent_tx_hex=prepared.parent_evidence.parent_tx_hex,
            timeout_txid=prepared.timeout_evidence.timeout_txid,
            channel_funding_outpoint=prepared.terms.snapshot.funding_outpoint,
            channel_input_index=prepared.parent_evidence.channel_input_index,
            channel_input_witness=(b"\x99" * 64,),
            sighash="default",
            signature_verified=True,
            channel_witness_txid_verified=True,
            commits_all_inputs=True,
            commits_all_outputs=True,
        )
        return ChannelInputEscrowRecord(
            terms=prepared.terms,
            negotiated_parent_evidence=prepared.parent_evidence,
            state=EscrowLifecycleState.CHANNEL_SIGNATURE_RELEASED,
            prepared=prepared,
            authorization=authorization,
        )


class FakeBackend:
    def __init__(self, utxos: dict[tuple[str, int], UTXO]) -> None:
        self.utxos = utxos

    def requires_neutrino_metadata(self) -> bool:
        return False

    async def get_utxo(self, txid: str, vout: int) -> UTXO | None:
        return self.utxos.get((txid, vout))


def make_session(plan: ChannelInputEscrowPlan, backend: FakeBackend) -> CoinJoinSession:
    wallet = type("Wallet", (), {"network": "regtest"})()
    return CoinJoinSession(
        taker_nick="taker",
        offer=Offer(
            counterparty="maker",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=1,
            maxsize=1_000_000,
            txfee=1_000,
            cjfee=2_000,
        ),
        wallet=wallet,
        backend=backend,
        channel_input_escrow_plan=plan,
    )


def test_plan_requires_p2tr_script_and_structural_coordinator() -> None:
    terms = make_terms()
    plan = ChannelInputEscrowPlan(terms, ESCROW_SCRIPT.upper(), FakeCoordinator())

    assert plan.escrow_scriptpubkey == ESCROW_SCRIPT
    assert plan.escrow_address(NetworkType.REGTEST).startswith("bcrt1p")
    with pytest.raises(ValueError, match="P2TR"):
        ChannelInputEscrowPlan(terms, WALLET_SCRIPT, FakeCoordinator())
    with pytest.raises(TypeError, match="coordinator"):
        ChannelInputEscrowPlan(terms, ESCROW_SCRIPT, object())


def test_exact_escrow_allocation_rejects_mutations() -> None:
    our_utxos = {(WALLET_TXID, 0): wallet_utxo()}
    base = {
        "inputs": [{"txid": WALLET_TXID, "vout": 0}, {"txid": FUNDING_TXID, "vout": 1}],
        "outputs": [
            {"address": "cj", "value": 50_000},
            {"address": "change", "value": 31_000},
            {"address": "escrow", "value": 80_000},
        ],
    }
    kwargs = {
        "tx_hex": "00",
        "our_utxos": our_utxos,
        "cj_address": "cj",
        "change_address": "change",
        "amount": 50_000,
        "cjfee": 2_000,
        "txfee": 1_000,
        "offer_type": OfferType.SW0_ABSOLUTE,
        "additional_input_values": {(FUNDING_TXID, 1): 100_000},
        "escrow_output": ("escrow", 80_000),
    }

    from unittest.mock import patch

    with patch("maker.tx_verification.parse_transaction", return_value=base):
        assert verify_unsigned_transaction(**kwargs) == (True, "")
    altered_input = {**base, "inputs": base["inputs"][:1]}
    with patch("maker.tx_verification.parse_transaction", return_value=altered_input):
        assert "Additional input" in verify_unsigned_transaction(**kwargs)[1]
    altered_output = {
        **base,
        "outputs": [*base["outputs"][:2], {"address": "escrow", "value": 79_999}],
    }
    with patch("maker.tx_verification.parse_transaction", return_value=altered_output):
        assert "Escrow output value" in verify_unsigned_transaction(**kwargs)[1]
    duplicate_escrow = {
        **base,
        "outputs": [*base["outputs"], {"address": "escrow", "value": 80_000}],
    }
    with patch("maker.tx_verification.parse_transaction", return_value=duplicate_escrow):
        assert "Escrow address appears 2" in verify_unsigned_transaction(**kwargs)[1]
    overpaid_cj = {
        **base,
        "outputs": [
            {"address": "cj", "value": 50_001},
            *base["outputs"][1:],
        ],
    }
    with patch("maker.tx_verification.parse_transaction", return_value=overpaid_cj):
        assert "CJ output value" in verify_unsigned_transaction(**kwargs)[1]


@pytest.mark.asyncio
async def test_chain_snapshot_mismatch_is_rejected_before_advertising() -> None:
    plan = ChannelInputEscrowPlan(make_terms(), ESCROW_SCRIPT, FakeCoordinator())
    backend = FakeBackend(
        {
            (FUNDING_TXID, 1): UTXO(
                FUNDING_TXID, 1, 99_999, "bcrt1pchannel", 2, FUNDING_SCRIPT, height=100
            )
        }
    )
    session = make_session(plan, backend)

    with pytest.raises(ValueError, match="channel snapshot"):
        await session._resolve_channel_input_escrow()

    backend.utxos[(FUNDING_TXID, 1)] = UTXO(
        FUNDING_TXID, 1, 100_000, "bcrt1pchannel", 2, ESCROW_SCRIPT, height=100
    )
    with pytest.raises(ValueError, match="channel snapshot"):
        await session._resolve_channel_input_escrow()


@pytest.mark.asyncio
async def test_authenticated_fill_resolves_provider_only_after_taker_utxo_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend(
        {(WALLET_TXID, 0): UTXO(WALLET_TXID, 0, 60_000, "wallet", 6, WALLET_SCRIPT, height=100)}
    )
    wallet = type("Wallet", (), {"network": "regtest"})()
    session = CoinJoinSession(
        taker_nick="taker",
        offer=Offer(
            counterparty="maker",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=1,
            maxsize=1_000_000,
            txfee=1_000,
            cjfee=2_000,
        ),
        wallet=wallet,
        backend=backend,
        channel_input_escrow_plan_provider=AsyncMock(return_value=None),
    )
    session.state = CoinJoinState.PUBKEY_SENT
    session.commitment = b"\x01" * 32
    monkeypatch.setattr(
        "maker.coinjoin.parse_podle_revelation",
        lambda _revelation: {
            "P": b"",
            "P2": b"",
            "sig": b"",
            "e": b"",
            "txid": WALLET_TXID,
            "vout": 0,
        },
    )
    monkeypatch.setattr("maker.coinjoin.verify_podle", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(session, "_select_our_utxos", AsyncMock(return_value=({}, "", "", -1)))

    success, _response = await session.handle_auth("01" * 32, {}, "")

    assert success is False
    assert session.channel_input_escrow_plan_provider is not None
    session.channel_input_escrow_plan_provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_evidence_rejects_missing_escrow_input_output_and_nonsegwit_prevout() -> None:
    plan = ChannelInputEscrowPlan(make_terms(), ESCROW_SCRIPT, FakeCoordinator())
    backend = FakeBackend(
        {
            (WALLET_TXID, 0): UTXO(WALLET_TXID, 0, 60_000, "wallet", 2, WALLET_SCRIPT, height=100),
            (FUNDING_TXID, 1): UTXO(
                FUNDING_TXID, 1, 100_000, "channel", 2, FUNDING_SCRIPT, height=100
            ),
        }
    )
    session = make_session(plan, backend)

    with pytest.raises(ValueError, match="channel input exactly once"):
        await session._build_escrow_parent_evidence(make_tx(include_channel=False))
    with pytest.raises(ValueError, match="escrow output exactly once"):
        await session._build_escrow_parent_evidence(make_tx(escrow_value=79_999))

    backend.utxos[(WALLET_TXID, 0)].scriptpubkey = "76a914" + "11" * 20 + "88ac"
    with pytest.raises(ValueError, match="native SegWit"):
        await session._build_escrow_parent_evidence(make_tx())


@pytest.mark.asyncio
async def test_wallet_signing_precedes_timeout_and_typed_witness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = FakeCoordinator()
    plan = ChannelInputEscrowPlan(make_terms(), ESCROW_SCRIPT, coordinator)
    backend = FakeBackend(
        {
            (WALLET_TXID, 0): UTXO(WALLET_TXID, 0, 60_000, "wallet", 2, WALLET_SCRIPT, height=100),
            (FUNDING_TXID, 1): UTXO(
                FUNDING_TXID, 1, 100_000, "channel", 2, FUNDING_SCRIPT, height=100
            ),
        }
    )
    session = make_session(plan, backend)
    session.state = CoinJoinState.IOAUTH_SENT

    async def sign_wallet(_tx_hex: str) -> list[str]:
        coordinator.calls.append("wallet")
        return ["wallet-signature"]

    sign = AsyncMock(side_effect=sign_wallet)
    monkeypatch.setattr(session, "_sign_transaction", sign)
    monkeypatch.setattr("maker.coinjoin.verify_unsigned_transaction", lambda **_kwargs: (True, ""))

    success, response = await session.handle_tx(make_tx())

    assert success is True, response
    assert coordinator.calls == ["wallet", "prepare", "authorize"]
    assert response["signatures"] == [
        "wallet-signature",
        f"p2tr:{FUNDING_TXID}:1:{'99' * 64}",
    ]
    sign.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_preparation_failure_releases_no_channel_witness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingCoordinator(FakeCoordinator):
        async def prepare_timeout(self, request: PrepareTimeoutRequest) -> ChannelInputEscrowRecord:
            self.calls.append("prepare")
            raise RuntimeError("timeout preparation failed")

    coordinator = FailingCoordinator()
    plan = ChannelInputEscrowPlan(make_terms(), ESCROW_SCRIPT, coordinator)
    backend = FakeBackend(
        {
            (WALLET_TXID, 0): UTXO(WALLET_TXID, 0, 60_000, "wallet", 2, WALLET_SCRIPT, height=100),
            (FUNDING_TXID, 1): UTXO(
                FUNDING_TXID, 1, 100_000, "channel", 2, FUNDING_SCRIPT, height=100
            ),
        }
    )
    session = make_session(plan, backend)
    session.state = CoinJoinState.IOAUTH_SENT
    sign = AsyncMock(return_value=["wallet-signature"])
    monkeypatch.setattr(session, "_sign_transaction", sign)
    monkeypatch.setattr("maker.coinjoin.verify_unsigned_transaction", lambda **_kwargs: (True, ""))

    success, response = await session.handle_tx(make_tx())

    assert success is False
    assert "timeout preparation failed" in response["error"]
    assert coordinator.calls == ["prepare"]
    sign.assert_awaited_once()


@pytest.mark.asyncio
async def test_wallet_signing_failure_precedes_timeout_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = FakeCoordinator()
    plan = ChannelInputEscrowPlan(make_terms(), ESCROW_SCRIPT, coordinator)
    backend = FakeBackend(
        {
            (WALLET_TXID, 0): UTXO(WALLET_TXID, 0, 60_000, "wallet", 2, WALLET_SCRIPT, height=100),
            (FUNDING_TXID, 1): UTXO(
                FUNDING_TXID, 1, 100_000, "channel", 2, FUNDING_SCRIPT, height=100
            ),
        }
    )
    session = make_session(plan, backend)
    session.state = CoinJoinState.IOAUTH_SENT
    monkeypatch.setattr(session, "_sign_transaction", AsyncMock(return_value=[]))
    monkeypatch.setattr("maker.coinjoin.verify_unsigned_transaction", lambda **_kwargs: (True, ""))

    success, response = await session.handle_tx(make_tx())

    assert success is False
    assert response["error"] == "Failed to sign transaction"
    assert coordinator.calls == []


@pytest.mark.asyncio
async def test_ioauth_appends_only_public_escrow_token_after_channel_metadata() -> None:
    plan = ChannelInputEscrowPlan(make_terms(), ESCROW_SCRIPT, FakeCoordinator())
    session = make_session(plan, FakeBackend({}))
    taker_crypto = CryptoSession()
    session.crypto.setup_encryption(taker_crypto.get_pubkey_hex())
    taker_crypto.setup_encryption(session.crypto.get_pubkey_hex())

    class Client:
        def __init__(self) -> None:
            self.payload = ""

        async def send_private_message(self, _nick: str, _command: str, payload: str) -> None:
            self.payload = payload

    client = Client()
    bot = type("Bot", (), {"directory_clients": {"test": client}})()
    escrow_token = f"escrow:80000:{plan.escrow_address('regtest')}"
    await MakerSession(session).send_response(
        bot,
        "ioauth",
        {
            "utxo_list": f"{WALLET_TXID}:0,{FUNDING_TXID}:1:{FUNDING_SCRIPT}:100",
            "auth_pub": "02" + "11" * 32,
            "cj_addr": "bcrt1qcj",
            "change_addr": "bcrt1qchange",
            "btc_sig": "3044",
            "escrow_output": escrow_token,
        },
    )

    plaintext = taker_crypto.decrypt(client.payload)
    assert plaintext.split()[-1] == escrow_token
    assert plaintext.count("escrow:") == 1
    assert "escrow-session" not in plaintext
    assert "lnbcrt1privateinvoice" not in plaintext


def test_typed_witness_rejects_raw_and_preserves_exact_format() -> None:
    authorization = ParentAuthorization(
        session_id="escrow-session",
        parent_txid="01" * 32,
        prepared_parent_tx_hex="00",
        timeout_txid="02" * 32,
        channel_funding_outpoint=EscrowOutpoint(txid=FUNDING_TXID, vout=1),
        channel_input_index=1,
        channel_input_witness=(b"\x99" * 64,),
        sighash="default",
        signature_verified=True,
        channel_witness_txid_verified=True,
        commits_all_inputs=True,
        commits_all_outputs=True,
    )
    assert format_channel_input_witness(authorization) == f"p2tr:{FUNDING_TXID}:1:{'99' * 64}"
    with pytest.raises(TypeError, match="authorization"):
        format_channel_input_witness(b"\x99" * 64)  # type: ignore[arg-type]
