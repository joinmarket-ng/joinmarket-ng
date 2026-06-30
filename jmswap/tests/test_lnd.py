"""Unit tests for the LND taproot channel-as-change backend (no live node).

The gRPC stub is faked, so these assert the message construction and PSBT shaping
that the live nodes validated: SIMPLE_TAPROOT + private + PSBT shim on open, a
witness-UTXO-bearing unsigned PSBT, and PsbtVerify-then-PsbtFinalize on completion.
"""

from __future__ import annotations

import pytest
from jmcore.bitcoin import TxInput, TxOutput, scriptpubkey_to_address, serialize_transaction

from jmswap.lnd import LndBackend, unsigned_psbt_from_tx
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


class _FakeStub:
    def __init__(self) -> None:
        self.open_request: ln.OpenChannelRequest | None = None
        self.funding_steps: list[ln.FundingTransitionMsg] = []

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


async def test_open_uses_simple_taproot_private_psbt_shim() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    node = "02" + "ab" * 32
    ucid = await backend.open_channel_external_funding(node, "127.0.0.1:9836", 1_500_000)

    req = backend._stub.open_request
    assert req is not None
    assert req.commitment_type == ln.CommitmentType.SIMPLE_TAPROOT
    assert req.private is True
    assert req.local_funding_amount == 1_500_000
    assert req.funding_shim.psbt_shim.no_publish is True
    assert req.funding_shim.psbt_shim.pending_chan_id == bytes.fromhex(ucid)

    ready, spk_hex, value = await backend.get_funding_output(ucid)
    assert ready is True
    assert value == 1_500_000
    assert spk_hex  # address converted to a scriptpubkey


async def test_provide_funding_outpoint_verifies_then_finalizes() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    ucid = await backend.open_channel_external_funding(
        "02" + "ab" * 32, "127.0.0.1:9836", 1_500_000
    )
    tx_hex, iu = _simple_tx_hex()

    await backend.provide_funding_outpoint(ucid, "ab" * 32, 0, tx_hex, iu)

    steps = backend._stub.funding_steps
    assert [s.WhichOneof("trigger") for s in steps] == ["psbt_verify", "psbt_finalize"]
    pending = bytes.fromhex(ucid)
    assert steps[0].psbt_verify.pending_chan_id == pending
    assert steps[0].psbt_verify.funded_psbt.startswith(_PSBT_MAGIC)
    assert steps[1].psbt_finalize.pending_chan_id == pending
    assert steps[1].psbt_finalize.final_raw_tx == bytes.fromhex(tx_hex)


async def test_provide_funding_outpoint_requires_tx_and_utxos() -> None:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _FakeStub()
    ucid = await backend.open_channel_external_funding(
        "02" + "ab" * 32, "127.0.0.1:9836", 1_500_000
    )
    with pytest.raises(ValueError, match="raw_tx_hex"):
        await backend.provide_funding_outpoint(ucid, "ab" * 32, 0)
