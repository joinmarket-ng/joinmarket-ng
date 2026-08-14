"""Focused integration tests for the taker's LND channel-as-change ring plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

import pytest
from jmcore.bitcoin import (
    address_to_scriptpubkey,
    create_p2tr_scriptpubkey,
    parse_transaction,
    scriptpubkey_to_address,
)
from jmcore.encryption import CryptoSession
from jmcore.models import NetworkType, Offer, OfferType

from taker.coinjoin_session import CoinJoinSession
from taker.knapsack import PooledPlan, plan_pooled_knapsack, plan_pooled_session
from taker.models import MakerSession
from taker.tx_builder import build_coinjoin_tx

if TYPE_CHECKING:
    from taker.taker import Taker


CHANNEL_MINIMUM = 200_000
COINJOIN_AMOUNT = 1_000_000


class ReversibleCrypto(CryptoSession):
    """Minimal symmetric stand-in for a maker encryption session."""

    _PREFIX = "encrypted:"

    def __init__(self) -> None:
        """Avoid real key generation because this double is deliberately reversible."""

    def encrypt(self, plaintext: str) -> str:
        return f"{self._PREFIX}{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        assert ciphertext.startswith(self._PREFIX)
        return ciphertext.removeprefix(self._PREFIX)


@dataclass(frozen=True)
class Chanreq:
    """A chanreq send captured by the fake directory."""

    nick: str
    command: str
    payload: str
    log_routing: bool
    force_channel: str


ResponseMode = Literal["valid", "missing", "malformed"]


@dataclass
class FakeDirectory:
    """Records channel requests and supplies deterministic encrypted responses."""

    cryptos: dict[str, ReversibleCrypto]
    channel_addresses: dict[str, str]
    response_mode: ResponseMode = "valid"
    sent: list[Chanreq] = field(default_factory=list)

    async def send_privmsg(
        self,
        nick: str,
        command: str,
        encrypted_payload: str,
        *,
        log_routing: bool,
        force_channel: str,
    ) -> None:
        self.sent.append(
            Chanreq(
                nick=nick,
                command=command,
                payload=self.cryptos[nick].decrypt(encrypted_payload),
                log_routing=log_routing,
                force_channel=force_channel,
            )
        )

    async def wait_for_responses(
        self,
        *,
        expected_nicks: list[str],
        expected_command: str,
        timeout: float,
    ) -> dict[str, dict[str, str]]:
        assert expected_command == "!chanaddr"
        assert timeout == 12.0
        if self.response_mode == "missing":
            return {}

        responses = {
            nick: {"data": self.cryptos[nick].encrypt(self.channel_addresses[nick])}
            for nick in expected_nicks
        }
        if self.response_mode == "malformed":
            first_funder = expected_nicks[0]
            responses[first_funder] = {"data": self.cryptos[first_funder].encrypt("")}
        return responses


@dataclass(frozen=True)
class FakeConfig:
    """Only the session configuration consumed by _plan_lnd_ring."""

    network: NetworkType = NetworkType.REGTEST
    ring_min_channel_sats: int = CHANNEL_MINIMUM
    maker_timeout_sec: float = 12.0


@dataclass
class FakeTaker:
    """Minimal dependency owner accepted by CoinJoinSession.attach."""

    config: FakeConfig
    directory_client: FakeDirectory


def _address(byte: int) -> str:
    return scriptpubkey_to_address(create_p2tr_scriptpubkey(bytes([byte]) * 32), "regtest")


def _offer(nick: str) -> Offer:
    return Offer(
        counterparty=nick,
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=COINJOIN_AMOUNT,
        maxsize=COINJOIN_AMOUNT,
        txfee=0,
        cjfee=1_000,
    )


def _maker_session(nick: str, value: int, address_byte: int) -> MakerSession:
    return MakerSession(
        nick=nick,
        offer=_offer(nick),
        utxos=[{"txid": f"{address_byte:02x}" * 32, "vout": 0, "value": value}],
        cj_address=_address(address_byte),
        change_address=_address(address_byte + 1),
        crypto=ReversibleCrypto(),
        ln_node=f"02{address_byte:02x}@maker-{address_byte}.onion:9735",
        comm_channel=f"directory:node-{address_byte}:5222",
    )


def _changes(session: CoinJoinSession) -> dict[str, int]:
    return {
        nick: sum(int(utxo["value"]) for utxo in maker.utxos)
        - session.cj_amount
        - maker.offer.txfee
        + int(maker.offer.cjfee)
        for nick, maker in session.maker_sessions.items()
    }


def _maker_data(session: CoinJoinSession) -> dict[str, dict[str, object]]:
    return {
        nick: {
            "utxos": maker.utxos,
            "cj_addr": maker.cj_address,
            "change_addr": maker.change_address,
            "cjfee": int(maker.offer.cjfee),
            "txfee": maker.offer.txfee,
        }
        for nick, maker in session.maker_sessions.items()
    }


def _session(
    response_mode: ResponseMode = "valid",
) -> tuple[CoinJoinSession, FakeDirectory, PooledPlan]:
    makers = {
        "maker-c": _maker_session("maker-c", 1_451_000, 0x13),
        "maker-a": _maker_session("maker-a", 1_501_000, 0x11),
        "maker-b": _maker_session("maker-b", 1_401_000, 0x12),
    }
    changes = {
        nick: value - COINJOIN_AMOUNT + 1_000
        for nick, value in (("maker-c", 1_451_000), ("maker-a", 1_501_000), ("maker-b", 1_401_000))
    }
    plan = plan_pooled_knapsack(list(changes.items()), min_channel=CHANNEL_MINIMUM)
    addresses = {
        channel.party_a: _address(0x30 + index) for index, channel in enumerate(plan.channels)
    }
    cryptos = {nick: cast(ReversibleCrypto, maker.crypto) for nick, maker in makers.items()}
    directory = FakeDirectory(cryptos, addresses, response_mode=response_mode)
    session = CoinJoinSession()
    session.attach(cast("Taker", FakeTaker(config=FakeConfig(), directory_client=directory)))
    session.cj_amount = COINJOIN_AMOUNT
    session.maker_sessions = makers
    session._merge_change = False
    session._ring_relay = {}
    return session, directory, plan


@pytest.mark.asyncio
async def test_plan_lnd_ring_requests_channels_and_builds_pooled_outputs() -> None:
    """The LND ring requests each planned edge and creates its merged funding output."""
    session, directory, plan = _session()
    maker_data = _maker_data(session)
    changes = _changes(session)

    await session._plan_lnd_ring(maker_data)

    expected_requests = [
        Chanreq(
            nick=channel.party_a,
            command="chanreq",
            payload=(
                f"{session.maker_sessions[channel.party_b].ln_node} "
                f"{channel.value} {channel.contrib_b * 1000}"
            ),
            log_routing=True,
            force_channel=session.maker_sessions[channel.party_a].comm_channel,
        )
        for channel in plan.channels
    ]
    assert directory.sent == expected_requests

    expected_ring = plan_pooled_session(
        plan,
        lambda party_a, _party_b: directory.channel_addresses[party_a],
    )
    assert session._merge_change is True
    assert session._ring_relay == expected_ring.change_outputs
    for nick, pieces in session._ring_relay.items():
        assert maker_data[nick]["change_outputs"] == pieces
        assert sum(amount for _address, amount in pieces) == changes[nick]

    tx_bytes, _metadata = build_coinjoin_tx(
        taker_utxos=[{"txid": "f" * 64, "vout": 0, "value": 5_000_000}],
        taker_cj_address=_address(0x01),
        taker_change_address=_address(0x02),
        taker_total_input=5_000_000,
        maker_data=maker_data,
        cj_amount=COINJOIN_AMOUNT,
        tx_fee=3_000,
        network="regtest",
        merge_change_by_address=session._merge_change,
    )
    transaction = parse_transaction(tx_bytes.hex())
    for channel in plan.channels:
        channel_address = directory.channel_addresses[channel.party_a]
        channel_script = address_to_scriptpubkey(channel_address)
        output_values = [
            output.value for output in transaction.outputs if output.script == channel_script
        ]
        assert output_values == [channel.value]


@pytest.mark.asyncio
@pytest.mark.parametrize("response_mode", ["missing", "malformed"])
async def test_plan_lnd_ring_falls_back_without_a_complete_chanaddr_set(
    response_mode: Literal["missing", "malformed"],
) -> None:
    """A missing or unusable chanaddr response leaves ordinary maker change untouched."""
    session, _directory, _plan = _session(response_mode)
    maker_data = _maker_data(session)

    await session._plan_lnd_ring(maker_data)

    assert session._merge_change is False
    assert session._ring_relay == {}
    assert all("change_outputs" not in data for data in maker_data.values())
