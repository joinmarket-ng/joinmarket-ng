"""Tests for applying signed market faults to ordinary Taker orderbooks."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from bitcointx.core.key import CKey
from jmcore.credential_market import (
    Allocation,
    BondReference,
    Delivery,
    FaultProof,
    MarketAuthorization,
    bond_resource,
    canonical,
    document_hash,
    sign_document,
)
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.models import NetworkType, Offer, OfferType

from taker.config import TakerConfig
from taker.taker import Taker


def _key(byte: int) -> CKey:
    return CKey(bytes([byte]) * 32)


def _pubkey(key: CKey) -> str:
    return bytes(key.pub).hex()


def _proof() -> tuple[bytes, BondReference]:
    owner = _key(0x51)
    seller = _key(0x52)
    certificate = _key(0x53)
    bond = BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid="ab" * 32, vout=2),
        pubkey=_pubkey(owner),
        locktime=600_000_000,
    )
    authorization = sign_document(
        MarketAuthorization(bond=bond, period=0, seller_pubkey=_pubkey(seller)), owner
    )
    allocation = sign_document(
        Allocation(
            authorization=document_hash(authorization.body),
            allocation_id="04" * 32,
            buyer_tag="05" * 32,
            product="bond",
            resource=bond_resource(bond, 0),
            certificate_pubkey=_pubkey(certificate),
        ),
        seller,
    )
    delivery = sign_document(
        Delivery(allocation=document_hash(allocation.body), credential={"version": 1}), seller
    )
    return canonical(
        FaultProof(
            reason="invalid-delivery",
            authorization=authorization,
            first=allocation,
            second=delivery,
        )
    ), bond


def _offer(bond: BondReference, nick: str) -> Offer:
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
        fidelity_bond_data={
            "utxo_txid": bond.outpoint.txid,
            "utxo_vout": bond.outpoint.vout,
            "utxo_pub": bond.pubkey,
            "locktime": bond.locktime,
            "cert_expiry": 2016,
        },
    )


class _FaultDirectory:
    def __init__(self, proofs: list[bytes]) -> None:
        self._proofs = proofs

    def drain_market_faults(self) -> list[bytes]:
        proofs = self._proofs
        self._proofs = []
        return proofs


def test_disabled_market_trading_still_hard_excludes_matching_offers(tmp_path) -> None:
    wallet = AsyncMock()
    wallet.mixdepth_count = 5
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=False)
    config = TakerConfig(
        mnemonic="abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about",
        network=NetworkType.REGTEST,
        directory_servers=["localhost:5222"],
        data_dir=tmp_path,
        external_podle_mode="disabled",
    )
    taker = Taker(wallet, backend, config)
    raw, bond = _proof()
    taker.directory_client.clients = {"localhost:5222": _FaultDirectory([raw])}  # type: ignore[assignment]
    matching_one = _offer(bond, "matching-one")
    matching_two = _offer(bond, "matching-two")
    untainted = _offer(bond, "untainted")
    assert untainted.fidelity_bond_data is not None
    untainted.fidelity_bond_data["utxo_pub"] = _pubkey(_key(0x54))

    filtered = taker._drain_and_apply_market_faults(
        [matching_one, matching_two, untainted], current_block_height=2016
    )
    taker.orderbook_manager.update_offers(filtered)

    assert [offer.counterparty for offer in filtered] == ["untainted"]
    selected, _fee = taker.orderbook_manager.select_makers(cj_amount=1, n=3)
    assert set(selected) == {"untainted"}
