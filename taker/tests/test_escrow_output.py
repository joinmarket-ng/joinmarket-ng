"""Focused tests for taker parsing of escrow !ioauth output tokens."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from jmcore.bitcoin import create_p2tr_scriptpubkey, scriptpubkey_to_address
from jmcore.models import NetworkType, Offer, OfferType

from taker.coinjoin_session import CoinJoinSession
from taker.models import MakerSession

if TYPE_CHECKING:
    from jmcore.encryption import CryptoSession

    from taker.taker import Taker


@dataclass
class FakeConfig:
    """Minimal configuration used by !ioauth optional-field parsing."""

    network: NetworkType = NetworkType.REGTEST
    maker_timeout_sec: float = 1.0
    minimum_makers: int = 1


@dataclass
class FakeTaker:
    """Minimal owner used to attach a CoinJoin session."""

    config: FakeConfig


def _p2tr_address(byte: int, network: str = "regtest") -> str:
    return scriptpubkey_to_address(create_p2tr_scriptpubkey(bytes([byte]) * 32), network)


def _session() -> CoinJoinSession:
    session = CoinJoinSession()
    session.attach(cast("Taker", FakeTaker(config=FakeConfig())))
    return session


def _maker_session() -> MakerSession:
    return MakerSession(
        nick="J5Maker",
        offer=Offer(
            counterparty="J5Maker",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=100_000,
            maxsize=1_000_000,
            txfee=0,
            cjfee=0,
        ),
    )


def test_parses_escrow_token_after_legacy_optional_fields() -> None:
    session = _session()
    maker = _maker_session()
    address = _p2tr_address(0x11)

    session._parse_ioauth_optional_fields(
        maker,
        ["02" + "22" * 32, "03" + "33" * 32 + "@maker.onion:9735", f"escrow:250000:{address}"],
    )

    assert maker.cc_funding_pubkey == "02" + "22" * 32
    assert maker.ln_node == "03" + "33" * 32 + "@maker.onion:9735"
    assert maker.escrow_output_address == address
    assert maker.escrow_output_value == 250_000


@pytest.mark.parametrize(
    "token",
    [
        "escrow:not-a-number:bcrt1pqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0l98cr",
        "escrow:0:bcrt1pqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0l98cr",
        "escrow:1000",
    ],
)
def test_rejects_malformed_escrow_tokens(token: str) -> None:
    session = _session()

    with pytest.raises(ValueError, match="escrow"):
        session._parse_ioauth_optional_fields(_maker_session(), [token])


def test_rejects_duplicate_escrow_tokens() -> None:
    session = _session()
    address = _p2tr_address(0x22)

    with pytest.raises(ValueError, match="duplicate escrow"):
        session._parse_ioauth_optional_fields(
            _maker_session(), [f"escrow:1000:{address}", f"escrow:2000:{address}"]
        )


def test_rejects_non_p2tr_escrow_address() -> None:
    session = _session()
    p2wpkh_address = scriptpubkey_to_address(b"\x00\x14" + b"\x44" * 20, "regtest")

    with pytest.raises(ValueError, match="P2TR"):
        session._parse_ioauth_optional_fields(_maker_session(), [f"escrow:1000:{p2wpkh_address}"])


def test_rejects_escrow_address_for_another_network() -> None:
    session = _session()

    with pytest.raises(ValueError, match="wrong network"):
        session._parse_ioauth_optional_fields(
            _maker_session(), [f"escrow:1000:{_p2tr_address(0x55, 'testnet')}"]
        )


@pytest.mark.asyncio
async def test_phase_auth_drops_maker_with_malformed_escrow_token() -> None:
    class FakeCrypto:
        def encrypt(self, plaintext: str) -> str:
            return plaintext

        def decrypt(self, ciphertext: str) -> str:
            return (
                f"{'a' * 64}:0 02{'11' * 32} cj-address change-address "
                f"btc-signature escrow:0:{_p2tr_address(0x66)}"
            )

    class FakeBackend:
        def requires_neutrino_metadata(self) -> bool:
            return False

    class FakeDirectory:
        def upgrade_channel_prefer_direct(self, nick: str, channel: str) -> str:
            return channel

        async def send_privmsg(self, *args: object, **kwargs: object) -> None:
            return None

        async def wait_for_responses(
            self, *args: object, **kwargs: object
        ) -> dict[str, dict[str, str]]:
            return {"J5Maker": {"data": "encrypted-response"}}

    session = CoinJoinSession()
    session.attach(
        cast(
            "Taker",
            SimpleNamespace(
                config=FakeConfig(), backend=FakeBackend(), directory_client=FakeDirectory()
            ),
        )
    )
    session.podle_commitment = SimpleNamespace(
        has_neutrino_metadata=lambda: False,
        to_revelation=lambda extended: {"utxo": "", "P": "", "P2": "", "sig": "", "e": ""},
    )
    maker = _maker_session()
    maker.pubkey = "22" * 32
    maker.crypto = cast("CryptoSession", FakeCrypto())
    session.maker_sessions = {maker.nick: maker}

    with patch("jmcore.crypto.ecdsa_verify", return_value=True):
        result = await session._phase_auth()

    assert not result.success
    assert result.failed_makers == [maker.nick]
    assert session.maker_sessions == {}
