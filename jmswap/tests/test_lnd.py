"""Unit tests for the LND taproot channel-as-change backend (no live node).

The gRPC stub is faked, so these assert the message construction and PSBT shaping
that the live nodes validated: SIMPLE_TAPROOT + private + PSBT shim on open, a
witness-UTXO-bearing unsigned PSBT, and PsbtVerify-then-PsbtFinalize on completion.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path

import pytest
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    get_txid,
    parse_transaction,
    scriptpubkey_to_address,
    serialize_transaction,
)

from jmswap.errors import LightningError
from jmswap.lnd import (
    JOINMARKET_ESCROW_MESSAGE_TYPE,
    LND_CUSTOM_MESSAGE_MAX_DATA_SIZE,
    LndBackend,
    LndEscrowInvoice,
    LndEscrowStage,
    unsigned_psbt_from_tx,
)
from jmswap.lndrpc import channelescrow_pb2 as escrow
from jmswap.lndrpc import lightning_pb2 as ln

_PSBT_MAGIC = b"\x70\x73\x62\x74\xff"
# A valid signet P2TR address so get_funding_output's address->scriptpubkey round-trips.
_FUNDING_ADDR = scriptpubkey_to_address(bytes.fromhex("5120" + "44" * 32), "signet")


def _simple_tx_hex() -> tuple[str, list[tuple[int, bytes]]]:
    """A 2-input, 2-output unsigned tx + its per-input (value, scriptpubkey)."""
    inputs = [
        TxInput.from_hex("11" * 32, 0, sequence=0xFFFFFFFD),
        TxInput.from_hex("22" * 32, 1, sequence=0xFFFFFFFD),
    ]
    spk0 = bytes.fromhex("0014" + "33" * 20)  # P2WPKH
    spk1 = bytes.fromhex("5120" + "44" * 32)  # P2TR
    outputs = [
        TxOutput(value=1_000_000, script=bytes.fromhex("5120" + "55" * 32)),
        TxOutput(value=900_000, script=bytes.fromhex("0014" + "66" * 20)),
    ]
    tx_hex = serialize_transaction(2, inputs, outputs, 0).hex()
    return tx_hex, [(700_000, spk0), (1_300_000, spk1)]


def test_unsigned_psbt_has_magic_and_witness_utxos() -> None:
    tx_hex, iu = _simple_tx_hex()
    psbt = unsigned_psbt_from_tx(tx_hex, iu)
    assert psbt.startswith(_PSBT_MAGIC)
    # both input witness UTXO values appear (8-byte LE) in the per-input maps
    assert (700_000).to_bytes(8, "little") in psbt
    assert (1_300_000).to_bytes(8, "little") in psbt


def test_unsigned_psbt_rejects_mismatched_input_utxos() -> None:
    tx_hex, iu = _simple_tx_hex()
    with pytest.raises(ValueError, match="input_utxos"):
        unsigned_psbt_from_tx(tx_hex, iu[:1])


class _FakeStream:
    def __init__(self, update: ln.OpenStatusUpdate) -> None:
        self._update = update

    async def read(self) -> ln.OpenStatusUpdate:
        return self._update


class _FakeCustomMessageStream:
    def __init__(self, messages: list[ln.CustomMessage]) -> None:
        self._messages = messages
        self._index = 0

    def __aiter__(self) -> AsyncIterator[ln.CustomMessage]:
        return self

    async def __anext__(self) -> ln.CustomMessage:
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        message = self._messages[self._index]
        self._index += 1
        return message


class _FakeStub:
    def __init__(self) -> None:
        self.open_request: ln.OpenChannelRequest | None = None
        self.funding_steps: list[ln.FundingTransitionMsg] = []
        self.custom_message_request: ln.SendCustomMessageRequest | None = None
        self.subscribe_custom_messages_request: ln.SubscribeCustomMessagesRequest | None = None
        self.custom_message_send_status = "message sent successfully"
        self.custom_message_stream: list[ln.CustomMessage] = []
        self.add_invoice_request: ln.Invoice | None = None
        self.decode_pay_req_request: ln.PayReqString | None = None
        self.get_info_request: ln.GetInfoRequest | None = None
        self.local_pubkey = "02" + "aa" * 32
        self.payment_secret = b"s" * 32
        self.payment_hash = sha256(b"p" * 32).digest()
        self.amount_msat = 1
        self.expiry = 3_600
        self.cltv_expiry = 40
        self.timestamp = int(time.time())
        self.add_response_hash: bytes | None = None
        self.decoded_destination: str | None = None
        self.decoded_payment_hash: str | None = None
        self.decoded_payment_secret: bytes | None = None
        self.decoded_amount_msat: int | None = None
        self.decoded_expiry: int | None = None
        self.decoded_cltv_expiry: int | None = None
        self.duplicate_invoice = False
        self.lookup_invoice_request: ln.PaymentHash | None = None
        self.lookup_payment_secret: bytes | None = None
        self.lookup_amount_msat: int | None = None
        self.lookup_expiry: int | None = None
        self.lookup_cltv_expiry: int | None = None
        self.lookup_memo: str | None = None
        self.lookup_private = True
        self.lookup_state = ln.Invoice.OPEN

    def OpenChannel(self, req: ln.OpenChannelRequest) -> _FakeStream:  # noqa: N802
        self.open_request = req
        return _FakeStream(
            ln.OpenStatusUpdate(
                psbt_fund=ln.ReadyForPsbtFunding(
                    funding_address=_FUNDING_ADDR, funding_amount=1_500_000
                )
            )
        )

    async def ConnectPeer(self, req: ln.ConnectPeerRequest) -> ln.ConnectPeerResponse:  # noqa: N802
        return ln.ConnectPeerResponse()

    async def GetInfo(self, request: ln.GetInfoRequest) -> ln.GetInfoResponse:  # noqa: N802
        self.get_info_request = request
        return ln.GetInfoResponse(identity_pubkey=self.local_pubkey)

    async def AddInvoice(self, request: ln.Invoice) -> ln.AddInvoiceResponse:  # noqa: N802
        self.add_invoice_request = request
        if self.duplicate_invoice:
            raise RuntimeError("invoice already exists")
        self.payment_hash = sha256(request.r_preimage).digest()
        self.amount_msat = request.value_msat
        self.expiry = request.expiry
        self.cltv_expiry = request.cltv_expiry
        return ln.AddInvoiceResponse(
            r_hash=self.add_response_hash or self.payment_hash,
            payment_request="LNSB1ESCROW",
            payment_addr=self.payment_secret,
        )

    async def LookupInvoice(self, request: ln.PaymentHash) -> ln.Invoice:  # noqa: N802
        self.lookup_invoice_request = request
        invoice_request = self.add_invoice_request
        if invoice_request is None:
            invoice_request = ln.Invoice(
                r_preimage=b"p" * 32,
                value_msat=self.amount_msat,
                expiry=self.expiry,
                cltv_expiry=self.cltv_expiry,
                memo="",
                private=True,
            )
        return ln.Invoice(
            r_preimage=invoice_request.r_preimage,
            r_hash=self.payment_hash,
            value_msat=self.lookup_amount_msat or invoice_request.value_msat,
            payment_request="LNSB1ESCROW",
            expiry=self.lookup_expiry or invoice_request.expiry,
            cltv_expiry=self.lookup_cltv_expiry or invoice_request.cltv_expiry,
            memo=invoice_request.memo if self.lookup_memo is None else self.lookup_memo,
            private=self.lookup_private,
            payment_addr=self.payment_secret
            if self.lookup_payment_secret is None
            else self.lookup_payment_secret,
            state=self.lookup_state,
        )

    async def DecodePayReq(self, request: ln.PayReqString) -> ln.PayReq:  # noqa: N802
        self.decode_pay_req_request = request
        return ln.PayReq(
            destination=self.decoded_destination or self.local_pubkey,
            payment_hash=self.decoded_payment_hash or self.payment_hash.hex(),
            num_msat=self.decoded_amount_msat or self.amount_msat,
            timestamp=self.timestamp,
            expiry=self.decoded_expiry or self.expiry,
            cltv_expiry=self.decoded_cltv_expiry or self.cltv_expiry,
            payment_addr=self.decoded_payment_secret or self.payment_secret,
        )

    async def FundingStateStep(  # noqa: N802
        self, msg: ln.FundingTransitionMsg
    ) -> ln.FundingStateStepResp:
        self.funding_steps.append(msg)
        return ln.FundingStateStepResp()

    async def SendCustomMessage(  # noqa: N802
        self, request: ln.SendCustomMessageRequest
    ) -> ln.SendCustomMessageResponse:
        self.custom_message_request = request
        return ln.SendCustomMessageResponse(status=self.custom_message_send_status)

    def SubscribeCustomMessages(  # noqa: N802
        self, request: ln.SubscribeCustomMessagesRequest
    ) -> _FakeCustomMessageStream:
        self.subscribe_custom_messages_request = request
        return _FakeCustomMessageStream(self.custom_message_stream)


class _FakeEscrowStub:
    def __init__(self) -> None:
        self.freeze_request: escrow.FreezeChannelRequest | None = None
        self.prepare_request: escrow.PrepareParentRequest | None = None
        self.begin_request: escrow.BeginSigningRequest | None = None
        self.sign_request: escrow.SignParentRequest | None = None
        self.finalize_request: escrow.FinalizeParentRequest | None = None
        self.status_request: escrow.StatusRequest | None = None
        self.cancel_request: escrow.CancelRequest | None = None
        self.parent_tx = b""
        self.parent_txid = b""
        self.finalized_witnesses: list[list[bytes]] = [[], [b"f" * 64]]
        self.stage = LndEscrowStage.LOCKED
        self.durable_local_public_nonce = b""
        self.durable_remote_public_nonce = b""
        self.durable_local_partial_signature = b""
        self.active_attempt_id = b""
        self.active_local_public_nonce = b""
        self.finalized_parent_tx = b""
        self.funding_script = bytes.fromhex("5120" + "55" * 32)
        self.local_funding_pubkey = bytes.fromhex("02" + "66" * 32)
        self.remote_funding_pubkey = bytes.fromhex("03" + "77" * 32)

    async def FreezeChannel(  # noqa: N802
        self, request: escrow.FreezeChannelRequest
    ) -> escrow.FreezeChannelResponse:
        self.freeze_request = request
        return escrow.FreezeChannelResponse(
            capacity_sat=2_000_000,
            local_claim_sat=1_200_000,
            remote_claim_sat=800_000,
            peer_pubkey=bytes.fromhex("02" + "44" * 32),
            funding_outpoint=request.channel_point,
            funding_script=self.funding_script,
            local_funding_pubkey=self.local_funding_pubkey,
            remote_funding_pubkey=self.remote_funding_pubkey,
        )

    async def PrepareParent(  # noqa: N802
        self, request: escrow.PrepareParentRequest
    ) -> escrow.PrepareParentResponse:
        self.prepare_request = request
        self.parent_tx = bytes(request.raw_parent_tx)
        self.parent_txid = bytes.fromhex(get_txid(self.parent_tx.hex()))[::-1]
        self.stage = LndEscrowStage.PARENT_PREPARED
        return escrow.PrepareParentResponse(txid=self.parent_txid)

    async def BeginSigning(  # noqa: N802
        self, request: escrow.BeginSigningRequest
    ) -> escrow.BeginSigningResponse:
        self.begin_request = request
        self.active_attempt_id = b"a" * 32
        self.active_local_public_nonce = b"n" * 66
        return escrow.BeginSigningResponse(
            attempt_id=self.active_attempt_id,
            local_public_nonce=self.active_local_public_nonce,
        )

    async def SignParent(  # noqa: N802
        self, request: escrow.SignParentRequest
    ) -> escrow.SignParentResponse:
        self.sign_request = request
        self.stage = LndEscrowStage.PARENT_SIGNED
        self.durable_local_public_nonce = self.active_local_public_nonce
        self.durable_remote_public_nonce = bytes(request.remote_public_nonce)
        self.durable_local_partial_signature = b"s" * 32
        return escrow.SignParentResponse(local_partial_signature=b"s" * 32)

    async def FinalizeParent(  # noqa: N802
        self, request: escrow.FinalizeParentRequest
    ) -> escrow.FinalizeParentResponse:
        self.finalize_request = request
        self.stage = LndEscrowStage.FINALIZED
        parent = parse_transaction(self.parent_tx.hex())
        finalized = serialize_transaction(
            parent.version,
            parent.inputs,
            parent.outputs,
            parent.locktime,
            witnesses=self.finalized_witnesses,
        )
        self.finalized_parent_tx = finalized
        self.active_attempt_id = b""
        self.active_local_public_nonce = b""
        return escrow.FinalizeParentResponse(raw_tx=finalized, txid=self.parent_txid)

    async def Status(self, request: escrow.StatusRequest) -> escrow.StatusResponse:  # noqa: N802
        self.status_request = request
        return escrow.StatusResponse(
            stage=self.stage,
            parent_txid=self.parent_txid,
            finalized=self.stage is LndEscrowStage.FINALIZED,
            durable_local_public_nonce=self.durable_local_public_nonce,
            durable_remote_public_nonce=self.durable_remote_public_nonce,
            durable_local_partial_signature=self.durable_local_partial_signature,
            finalized_parent_tx=self.finalized_parent_tx,
            active_attempt_id=self.active_attempt_id,
            active_local_public_nonce=self.active_local_public_nonce,
        )

    async def Cancel(self, request: escrow.CancelRequest) -> escrow.CancelResponse:  # noqa: N802
        self.cancel_request = request
        return escrow.CancelResponse(link_resumed=True)


async def test_create_escrow_invoice_uses_exact_request_and_returns_verified_artifact() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    stub = _FakeStub()
    backend._stub = stub
    preimage = b"p" * 32

    artifact = await backend.create_escrow_invoice(125_000, preimage, 900, 18, memo="escrow")

    request = stub.add_invoice_request
    assert request is not None
    assert request.r_preimage == preimage
    assert request.r_hash == b""
    assert request.value_msat == 125_000
    assert request.expiry == 900
    assert request.cltv_expiry == 18
    assert request.memo == "escrow"
    assert request.private is True
    assert stub.get_info_request is not None
    assert stub.decode_pay_req_request is not None
    assert stub.decode_pay_req_request.pay_req == "LNSB1ESCROW"
    assert artifact == LndEscrowInvoice(
        invoice="lnsb1escrow",
        payment_hash=sha256(preimage).hexdigest(),
        payee_pubkey=stub.local_pubkey,
        payment_secret=stub.payment_secret.hex(),
        amount_msat=125_000,
        expiry_unix=stub.timestamp + 900,
        min_final_cltv_delta=18,
    )


async def test_decode_escrow_invoice_returns_signed_normalized_terms() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    stub = _FakeStub()
    stub.decoded_destination = "03" + "bb" * 32
    backend._stub = stub

    artifact = await backend.decode_escrow_invoice("LNSB1ESCROW")

    assert artifact.invoice == "lnsb1escrow"
    assert artifact.payee_pubkey == stub.decoded_destination
    assert artifact.payment_hash == stub.payment_hash.hex()
    assert artifact.payment_secret == stub.payment_secret.hex()
    assert artifact.amount_msat == stub.amount_msat
    assert artifact.expiry_unix == stub.timestamp + stub.expiry
    assert artifact.min_final_cltv_delta == stub.cltv_expiry
    assert stub.get_info_request is None


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"amount_msat": True}, "amount_msat"),
        ({"amount_msat": 0}, "amount_msat"),
        ({"amount_msat": -1}, "amount_msat"),
        ({"expiry_seconds": True}, "expiry_seconds"),
        ({"expiry_seconds": 0}, "expiry_seconds"),
        ({"expiry_seconds": -1}, "expiry_seconds"),
        ({"min_final_cltv_delta": True}, "min_final_cltv_delta"),
        ({"min_final_cltv_delta": 0}, "min_final_cltv_delta"),
        ({"min_final_cltv_delta": -1}, "min_final_cltv_delta"),
        ({"preimage": b"short"}, "preimage"),
        ({"preimage": bytearray(b"p" * 32)}, "preimage"),
        ({"memo": 1}, "memo"),
    ],
)
async def test_create_escrow_invoice_rejects_invalid_inputs(
    kwargs: dict[str, object], match: str
) -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    args: dict[str, object] = {
        "amount_msat": 1_000,
        "preimage": b"p" * 32,
        "expiry_seconds": 3_600,
        "min_final_cltv_delta": 18,
    }
    args.update(kwargs)

    with pytest.raises(ValueError, match=match):
        await backend.create_escrow_invoice(**args)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("add_response_hash", b"h" * 32),
        ("decoded_amount_msat", 2_000),
        ("decoded_destination", "03" + "bb" * 32),
        ("decoded_payment_secret", b"t" * 32),
        ("decoded_expiry", 3_601),
        ("decoded_cltv_expiry", 19),
    ],
)
async def test_create_escrow_invoice_rejects_response_mismatches(
    attribute: str, value: object
) -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    stub = _FakeStub()
    setattr(stub, attribute, value)
    backend._stub = stub

    with pytest.raises(LightningError):
        await backend.create_escrow_invoice(1_000, b"p" * 32, 3_600, 18)


async def test_create_escrow_invoice_recovers_exact_duplicate_by_hash() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    stub = _FakeStub()
    preimage = b"p" * 32
    await stub.AddInvoice(
        ln.Invoice(
            r_preimage=preimage,
            value_msat=1_000,
            expiry=3_600,
            cltv_expiry=18,
            memo="escrow",
            private=True,
        )
    )
    stub.duplicate_invoice = True
    backend._stub = stub

    invoice = await backend.create_escrow_invoice(1_000, preimage, 3_600, 18, memo="escrow")

    assert stub.lookup_invoice_request is not None
    assert stub.lookup_invoice_request.r_hash == sha256(preimage).digest()
    assert invoice.payment_hash == sha256(preimage).hexdigest()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("lookup_payment_secret", b"x" * 32),
        ("lookup_amount_msat", 2_000),
        ("lookup_expiry", 3_601),
        ("lookup_cltv_expiry", 19),
        ("lookup_memo", "other"),
        ("lookup_private", False),
        ("lookup_state", ln.Invoice.SETTLED),
        ("lookup_state", ln.Invoice.CANCELED),
    ],
)
async def test_create_escrow_invoice_rejects_conflicting_duplicate(
    attribute: str, value: object
) -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    stub = _FakeStub()
    preimage = b"p" * 32
    await stub.AddInvoice(
        ln.Invoice(
            r_preimage=preimage,
            value_msat=1_000,
            expiry=3_600,
            cltv_expiry=18,
            memo="escrow",
            private=True,
        )
    )
    stub.duplicate_invoice = True
    setattr(stub, attribute, value)
    backend._stub = stub

    with pytest.raises(LightningError):
        await backend.create_escrow_invoice(1_000, preimage, 3_600, 18, memo="escrow")


async def test_open_uses_simple_taproot_private_psbt_shim() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    node = "02" + "ab" * 32
    ucid = await backend.open_channel_external_funding(
        node, "127.0.0.1:9836", 1_500_000, push_to_counterparty_msat=400_000_000
    )

    req = backend._stub.open_request
    assert req is not None
    assert req.commitment_type == ln.CommitmentType.SIMPLE_TAPROOT
    assert req.private is True
    assert req.local_funding_amount == 1_500_000
    assert req.push_sat == 400_000
    assert req.funding_shim.psbt_shim.no_publish is True
    assert req.funding_shim.psbt_shim.pending_chan_id == bytes.fromhex(ucid)

    ready, spk_hex, value = await backend.get_funding_output(ucid)
    assert ready is True
    assert value == 1_500_000
    assert spk_hex  # address converted to a scriptpubkey


async def test_open_uses_caller_selected_pending_channel_id() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    user_channel_id = "ab" * 32

    opened_id = await backend.open_channel_external_funding(
        "02" + "ab" * 32,
        "127.0.0.1:9836",
        1_500_000,
        user_channel_id=user_channel_id,
    )

    assert opened_id == user_channel_id
    assert backend._stub.open_request is not None
    assert backend._stub.open_request.funding_shim.psbt_shim.pending_chan_id == bytes.fromhex(
        user_channel_id
    )


async def test_send_custom_message_uses_default_escrow_type() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    stub = _FakeStub()
    backend._stub = stub
    peer_node_id = "02" + "ab" * 32

    await backend.send_custom_message(peer_node_id, b"escrow payload")

    request = stub.custom_message_request
    assert request is not None
    assert request.peer == bytes.fromhex(peer_node_id)
    assert request.type == JOINMARKET_ESCROW_MESSAGE_TYPE
    assert request.data == b"escrow payload"


async def test_send_custom_message_rejects_invalid_peer_type_and_data() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()

    with pytest.raises(ValueError, match="compressed public key"):
        await backend.send_custom_message("04" + "ab" * 32, b"payload")
    with pytest.raises(ValueError, match="32768..65535"):
        await backend.send_custom_message("02" + "ab" * 32, b"payload", 32_767)
    with pytest.raises(ValueError, match="integer"):
        await backend.send_custom_message("02" + "ab" * 32, b"payload", True)
    with pytest.raises(ValueError, match="cannot be empty"):
        await backend.send_custom_message("02" + "ab" * 32, b"")
    with pytest.raises(ValueError, match="must be bytes"):
        await backend.send_custom_message("02" + "ab" * 32, bytearray(b"payload"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at most"):
        await backend.send_custom_message(
            "02" + "ab" * 32, b"x" * (LND_CUSTOM_MESSAGE_MAX_DATA_SIZE + 1)
        )


async def test_subscribe_custom_messages_filters_other_types() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    stub = _FakeStub()
    stub.custom_message_stream = [
        ln.CustomMessage(peer=bytes.fromhex("02" + "aa" * 32), type=50_000, data=b"ignored"),
        ln.CustomMessage(
            peer=bytes.fromhex("03" + "bb" * 32),
            type=JOINMARKET_ESCROW_MESSAGE_TYPE,
            data=b"escrow payload",
        ),
    ]
    backend._stub = stub

    messages = [message async for message in backend.subscribe_custom_messages()]

    assert stub.subscribe_custom_messages_request is not None
    assert messages[0].peer_node_id == "03" + "bb" * 32
    assert messages[0].data == b"escrow payload"


@pytest.mark.parametrize(
    "message",
    [
        ln.CustomMessage(
            peer=bytes.fromhex("04" + "aa" * 32),
            type=JOINMARKET_ESCROW_MESSAGE_TYPE,
            data=b"payload",
        ),
        ln.CustomMessage(
            peer=bytes.fromhex("02" + "aa" * 32),
            type=JOINMARKET_ESCROW_MESSAGE_TYPE,
            data=b"",
        ),
    ],
)
async def test_subscribe_custom_messages_rejects_malformed_selected_message(
    message: ln.CustomMessage,
) -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    stub = _FakeStub()
    stub.custom_message_stream = [message]
    backend._stub = stub

    with pytest.raises(ValueError):
        await anext(backend.subscribe_custom_messages())


async def test_send_custom_message_rejects_explicit_failure_status() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    stub = _FakeStub()
    stub.custom_message_send_status = "message failed"
    backend._stub = stub

    with pytest.raises(RuntimeError, match="unexpected custom message status"):
        await backend.send_custom_message("02" + "ab" * 32, b"payload")


def test_from_paths_loads_scoped_escrow_macaroon(tmp_path: Path) -> None:
    tls_path = tmp_path / "tls.cert"
    macaroon_path = tmp_path / "admin.macaroon"
    escrow_macaroon_path = tmp_path / "channelescrow.macaroon"
    tls_path.write_bytes(b"tls")
    macaroon_path.write_bytes(b"admin")
    escrow_macaroon_path.write_bytes(b"escrow")

    backend = LndBackend.from_paths(
        "127.0.0.1:10009",
        str(tls_path),
        str(macaroon_path),
        escrow_macaroon_path=str(escrow_macaroon_path),
    )

    assert backend.tls_cert == b"tls"
    assert backend.macaroon_hex == b"admin".hex()
    assert backend.escrow_macaroon_hex == b"escrow".hex()


@pytest.mark.parametrize("push_msat", [-1, 1, 1_500_000_000])
async def test_open_rejects_invalid_push(push_msat: int) -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()

    with pytest.raises(ValueError, match="push"):
        await backend.open_channel_external_funding(
            "02" + "ab" * 32,
            "127.0.0.1:9836",
            1_500_000,
            push_to_counterparty_msat=push_msat,
        )


async def test_provide_funding_outpoint_verifies_with_skip_finalize() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    ucid = await backend.open_channel_external_funding(
        "02" + "ab" * 32, "127.0.0.1:9836", 1_500_000
    )
    tx_hex, iu = _simple_tx_hex()

    await backend.provide_funding_outpoint(ucid, "ab" * 32, 0, tx_hex, iu)

    steps = backend._stub.funding_steps
    # Only a verify with skip_finalize: LND watches the outpoint, the taker broadcasts.
    assert [s.WhichOneof("trigger") for s in steps] == ["psbt_verify"]
    assert steps[0].psbt_verify.pending_chan_id == bytes.fromhex(ucid)
    assert steps[0].psbt_verify.funded_psbt.startswith(_PSBT_MAGIC)
    assert steps[0].psbt_verify.skip_finalize is True


async def test_provide_funding_outpoint_requires_tx_and_utxos() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    ucid = await backend.open_channel_external_funding(
        "02" + "ab" * 32, "127.0.0.1:9836", 1_500_000
    )
    with pytest.raises(ValueError, match="raw_tx_hex"):
        await backend.provide_funding_outpoint(ucid, "ab" * 32, 0)


async def test_cancel_external_channel_funding_cancels_pending_shim() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    ucid = await backend.open_channel_external_funding(
        "02" + "ab" * 32, "127.0.0.1:9836", 1_500_000
    )

    await backend.cancel_external_channel_funding(ucid)

    steps = backend._stub.funding_steps
    assert [s.WhichOneof("trigger") for s in steps] == ["shim_cancel"]
    assert steps[0].shim_cancel.pending_chan_id == bytes.fromhex(ucid)
    assert ucid not in backend._pending


async def test_cancel_external_channel_funding_accepts_missing_local_pending_shim() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    user_channel_id = "ab" * 32

    await backend.cancel_external_channel_funding(user_channel_id)

    steps = backend._stub.funding_steps
    assert [step.WhichOneof("trigger") for step in steps] == ["shim_cancel"]
    assert steps[0].shim_cancel.pending_chan_id == bytes.fromhex(user_channel_id)


async def test_recover_external_channel_funding_restores_and_checks_exact_terms() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    user_channel_id = "ab" * 32

    await backend.recover_external_channel_funding(user_channel_id, _FUNDING_ADDR, 1_500_000)

    pending = backend._pending[user_channel_id]
    assert pending.pending_chan_id == bytes.fromhex(user_channel_id)
    assert pending.stream is None
    assert pending.funding_address == _FUNDING_ADDR
    assert pending.funding_amount == 1_500_000
    await backend.recover_external_channel_funding(user_channel_id, _FUNDING_ADDR, 1_500_000)
    with pytest.raises(ValueError, match="conflicts"):
        await backend.recover_external_channel_funding(user_channel_id, _FUNDING_ADDR, 1_500_001)
    with pytest.raises(ValueError, match="32-byte hexadecimal"):
        await backend.recover_external_channel_funding("bad", _FUNDING_ADDR, 1_500_000)


async def test_patched_lnd_channel_input_signing_lifecycle() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    escrow_stub = _FakeEscrowStub()
    backend._escrow_stub = escrow_stub
    channel_txid = "22" * 32
    channel_point = f"{channel_txid}:1"
    session_id = "maker-private-session"

    snapshot = await backend.freeze_channel_input(session_id, channel_point)
    assert snapshot.funding_outpoint.txid == channel_txid
    assert snapshot.funding_outpoint.vout == 1
    assert snapshot.maker_claim_sats == 1_200_000
    assert snapshot.peer_claim_sats == 800_000
    assert snapshot.funding_scriptpubkey == "5120" + "55" * 32
    assert snapshot.local_funding_pubkey == "02" + "66" * 32
    assert snapshot.remote_funding_pubkey == "03" + "77" * 32
    assert escrow_stub.freeze_request is not None
    assert (
        escrow_stub.freeze_request.channel_point.funding_txid == bytes.fromhex(channel_txid)[::-1]
    )
    assert len(escrow_stub.freeze_request.session_id) == 32

    parent_tx_hex, input_utxos = _simple_tx_hex()
    parent_txid = await backend.prepare_channel_input_parent(
        session_id, snapshot.funding_outpoint, parent_tx_hex, input_utxos, 1
    )
    assert parent_txid == get_txid(parent_tx_hex)
    assert escrow_stub.prepare_request is not None
    assert escrow_stub.prepare_request.channel_input_index == 1
    assert [prev.value_sat for prev in escrow_stub.prepare_request.prev_outs] == [
        700_000,
        1_300_000,
    ]

    attempt = await backend.begin_channel_input_signing(session_id, snapshot.funding_outpoint)
    assert attempt.attempt_id == b"a" * 32
    assert attempt.public_nonce == b"n" * 66
    partial = await backend.sign_channel_input_parent(
        session_id, snapshot.funding_outpoint, attempt, b"r" * 66
    )
    assert partial == b"s" * 32
    assert escrow_stub.sign_request is not None
    assert escrow_stub.sign_request.remote_public_nonce == b"r" * 66

    signed_status = await backend.channel_input_escrow_status(session_id, snapshot.funding_outpoint)
    assert signed_status.stage is LndEscrowStage.PARENT_SIGNED
    assert signed_status.durable_local_public_nonce == b"n" * 66
    assert signed_status.durable_remote_public_nonce == b"r" * 66
    assert signed_status.durable_local_partial_signature == b"s" * 32
    assert signed_status.active_attempt == attempt

    finalized = await backend.finalize_channel_input_parent(
        session_id, snapshot.funding_outpoint, attempt, b"p" * 32
    )
    assert finalized.txid == parent_txid
    assert get_txid(finalized.tx_hex) == get_txid(parent_tx_hex)
    assert finalized.channel_input_index == 1
    assert finalized.channel_input_witness == (b"f" * 64,)
    assert escrow_stub.finalize_request is not None
    assert escrow_stub.finalize_request.remote_partial_signature == b"p" * 32

    recovered = await backend.recover_channel_input_parent(
        session_id, snapshot.funding_outpoint, b"p" * 32
    )
    assert recovered == finalized
    assert escrow_stub.finalize_request.attempt_id == b""

    status = await backend.channel_input_escrow_status(session_id, snapshot.funding_outpoint)
    assert status.stage is LndEscrowStage.FINALIZED
    assert status.parent_txid == parent_txid
    assert status.finalized is True
    assert status.durable_local_public_nonce == b"n" * 66
    assert status.durable_remote_public_nonce == b"r" * 66
    assert status.durable_local_partial_signature == b"s" * 32
    assert status.finalized_parent == finalized
    assert status.active_attempt is None
    assert await backend.cancel_channel_input_escrow(session_id, snapshot.funding_outpoint) is True
    assert escrow_stub.cancel_request is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("funding_script", bytes.fromhex("0014" + "55" * 20)),
        ("funding_script", bytes.fromhex("5120" + "55" * 31)),
        ("local_funding_pubkey", b"\x04" + b"l" * 32),
        ("remote_funding_pubkey", b"short"),
        ("remote_funding_pubkey", bytes.fromhex("02" + "66" * 32)),
    ],
)
async def test_freeze_channel_input_rejects_invalid_funding_signing_data(
    field: str, value: bytes
) -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    escrow_stub = _FakeEscrowStub()
    setattr(escrow_stub, field, value)
    backend._escrow_stub = escrow_stub

    with pytest.raises(RuntimeError, match="invalid channel funding signing data"):
        await backend.freeze_channel_input("session", f"{'22' * 32}:1")


async def test_patched_lnd_status_recovers_after_backend_restart() -> None:
    escrow_stub = _FakeEscrowStub()
    restarted_backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    restarted_backend._stub = _FakeStub()
    restarted_backend._escrow_stub = escrow_stub
    funding_outpoint = LndBackend._parse_channel_point(f"{'12' * 32}:3")

    status = await restarted_backend.channel_input_escrow_status(
        "maker-private-session", funding_outpoint
    )

    assert status.stage is LndEscrowStage.LOCKED
    assert status.parent_txid is None
    assert escrow_stub.status_request is not None
    assert escrow_stub.status_request.channel_point.output_index == 3


async def test_patched_lnd_rejects_parent_txid_before_preparation() -> None:
    escrow_stub = _FakeEscrowStub()
    escrow_stub.parent_txid = b"\x00" * 32
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    backend._escrow_stub = escrow_stub
    funding_outpoint = LndBackend._parse_channel_point(f"{'12' * 32}:3")

    with pytest.raises(RuntimeError, match="inconsistent escrow parent"):
        await backend.channel_input_escrow_status("session", funding_outpoint)


@pytest.mark.parametrize(
    ("stage", "updates", "message"),
    [
        (
            LndEscrowStage.PARENT_SIGNED,
            {},
            "durable signing status",
        ),
        (
            LndEscrowStage.PARENT_PREPARED,
            {"durable_local_public_nonce": b"n" * 66},
            "durable signing status",
        ),
        (
            LndEscrowStage.PARENT_PREPARED,
            {"active_attempt_id": b"a" * 32},
            "active signing status",
        ),
        (
            LndEscrowStage.FINALIZED,
            {
                "durable_local_public_nonce": b"n" * 66,
                "durable_remote_public_nonce": b"r" * 66,
                "durable_local_partial_signature": b"s" * 32,
            },
            "finalized parent status",
        ),
    ],
)
async def test_patched_lnd_rejects_inconsistent_recovery_status(
    stage: LndEscrowStage, updates: dict[str, bytes], message: str
) -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    escrow_stub = _FakeEscrowStub()
    backend._escrow_stub = escrow_stub
    funding_outpoint = LndBackend._parse_channel_point(f"{'12' * 32}:3")
    parent_tx_hex, input_utxos = _simple_tx_hex()
    await backend.prepare_channel_input_parent(
        "session", funding_outpoint, parent_tx_hex, input_utxos, 1
    )
    escrow_stub.stage = stage
    for field, value in updates.items():
        setattr(escrow_stub, field, value)

    with pytest.raises(RuntimeError, match=message):
        await backend.channel_input_escrow_status("session", funding_outpoint)


async def test_patched_lnd_rejects_invalid_remote_signing_data() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    backend._escrow_stub = _FakeEscrowStub()
    funding_outpoint = LndBackend._parse_channel_point(f"{'12' * 32}:3")
    attempt = await backend.begin_channel_input_signing("session", funding_outpoint)

    with pytest.raises(ValueError, match="public nonce"):
        await backend.sign_channel_input_parent("session", funding_outpoint, attempt, b"short")
    with pytest.raises(ValueError, match="partial signature"):
        await backend.finalize_channel_input_parent("session", funding_outpoint, attempt, b"short")


@pytest.mark.parametrize(
    "witnesses",
    [
        [[], []],
        [[b"unexpected"], [b"f" * 64]],
        [[], [b"f" * 63]],
        [[], [b"f" * 64, b"extra"]],
    ],
)
async def test_patched_lnd_rejects_invalid_finalized_channel_witness(
    witnesses: list[list[bytes]],
) -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    escrow_stub = _FakeEscrowStub()
    backend._escrow_stub = escrow_stub
    funding_outpoint = LndBackend._parse_channel_point(f"{'22' * 32}:1")
    parent_tx_hex, input_utxos = _simple_tx_hex()
    await backend.prepare_channel_input_parent(
        "session", funding_outpoint, parent_tx_hex, input_utxos, 1
    )
    attempt = await backend.begin_channel_input_signing("session", funding_outpoint)
    await backend.sign_channel_input_parent("session", funding_outpoint, attempt, b"r" * 66)
    escrow_stub.finalized_witnesses = witnesses

    with pytest.raises(RuntimeError, match="invalid channel-input witness"):
        await backend.finalize_channel_input_parent("session", funding_outpoint, attempt, b"p" * 32)
