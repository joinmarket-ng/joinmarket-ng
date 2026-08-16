"""Unit tests for the LND taproot channel-as-change backend (no live node).

The gRPC stub is faked, so these assert the message construction and PSBT shaping
that the live nodes validated: SIMPLE_TAPROOT + private + PSBT shim on open, a
witness-UTXO-bearing unsigned PSBT, and PsbtVerify-then-PsbtFinalize on completion.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    get_txid,
    scriptpubkey_to_address,
    serialize_transaction,
)

from jmswap.lnd import (
    JOINMARKET_ESCROW_MESSAGE_TYPE,
    LND_CUSTOM_MESSAGE_MAX_DATA_SIZE,
    LndBackend,
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
        self.stage = LndEscrowStage.LOCKED

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
            funding_script=bytes.fromhex("5120" + "55" * 32),
            local_funding_pubkey=bytes.fromhex("02" + "66" * 32),
            remote_funding_pubkey=bytes.fromhex("03" + "77" * 32),
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
        return escrow.BeginSigningResponse(
            attempt_id=b"a" * 32,
            local_public_nonce=b"n" * 66,
        )

    async def SignParent(  # noqa: N802
        self, request: escrow.SignParentRequest
    ) -> escrow.SignParentResponse:
        self.sign_request = request
        self.stage = LndEscrowStage.PARENT_SIGNED
        return escrow.SignParentResponse(local_partial_signature=b"s" * 32)

    async def FinalizeParent(  # noqa: N802
        self, request: escrow.FinalizeParentRequest
    ) -> escrow.FinalizeParentResponse:
        self.finalize_request = request
        self.stage = LndEscrowStage.FINALIZED
        return escrow.FinalizeParentResponse(raw_tx=self.parent_tx, txid=self.parent_txid)

    async def Status(self, request: escrow.StatusRequest) -> escrow.StatusResponse:  # noqa: N802
        self.status_request = request
        return escrow.StatusResponse(
            stage=self.stage,
            parent_txid=self.parent_txid,
            finalized=self.stage is LndEscrowStage.FINALIZED,
        )

    async def Cancel(self, request: escrow.CancelRequest) -> escrow.CancelResponse:  # noqa: N802
        self.cancel_request = request
        return escrow.CancelResponse(link_resumed=True)


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


async def test_patched_lnd_channel_input_signing_lifecycle() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    escrow_stub = _FakeEscrowStub()
    backend._escrow_stub = escrow_stub
    channel_txid = "12" * 32
    channel_point = f"{channel_txid}:3"
    session_id = "maker-private-session"

    snapshot = await backend.freeze_channel_input(session_id, channel_point)
    assert snapshot.funding_outpoint.txid == channel_txid
    assert snapshot.funding_outpoint.vout == 3
    assert snapshot.maker_claim_sats == 1_200_000
    assert snapshot.peer_claim_sats == 800_000
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

    finalized = await backend.finalize_channel_input_parent(
        session_id, snapshot.funding_outpoint, attempt, b"p" * 32
    )
    assert finalized.txid == parent_txid
    assert finalized.tx_hex == parent_tx_hex
    assert escrow_stub.finalize_request is not None
    assert escrow_stub.finalize_request.remote_partial_signature == b"p" * 32

    status = await backend.channel_input_escrow_status(session_id, snapshot.funding_outpoint)
    assert status.stage is LndEscrowStage.FINALIZED
    assert status.parent_txid == parent_txid
    assert status.finalized is True
    assert await backend.cancel_channel_input_escrow(session_id, snapshot.funding_outpoint) is True
    assert escrow_stub.cancel_request is not None


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
