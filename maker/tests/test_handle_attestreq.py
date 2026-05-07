"""Unit tests for the maker !attestreq handler.

Drives :class:`ProtocolHandlersMixin._handle_attestreq` against a
minimal stub bot to avoid pulling in the full MakerBot construction
graph. Only the surface that the handler touches is mocked.
"""

from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from coincurve import PrivateKey
from jmcore.attestation_wire import (
    AttestPayload,
    AttestReqPayload,
    decode_attest,
    encode_attestreq,
)
from jmcore.clsag_attestation import RingMember, verify_ring

from maker.attestation_signer import AttestationSigner
from maker.protocol_handlers import ProtocolHandlersMixin


class _StubCrypto:
    """Identity-ish encryption: base64 wrap so encrypted token has no spaces."""

    is_encrypted = True

    def encrypt(self, plaintext: str) -> str:
        return base64.b64encode(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return base64.b64decode(token).decode()


@dataclass
class _StubSession:
    crypto: _StubCrypto

    def validate_channel(self, source: str) -> bool:  # noqa: ARG002
        return True


class _StubDirectoryClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_private_message(self, nick: str, command: str, content: str) -> None:
        self.sent.append((nick, command, content))


def _make_bot(*, with_signer: bool = True, bond_txid: str | None = None) -> Any:
    """Build a stub object exposing exactly the attributes the handler reads."""
    bot = SimpleNamespace()
    bot.active_sessions = {"taker_nick": _StubSession(crypto=_StubCrypto())}
    sk = PrivateKey(b"\x11" * 32)
    bot.attestation_signer = (
        AttestationSigner.from_coincurve_private_key(sk) if with_signer else None
    )
    bond_txid = bond_txid or ("aa" * 32)
    bot.fidelity_bond = SimpleNamespace(txid=bond_txid, vout=0, private_key=sk)
    client = _StubDirectoryClient()
    bot.directory_clients = {"d1": client}
    bot._sent_client = client  # for tests
    return bot


def _random_xonly() -> bytes:
    """Return a valid x-only secp256k1 public key from a random scalar."""
    sk = PrivateKey(os.urandom(32))
    return sk.public_key.format(compressed=True)[1:]  # drop parity byte


def _ring_with_signer_at(idx: int, signer_xonly: bytes, signer_outpoint: bytes) -> list[RingMember]:
    ring = [RingMember(pubkey_xonly=_random_xonly(), outpoint=os.urandom(36)) for _ in range(5)]
    ring[idx] = RingMember(pubkey_xonly=signer_xonly, outpoint=signer_outpoint)
    return ring


def _bond_outpoint(txid_hex: str, vout: int) -> bytes:
    return bytes.fromhex(txid_hex)[::-1] + vout.to_bytes(4, "little")


def _drive(bot: Any, plaintext: str) -> None:
    handler = ProtocolHandlersMixin._handle_attestreq.__get__(bot, type(bot))
    enc = base64.b64encode(plaintext.encode()).decode()
    asyncio.run(handler("taker_nick", f"attestreq {enc}", "direct:taker_nick"))


def _drive_with_send(bot: Any, plaintext: str) -> None:
    """Same as _drive but also bind _send_response from the mixin."""
    bot._send_response = ProtocolHandlersMixin._send_response.__get__(bot, type(bot))  # noqa: SLF001
    _drive(bot, plaintext)


def test_handler_signs_and_replies_with_valid_attest() -> None:
    bot = _make_bot()
    signer_xonly = bot.attestation_signer.pubkey_xonly
    outpoint = _bond_outpoint(bot.fidelity_bond.txid, bot.fidelity_bond.vout)
    ring = _ring_with_signer_at(2, signer_xonly, outpoint)
    run_id = os.urandom(32)
    req = AttestReqPayload(run_id=run_id, round_no=1, signer_idx=2, ring=ring)

    _drive_with_send(bot, encode_attestreq(req))

    assert len(bot._sent_client.sent) == 1  # noqa: SLF001
    nick, cmd, content = bot._sent_client.sent[0]  # noqa: SLF001
    assert nick == "taker_nick"
    assert cmd == "!attest"
    decoded: AttestPayload = decode_attest(
        base64.b64decode(content).decode(), expected_set_size=len(ring)
    )
    assert decoded.run_id == run_id
    assert decoded.round_no == 1
    # Verify the signature embeds a valid CLSAG over the disclosed ring.
    ok, _key_image = verify_ring(signature=decoded.signature, ring=ring, run_id=run_id, round_no=1)
    assert ok


def test_handler_refuses_when_no_signer_available() -> None:
    bot = _make_bot(with_signer=False)
    ring = _ring_with_signer_at(2, os.urandom(32), os.urandom(36))
    req = AttestReqPayload(run_id=os.urandom(32), round_no=0, signer_idx=2, ring=ring)
    _drive_with_send(bot, encode_attestreq(req))
    assert bot._sent_client.sent == []  # noqa: SLF001


def test_handler_refuses_when_signer_idx_points_at_other_maker() -> None:
    bot = _make_bot()
    # signer_idx=2 but slot 2 is some other maker; our slot is unrelated.
    ring = [RingMember(pubkey_xonly=_random_xonly(), outpoint=os.urandom(36)) for _ in range(5)]
    req = AttestReqPayload(run_id=os.urandom(32), round_no=0, signer_idx=2, ring=ring)
    _drive_with_send(bot, encode_attestreq(req))
    assert bot._sent_client.sent == []  # noqa: SLF001


def test_handler_drops_malformed_wire_payload() -> None:
    bot = _make_bot()
    _drive_with_send(bot, "not a valid attestreq")
    assert bot._sent_client.sent == []  # noqa: SLF001


def test_handler_drops_when_no_active_session() -> None:
    bot = _make_bot()
    bot.active_sessions = {}
    signer_xonly = bot.attestation_signer.pubkey_xonly
    outpoint = _bond_outpoint(bot.fidelity_bond.txid, bot.fidelity_bond.vout)
    ring = _ring_with_signer_at(2, signer_xonly, outpoint)
    req = AttestReqPayload(run_id=os.urandom(32), round_no=0, signer_idx=2, ring=ring)
    _drive_with_send(bot, encode_attestreq(req))
    assert bot._sent_client.sent == []  # noqa: SLF001


def test_handler_drops_duplicate_run_round_request() -> None:
    bot = _make_bot()
    signer_xonly = bot.attestation_signer.pubkey_xonly
    outpoint = _bond_outpoint(bot.fidelity_bond.txid, bot.fidelity_bond.vout)
    ring1 = _ring_with_signer_at(2, signer_xonly, outpoint)
    ring2 = _ring_with_signer_at(3, signer_xonly, outpoint)  # different ring, same run/round
    run_id = os.urandom(32)

    req1 = AttestReqPayload(run_id=run_id, round_no=0, signer_idx=2, ring=ring1)
    _drive_with_send(bot, encode_attestreq(req1))
    assert len(bot._sent_client.sent) == 1  # noqa: SLF001

    req2 = AttestReqPayload(run_id=run_id, round_no=0, signer_idx=3, ring=ring2)
    _drive_with_send(bot, encode_attestreq(req2))
    # Second request for the same (run_id, round_no) must be dropped.
    assert len(bot._sent_client.sent) == 1  # noqa: SLF001


@pytest.mark.parametrize("bad_decrypt", ["", "no-prefix"])
def test_handler_drops_undecryptable_input(bad_decrypt: str) -> None:
    bot = _make_bot()
    handler = ProtocolHandlersMixin._handle_attestreq.__get__(bot, type(bot))
    asyncio.run(handler("taker_nick", f"attestreq {bad_decrypt}", "direct:taker_nick"))
    bot._send_response = ProtocolHandlersMixin._send_response.__get__(bot, type(bot))  # noqa: SLF001
    assert bot.directory_clients["d1"].sent == []
