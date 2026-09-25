"""Renter-side reporting of an owner who also runs the bond he leased out.

The CLI observes the ordinary maker orderbook, exactly like any watcher, and
turns a contradicting offer into signed evidence.  These tests use real owner
and renter signatures and real fidelity bond proofs; only the directory
transport and the chain backend are replaced.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bitcointx.core.key import CKey  # type: ignore[import-not-found]
from jmcore.credential_market import (
    Allocation,
    BondCredential,
    BondReference,
    CredentialPackage,
    Delivery,
    FaultProof,
    MarketAuthorization,
    bond_resource,
    canonical,
    document_hash,
    sign_bond_lease,
    sign_document,
)
from jmcore.crypto import bitcoin_message_hash_bytes, get_cert_msg
from jmcore.directory_client import parse_fidelity_bond_proof
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_faults import MarketFaultCache
from jmcore.models import NetworkType, Offer, OfferType
from jmcore.settings import JoinMarketSettings, NetworkSettings

from taker import market_cli

PERIOD = 17
RENTED_EXPIRY = PERIOD + 1
MAKER_NICK = "J54Ac1oFFRcTLgUB"
RIVAL_EXPIRY_PERIODS = 40


def _key(value: int) -> CKey:
    return CKey(bytes([value]) * 32)


def _pub(key: CKey) -> str:
    return bytes(key.pub).hex()


OWNER = _key(0x11)
SELLER = _key(0x22)
RENTER = _key(0x33)
RIVAL = _key(0x44)
OTHER_RENTER = _key(0x55)
BOND = BondReference(
    network="regtest",
    outpoint=ExternalPoDLEOutpoint(txid="aa" * 32, vout=7),
    pubkey=_pub(OWNER),
    locktime=0xFFFFFFFF,
)


def _height(period: int) -> int:
    return period * 2016 + 1


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _certificate(owner: CKey, cert_key: CKey, expiry: int) -> bytes:
    return owner.sign(
        bitcoin_message_hash_bytes(get_cert_msg(bytes(cert_key.pub), expiry)),
        _ecdsa_sig_grind_low_r=False,
    )


def _credential(cert_key: CKey = RENTER, expiry: int = RENTED_EXPIRY) -> BondCredential:
    return BondCredential(
        bond=BOND,
        cert_pubkey=_pub(cert_key),
        cert_expiry=expiry,
        cert_signature=_certificate(OWNER, cert_key, expiry).hex(),
        lease=sign_bond_lease(BOND, expiry - 1, _pub(cert_key), OWNER),
    )


def _package(cert_key: CKey = RENTER) -> CredentialPackage:
    authorization = sign_document(
        MarketAuthorization(bond=BOND, period=PERIOD, seller_pubkey=_pub(SELLER)), OWNER
    )
    allocation = sign_document(
        Allocation(
            authorization=document_hash(authorization.body),
            allocation_id="01" * 32,
            buyer_tag="02" * 32,
            product="bond",
            resource=bond_resource(BOND, PERIOD),
            certificate_pubkey=_pub(cert_key),
        ),
        SELLER,
    )
    delivery = sign_document(
        Delivery(
            allocation=document_hash(allocation.body),
            credential=_credential(cert_key).model_dump(mode="json"),
        ),
        SELLER,
    )
    return CredentialPackage(authorization=authorization, allocation=allocation, delivery=delivery)


def _ordinary_proof(cert_key: CKey, expiry: int, *, nick: str = MAKER_NICK) -> str:
    """Build the fidelity bond proof a maker publishes with an ordinary offer."""
    cert_sig = _certificate(OWNER, cert_key, expiry).rjust(72, b"\xff")
    nick_sig = cert_key.sign(
        bitcoin_message_hash_bytes((nick + "|" + nick).encode("ascii")),
        _ecdsa_sig_grind_low_r=False,
    ).rjust(72, b"\xff")
    packed = struct.pack(
        "<72s72s33sH33s32sII",
        nick_sig,
        cert_sig,
        bytes(cert_key.pub),
        expiry,
        bytes.fromhex(BOND.pubkey),
        bytes.fromhex(BOND.outpoint.txid),
        BOND.outpoint.vout,
        BOND.locktime,
    )
    return base64.b64encode(packed).decode("ascii")


def _offer(
    cert_key: CKey = RIVAL, expiry: int = RIVAL_EXPIRY_PERIODS, *, nick: str = MAKER_NICK
) -> Offer:
    """One public orderbook offer, parsed exactly as the directory client does."""
    bond_data = parse_fidelity_bond_proof(_ordinary_proof(cert_key, expiry, nick=nick), nick, nick)
    assert bond_data is not None
    return Offer(
        counterparty=nick,
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=1,
        maxsize=1_000_000,
        txfee=0,
        cjfee=0,
        fidelity_bond_value=0,
        fidelity_bond_data=bond_data,
    )


class _Backend:
    """A chain backend that confirms the leased bond at one fixed height."""

    def __init__(self, height: int) -> None:
        self.height = height
        self.heights_read = 0

    async def get_block_height(self) -> int:
        self.heights_read += 1
        return self.height

    async def verify_bonds(self, bonds: list[Any]) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                valid=True, txid=bond.txid, vout=bond.vout, confirmations=6, value=1_000_000
            )
            for bond in bonds
        ]

    async def close(self) -> None:
        return None


class _Orderbook:
    """A directory client that can only be connected, read, and closed."""

    def __init__(self, offers: list[Offer], *, fail: bool = False) -> None:
        self.offers = offers
        self.fail = fail
        self.calls: list[str] = []
        self.timeouts: list[float] = []

    async def connect_all(self) -> int:
        self.calls.append("connect_all")
        return 1

    async def fetch_orderbook(
        self, max_wait: float, min_wait: float, quiet_period: float
    ) -> list[Offer]:
        self.calls.append("fetch_orderbook")
        self.timeouts.append(max_wait)
        if self.fail:
            raise TimeoutError("directory unavailable")
        return list(self.offers)

    async def close_all(self) -> None:
        self.calls.append("close_all")


def _settings(data_dir: Path) -> JoinMarketSettings:
    return JoinMarketSettings(
        data_dir=data_dir,
        network_config=NetworkSettings(network=NetworkType.REGTEST, directory_servers=[]),
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    offers: list[Offer] | None = None,
    height: int = _height(PERIOD),
    fail: bool = False,
) -> tuple[_Orderbook, _Backend]:
    """Wire the CLI to a fake directory and chain, and forbid any publication."""
    orderbook = _Orderbook(offers if offers is not None else [], fail=fail)
    backend = _Backend(height)
    monkeypatch.setattr(market_cli, "_settings", lambda _args: _settings(tmp_path))
    monkeypatch.setattr(market_cli, "_new_orderbook_client", lambda _settings: orderbook)
    monkeypatch.setattr(market_cli, "_new_backend", lambda _settings: backend)
    monkeypatch.setattr(
        market_cli,
        "_new_transport",
        lambda *_args, **_kwargs: pytest.fail("observation must not open a publishing transport"),
    )
    return orderbook, backend


def _rental(tmp_path: Path, *, cert_key: CKey = RENTER, bundle_key: CKey | None = None) -> Path:
    """Write the delivered package and the per-request key bundle beside it."""
    request_file = tmp_path / "rent.json"
    _write(request_file.with_name("rent.json.delivery"), canonical(_package(cert_key)))
    _write(
        request_file.with_name("rent.json.keys"),
        canonical(
            {
                "version": 1,
                "seller_signing_key": _pub(SELLER)[2:],
                "encryption_key": "11" * 32,
                "renter_certificate_key": (bundle_key or cert_key).secret_bytes.hex(),
            }
        ),
    )
    return request_file


def _observe(tmp_path: Path, *extra: str) -> int:
    return market_cli.run(
        [
            "proof",
            "observe",
            "--package",
            str(tmp_path / "rent.json.delivery"),
            "--request-file",
            str(tmp_path / "rent.json"),
            "--output",
            str(tmp_path / "conflict.json"),
            *extra,
        ]
    )


def test_observed_conflict_becomes_signed_evidence_and_a_local_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    orderbook, backend = _install(monkeypatch, tmp_path, offers=[_offer()])
    _rental(tmp_path)

    assert _observe(tmp_path) == 0

    output = tmp_path / "conflict.json"
    assert output.stat().st_mode & 0o777 == 0o600
    proof = FaultProof.model_validate(json.loads(output.read_text(encoding="ascii")))
    authority, conflict = proof.verify_detailed()
    assert proof.reason == "conflicting-bond-certificate"
    assert authority.bond == BOND and authority.period == PERIOD
    assert conflict is not None and conflict.cert_pubkey == _pub(RIVAL)
    assert conflict.maker_nick == MAKER_NICK

    # The orderbook was only read, and the leased bond was verified afterwards.
    assert orderbook.calls == ["connect_all", "fetch_orderbook", "close_all"]
    assert backend.heights_read >= 1
    assert json.loads(capsys.readouterr().out) == {"conflict": True, "excluded_makers": 1}

    # The observation is durable: a fresh cache still sanctions the bond.
    restored = MarketFaultCache(tmp_path)
    assert restored.excludes_verified_bond(BOND, height=_height(PERIOD))
    assert restored.excluded_nicks(
        [_offer().model_copy(update={"fidelity_bond_verified": True})],
        network="regtest",
        height=_height(PERIOD),
    ) == {MAKER_NICK}


def test_renewing_the_rented_key_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner may keep serving the key he leased us; that is the deal."""
    orderbook, _backend = _install(
        monkeypatch, tmp_path, offers=[_offer(cert_key=RENTER, expiry=60)]
    )
    _rental(tmp_path)

    assert _observe(tmp_path) == 1
    assert orderbook.calls == ["connect_all", "fetch_orderbook", "close_all"]
    assert not (tmp_path / "conflict.json").exists()
    assert not (tmp_path / "market_faults.json").exists()


def test_an_unrelated_bond_on_the_orderbook_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _offer()
    data = dict(other.fidelity_bond_data or {})
    data["utxo_txid"] = "bb" * 32
    _install(monkeypatch, tmp_path, offers=[other.model_copy(update={"fidelity_bond_data": data})])
    _rental(tmp_path)

    assert _observe(tmp_path) == 1
    assert not (tmp_path / "conflict.json").exists()


def test_a_renter_without_the_leased_certificate_key_never_reaches_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    used: list[str] = []
    _install(monkeypatch, tmp_path, offers=[_offer()])

    def _refuse(_settings: object) -> _Orderbook:
        used.append("connected")
        return _Orderbook([])

    monkeypatch.setattr(market_cli, "_new_orderbook_client", _refuse)
    monkeypatch.setattr(
        market_cli, "_new_backend", lambda _settings: pytest.fail("no chain access is needed")
    )
    _rental(tmp_path, bundle_key=OTHER_RENTER)

    assert _observe(tmp_path) == 1
    assert used == []
    assert not (tmp_path / "conflict.json").exists()


def test_an_ended_period_produces_no_accusation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A certificate has no activation height, so a late sighting proves nothing."""
    orderbook, _backend = _install(
        monkeypatch, tmp_path, offers=[_offer()], height=_height(PERIOD + 1)
    )
    _rental(tmp_path)

    assert _observe(tmp_path) == 1
    assert orderbook.calls == ["connect_all", "fetch_orderbook", "close_all"]
    assert not (tmp_path / "conflict.json").exists()
    assert not (tmp_path / "market_faults.json").exists()


def test_a_failed_orderbook_fetch_still_disconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orderbook, _backend = _install(monkeypatch, tmp_path, offers=[_offer()], fail=True)
    _rental(tmp_path)

    assert _observe(tmp_path) == 1
    assert orderbook.calls == ["connect_all", "fetch_orderbook", "close_all"]
    assert not (tmp_path / "conflict.json").exists()


def test_period_boundary_during_collateral_verification_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, tmp_path, offers=[_offer()])
    _rental(tmp_path)

    async def verify_after_boundary(*_args: object, **_kwargs: object) -> int:
        return _height(PERIOD + 1)

    monkeypatch.setattr(market_cli, "verify_market_bond", verify_after_boundary)
    assert _observe(tmp_path) == 1
    assert not (tmp_path / "conflict.json").exists()
    assert not (tmp_path / "market_faults.json").exists()


@pytest.mark.parametrize("timeout", ["0", "-1", "121", "nan"])
def test_the_observation_timeout_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: str
) -> None:
    orderbook, _backend = _install(monkeypatch, tmp_path, offers=[_offer()])
    _rental(tmp_path)

    assert _observe(tmp_path, "--timeout", timeout) == 1
    assert orderbook.calls == []


def test_an_accepted_timeout_bounds_the_orderbook_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orderbook, _backend = _install(monkeypatch, tmp_path, offers=[_offer()])
    _rental(tmp_path)

    assert _observe(tmp_path, "--timeout", "120") == 0
    assert orderbook.timeouts == [120.0]


def test_the_observing_identity_is_fresh_isolated_and_silent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    first = market_cli._new_orderbook_client(settings)
    second = market_cli._new_orderbook_client(settings)

    assert first.nick != second.nick
    for client in (first, second):
        assert client.stream_isolation is True
        assert client.prefer_direct_connections is False
        assert client.our_location == "NOT-SERVING-ONION"
        assert (client.socks_host, client.socks_port) == (
            settings.tor.socks_host,
            settings.tor.socks_port,
        )


def test_reporting_never_publishes_by_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence stays local until the renter runs proof broadcast explicitly."""
    _install(monkeypatch, tmp_path, offers=[_offer()])
    _rental(tmp_path)
    assert _observe(tmp_path) == 0

    published: list[bytes] = []

    class _Publisher:
        async def start(self) -> None:
            return None

        async def broadcast_fault(self, raw: bytes) -> int:
            published.append(raw)
            return 2

        async def close(self) -> None:
            return None

    monkeypatch.setattr(market_cli, "_new_transport", lambda *_a, **_k: _Publisher())
    assert market_cli.run(["proof", "broadcast", "--proof", str(tmp_path / "conflict.json")]) == 0
    assert len(published) == 1
    # Broadcast re-verifies the new reason without claiming the observation time.
    republished = FaultProof.model_validate(json.loads(published[0].decode("ascii")))
    assert republished.reason == "conflicting-bond-certificate"
    assert MarketFaultCache().ingest(published[0]) is True
    assert (
        MarketFaultCache().excluded_nicks(
            [_offer().model_copy(update={"fidelity_bond_verified": True})],
            network="regtest",
            height=_height(PERIOD + 1),
        )
        == set()
    )


def test_locktime_expiry_is_checked_against_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bond whose timelock already passed is no longer usable collateral."""
    _install(monkeypatch, tmp_path, offers=[_offer()])
    _rental(tmp_path)
    monkeypatch.setattr(market_cli.time, "time", lambda: float(BOND.locktime) + 1)

    assert _observe(tmp_path) == 1
    assert not (tmp_path / "conflict.json").exists()
