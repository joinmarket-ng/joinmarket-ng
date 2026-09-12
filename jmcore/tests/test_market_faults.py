"""Tests for bounded signed market-fault evidence handling."""

from __future__ import annotations

import base64
import json
import stat

import pytest
from bitcointx.core.key import CKey

from jmcore.credential_market import (
    MAX_MARKET_BYTES,
    Allocation,
    BondReference,
    Delivery,
    FaultProof,
    MarketAuthorization,
    MarketError,
    bond_resource,
    canonical,
    document_hash,
    sign_document,
)
from jmcore.crypto import NickIdentity
from jmcore.directory_client import DirectoryClient
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_faults import MarketFaultCache
from jmcore.models import Offer, OfferType
from jmcore.protocol import JM_VERSION, MessageType


def _key(byte: int) -> CKey:
    return CKey(bytes([byte]) * 32)


def _pubkey(key: CKey) -> str:
    return bytes(key.pub).hex()


def _fault_proof(*, period: int = 0) -> tuple[FaultProof, bytes, BondReference]:
    owner = _key(0x11)
    seller = _key(0x22)
    certificate = _key(0x33)
    bond = BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid="aa" * 32, vout=7),
        pubkey=_pubkey(owner),
        locktime=600_000_000,
    )
    authorization = sign_document(
        MarketAuthorization(bond=bond, period=period, seller_pubkey=_pubkey(seller)), owner
    )
    allocation = sign_document(
        Allocation(
            authorization=document_hash(authorization.body),
            allocation_id="01" * 32,
            buyer_tag="02" * 32,
            product="bond",
            resource=bond_resource(bond, period),
            certificate_pubkey=_pubkey(certificate),
        ),
        seller,
    )
    delivery = sign_document(
        Delivery(allocation=document_hash(allocation.body), credential={"version": 1}), seller
    )
    proof = FaultProof(
        reason="invalid-delivery",
        authorization=authorization,
        first=allocation,
        second=delivery,
    )
    return proof, canonical(proof), bond


def _offer(bond: BondReference, nick: str, **changes: object) -> Offer:
    bond_data: dict[str, object] = {
        "utxo_txid": bond.outpoint.txid,
        "utxo_vout": bond.outpoint.vout,
        "utxo_pub": bond.pubkey,
        "locktime": bond.locktime,
        "cert_expiry": 2016,
    }
    bond_data.update(changes)
    return Offer(
        counterparty=nick,
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=1,
        maxsize=1_000_000,
        txfee=0,
        cjfee=0,
        fidelity_bond_value=0,
        fidelity_bond_verified=True,
        fidelity_bond_data=bond_data,
    )


def test_matching_verified_bond_hard_excludes_every_nick_and_persists(tmp_path) -> None:
    _proof, raw, bond = _fault_proof()
    cache = MarketFaultCache(tmp_path)
    offers = [_offer(bond, "maker-one"), _offer(bond, "maker-two")]

    assert cache.ingest(raw)
    assert not (tmp_path / "market_faults.json").exists()
    assert cache.excluded_nicks(offers, network="regtest", height=2016) == {
        "maker-one",
        "maker-two",
    }

    persisted = tmp_path / "market_faults.json"
    assert persisted.exists()
    assert stat.S_IMODE(persisted.stat().st_mode) == 0o600

    restored = MarketFaultCache(tmp_path)
    assert restored.excluded_nicks(offers, network="regtest", height=2016) == {
        "maker-one",
        "maker-two",
    }


@pytest.mark.parametrize(
    ("changes", "network"),
    [
        ({"utxo_txid": "bb" * 32}, "regtest"),
        ({"utxo_pub": _pubkey(_key(0x44))}, "regtest"),
        ({"locktime": 600_000_001}, "regtest"),
        ({}, "testnet"),
    ],
)
def test_wrong_bond_metadata_or_network_cannot_frame(
    changes: dict[str, object], network: str
) -> None:
    _proof, raw, bond = _fault_proof()
    cache = MarketFaultCache()

    assert cache.ingest(raw)
    assert (
        cache.excluded_nicks([_offer(bond, "untouched", **changes)], network=network, height=1)
        == set()
    )


def test_current_and_next_period_are_excluded_and_reorg_restores_match() -> None:
    _proof, raw, bond = _fault_proof(period=0)
    cache = MarketFaultCache()
    offer = _offer(bond, "maker")

    assert cache.ingest(raw)
    assert cache.excluded_nicks([offer], network="regtest", height=1) == {"maker"}
    assert cache.excluded_nicks([offer], network="regtest", height=2016) == {"maker"}
    assert cache.excluded_nicks([offer], network="regtest", height=2017) == {"maker"}
    assert cache.excluded_nicks([offer], network="regtest", height=4032) == {"maker"}
    assert cache.excluded_nicks([offer], network="regtest", height=4033) == set()
    assert cache.excluded_nicks([offer], network="regtest", height=2016) == {"maker"}


def test_unverified_zero_value_offer_is_not_tainted() -> None:
    _proof, raw, bond = _fault_proof()
    cache = MarketFaultCache()
    offer = _offer(bond, "unverified")
    offer.fidelity_bond_verified = None

    assert cache.ingest(raw)
    assert cache.excluded_nicks([offer], network="regtest", height=1) == set()


def test_unbacked_claims_cannot_evict_promoted_faults(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("jmcore.market_faults._MAX_ENTRIES", 1)
    _proof, raw, bond = _fault_proof(period=0)
    cache = MarketFaultCache(tmp_path)
    assert cache.ingest(raw)
    assert cache.excludes_verified_bond(bond, height=1)
    _other, unrelated_raw, _bond = _fault_proof(period=3)
    assert not cache.ingest(unrelated_raw)
    assert cache.excludes_verified_bond(bond, height=2017)
    assert not cache.excludes_verified_bond(bond, height=4033)
    assert MarketFaultCache(tmp_path).excludes_verified_bond(bond, height=1)


def test_malformed_oversized_and_signature_floods_are_bounded(monkeypatch) -> None:
    proof, raw, _bond = _fault_proof()
    cache = MarketFaultCache()

    assert not cache.ingest(b'{"proof":1,"proof":2}')
    assert not cache.ingest(b"x" * (MAX_MARKET_BYTES + 1))

    calls = 0

    def fail_verify(_proof: FaultProof):
        nonlocal calls
        calls += 1
        raise MarketError("invalid signature")

    monkeypatch.setattr(FaultProof, "verify", fail_verify)
    for value in range(16):
        document = json.loads(raw)
        signature = document["authorization"]["signature"]
        document["authorization"]["signature"] = signature[:-2] + f"{value:02x}"
        assert not cache.ingest(canonical(document))

    assert calls == 8
    assert len(cache) == 0


def test_directory_client_captures_only_bounded_exact_public_mproofs() -> None:
    client = DirectoryClient(host="localhost", port=5222, network="regtest")
    sender = NickIdentity(JM_VERSION).nick
    raw = b'{"kind":"fault"}'
    encoded = base64.b64encode(raw).decode("ascii")

    client._capture_market_fault(
        {"type": MessageType.PUBMSG.value, "line": f"{sender}!PUBLIC!mproof {encoded}"}
    )
    client._capture_market_fault(
        {"type": MessageType.PUBMSG.value, "line": f"{sender}!PUBLIC!mproof {encoded} extra"}
    )
    client._capture_market_fault(
        {"type": MessageType.PRIVMSG.value, "line": f"{sender}!PUBLIC!mproof {encoded}"}
    )
    assert client.drain_market_faults() == [raw]
    assert client.drain_market_faults() == []

    for value in range(80):
        item = base64.b64encode(f"proof-{value}".encode("ascii")).decode("ascii")
        client._capture_market_fault(
            {"type": MessageType.PUBMSG.value, "line": f"{sender}!PUBLIC!mproof {item}"}
        )
    assert len(client.drain_market_faults()) == 64
