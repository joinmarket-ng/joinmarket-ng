"""Tests for exclusive bond leases and owner-equivocation evidence.

The rented certificate is only worth something if the owner promised not to run
the same bond behind another hot key.  These tests cover the promise itself, the
renter's signed contradiction, and the strict local corroboration that keeps a
late but perfectly valid certificate from becoming a retrospective accusation.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

import pytest
from bitcointx.core.key import CKey
from pydantic import ValidationError

from jmcore.credential_market import (
    Allocation,
    BondCredential,
    BondReference,
    CertificateConflictReport,
    Delivery,
    ExclusiveBondLease,
    FaultProof,
    MarketAuthorization,
    MarketError,
    SignedDocument,
    bond_resource,
    canonical,
    document_hash,
    fault_proof_id,
    sign_bond_lease,
    sign_document,
    validate_credential,
    verify_allocation,
)
from jmcore.crypto import bitcoin_message_hash_bytes, get_cert_msg
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_faults import MarketFaultCache
from jmcore.models import Offer, OfferType

PERIOD = 17
RENTED_EXPIRY = PERIOD + 1
MAKER_NICK = "J54Ac1oFFRcTLgUBAY"[:16]
TAKER_NICK = "J55BdmnGGSdULhVCBZ"[:16]


def _key(byte: int) -> CKey:
    return CKey(bytes([byte]) * 32)


def _pub(key: CKey) -> str:
    return bytes(key.pub).hex()


OWNER = _key(0x11)
SELLER = _key(0x22)
RENTER = _key(0x33)
RIVAL = _key(0x44)
BOND = BondReference(
    network="regtest",
    outpoint=ExternalPoDLEOutpoint(txid="aa" * 32, vout=7),
    pubkey=_pub(OWNER),
    locktime=600_000_000,
)


def _certificate(owner: CKey, cert_key: CKey, expiry: int) -> bytes:
    return owner.sign(
        bitcoin_message_hash_bytes(get_cert_msg(bytes(cert_key.pub), expiry)),
        _ecdsa_sig_grind_low_r=False,
    )


def _credential(
    *,
    owner: CKey = OWNER,
    cert_key: CKey = RENTER,
    expiry: int = RENTED_EXPIRY,
    lease: SignedDocument | None = None,
    bond: BondReference = BOND,
) -> BondCredential:
    if lease is None:
        lease = sign_bond_lease(bond, expiry - 1, _pub(cert_key), owner)
    return BondCredential(
        bond=bond,
        cert_pubkey=_pub(cert_key),
        cert_expiry=expiry,
        cert_signature=_certificate(owner, cert_key, expiry).hex(),
        lease=lease,
    )


def _ordinary_proof(
    *,
    owner: CKey = OWNER,
    cert_key: CKey = RIVAL,
    expiry: int = 40,
    bond: BondReference = BOND,
    maker_nick: str = MAKER_NICK,
    taker_nick: str = TAKER_NICK,
) -> str:
    """Build a normal JoinMarket maker bond proof, unchanged by the market."""
    cert_sig = _certificate(owner, cert_key, expiry).rjust(72, b"\xff")
    nick_sig = cert_key.sign(
        bitcoin_message_hash_bytes((taker_nick + "|" + maker_nick).encode("ascii")),
        _ecdsa_sig_grind_low_r=False,
    ).rjust(72, b"\xff")
    packed = struct.pack(
        "<72s72s33sH33s32sII",
        nick_sig,
        cert_sig,
        bytes(cert_key.pub),
        expiry,
        bytes.fromhex(bond.pubkey),
        bytes.fromhex(bond.outpoint.txid),
        bond.outpoint.vout,
        bond.locktime,
    )
    return base64.b64encode(packed).decode("ascii")


@dataclass(frozen=True)
class Rental:
    authorization: SignedDocument
    allocation: SignedDocument
    credential: BondCredential


def _rental(*, period: int = PERIOD, bond: BondReference = BOND) -> Rental:
    authorization = sign_document(
        MarketAuthorization(bond=bond, period=period, seller_pubkey=_pub(SELLER)), OWNER
    )
    allocation = sign_document(
        Allocation(
            authorization=document_hash(authorization.body),
            allocation_id="01" * 32,
            buyer_tag="02" * 32,
            product="bond",
            resource=bond_resource(bond, period),
            certificate_pubkey=_pub(RENTER),
        ),
        SELLER,
    )
    return Rental(
        authorization=authorization,
        allocation=allocation,
        credential=_credential(expiry=period + 1, bond=bond),
    )


def _conflict_proof(
    *,
    rental: Rental | None = None,
    proof: str | None = None,
    reporter: CKey = RENTER,
    credential: BondCredential | None = None,
    allocation_hash: str | None = None,
    maker_nick: str = MAKER_NICK,
    taker_nick: str = TAKER_NICK,
) -> FaultProof:
    rental = rental or _rental()
    report = CertificateConflictReport(
        allocation=allocation_hash or document_hash(rental.allocation.body),
        credential=(credential or rental.credential).model_dump(mode="json"),
        proof=proof if proof is not None else _ordinary_proof(),
        maker_nick=maker_nick,
        taker_nick=taker_nick,
    )
    return FaultProof(
        reason="conflicting-bond-certificate",
        authorization=rental.authorization,
        first=rental.allocation,
        second=sign_document(report, reporter),
    )


def _offer(
    nick: str,
    *,
    cert_pubkey: str | None = None,
    cert_expiry: int = 40 * 2016,
    bond: BondReference = BOND,
    verified: bool | None = True,
) -> Offer:
    return Offer(
        counterparty=nick,
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=1,
        maxsize=1_000_000,
        txfee=0,
        cjfee=0,
        fidelity_bond_value=0,
        fidelity_bond_verified=verified,
        fidelity_bond_data={
            "utxo_txid": bond.outpoint.txid,
            "utxo_vout": bond.outpoint.vout,
            "utxo_pub": bond.pubkey,
            "locktime": bond.locktime,
            "cert_pub": cert_pubkey if cert_pubkey is not None else _pub(RIVAL),
            "cert_expiry": cert_expiry,
        },
    )


def _height(period: int) -> int:
    return period * 2016 + 1


# -- the owner's promise ------------------------------------------------------


def test_lease_binds_bond_period_and_renter_key() -> None:
    credential = _credential()
    lease = credential.verify()

    assert lease.policy == "exclusive-period-v1"
    assert (lease.bond, lease.period, lease.cert_pubkey) == (BOND, PERIOD, _pub(RENTER))


def test_credential_without_a_lease_is_not_a_credential() -> None:
    body = _credential().model_dump(mode="json")
    del body["lease"]

    with pytest.raises(ValidationError):
        BondCredential.model_validate(body)


@pytest.mark.parametrize(
    "lease",
    [
        sign_document(
            ExclusiveBondLease(
                bond=BOND.model_copy(update={"locktime": 600_000_001}),
                period=PERIOD,
                cert_pubkey=_pub(RENTER),
            ),
            OWNER,
        ),
        sign_document(ExclusiveBondLease(bond=BOND, period=PERIOD, cert_pubkey=_pub(RIVAL)), OWNER),
        sign_document(
            ExclusiveBondLease(bond=BOND, period=PERIOD + 1, cert_pubkey=_pub(RENTER)), OWNER
        ),
        sign_document(
            ExclusiveBondLease(bond=BOND, period=PERIOD, cert_pubkey=_pub(RENTER)), SELLER
        ),
    ],
)
def test_lease_for_another_bond_key_period_or_signer_is_rejected(lease: SignedDocument) -> None:
    with pytest.raises(MarketError):
        _credential(lease=lease).verify()


def test_tampered_lease_body_breaks_the_owner_signature() -> None:
    credential = _credential()
    tampered = credential.lease.model_copy(
        update={"body": {**credential.lease.body, "cert_pubkey": _pub(RIVAL)}}
    )

    with pytest.raises(MarketError):
        _credential(lease=tampered).verify()


def test_only_the_bond_owner_can_sign_a_lease() -> None:
    with pytest.raises(MarketError):
        sign_bond_lease(BOND, PERIOD, _pub(RENTER), SELLER)


def test_delivered_credential_must_carry_the_lease_for_the_sold_period() -> None:
    rental = _rental()
    authority, allocation = verify_allocation(rental.authorization, rental.allocation)

    assert isinstance(
        validate_credential(authority, allocation, rental.credential.model_dump(mode="json")),
        BondCredential,
    )

    stale = _credential(
        lease=sign_bond_lease(BOND, PERIOD - 1, _pub(RENTER), OWNER),
    )
    with pytest.raises(MarketError):
        validate_credential(authority, allocation, stale.model_dump(mode="json"))


# -- the renter's contradiction ----------------------------------------------


def test_signed_conflict_names_the_rival_certificate() -> None:
    authority, conflict = _conflict_proof().verify_detailed()

    assert authority.bond == BOND and authority.period == PERIOD
    assert conflict is not None
    assert conflict.cert_pubkey == _pub(RIVAL)
    assert conflict.cert_expiry_height == 40 * 2016
    assert conflict.maker_nick == MAKER_NICK


def test_cold_certificate_signed_before_the_lease_is_still_covered() -> None:
    """A long-expiry key certified earlier is exactly what the lease forbids."""
    _authority, conflict = _conflict_proof(
        proof=_ordinary_proof(expiry=RENTED_EXPIRY)
    ).verify_detailed()

    assert conflict is not None and conflict.cert_expiry_height == RENTED_EXPIRY * 2016


def test_renewing_the_same_hot_key_is_not_a_conflict() -> None:
    with pytest.raises(MarketError):
        _conflict_proof(proof=_ordinary_proof(cert_key=RENTER, expiry=60)).verify()


def test_rival_certificate_expiring_before_the_period_ends_is_not_a_conflict() -> None:
    with pytest.raises(MarketError):
        _conflict_proof(proof=_ordinary_proof(expiry=PERIOD)).verify()


def test_conflicting_offer_must_use_the_rented_bond() -> None:
    other_bond = BOND.model_copy(update={"outpoint": ExternalPoDLEOutpoint(txid="bb" * 32, vout=7)})

    with pytest.raises(MarketError):
        _conflict_proof(proof=_ordinary_proof(bond=other_bond)).verify()


def test_ordinary_maker_proof_carries_no_exclusivity_promise() -> None:
    """A normal maker cert stays a normal maker cert: no lease, no accusation."""
    rental = _rental()
    body = rental.credential.model_dump(mode="json")
    del body["lease"]
    report = CertificateConflictReport.model_construct(
        allocation=document_hash(rental.allocation.body),
        credential=body,
        proof=_ordinary_proof(),
        maker_nick=MAKER_NICK,
        taker_nick=TAKER_NICK,
    )
    proof = FaultProof(
        reason="conflicting-bond-certificate",
        authorization=rental.authorization,
        first=rental.allocation,
        second=sign_document(report, RENTER),
    )

    with pytest.raises(MarketError):
        proof.verify()


def test_only_the_renter_can_sign_the_report() -> None:
    with pytest.raises(MarketError):
        _conflict_proof(reporter=SELLER).verify()
    with pytest.raises(MarketError):
        _conflict_proof(reporter=OWNER).verify()


def test_report_must_cover_the_allocation_it_travels_with() -> None:
    with pytest.raises(MarketError):
        _conflict_proof(allocation_hash="ff" * 32).verify()


def test_report_credential_must_be_the_rented_one() -> None:
    other = _credential(cert_key=RIVAL, expiry=RENTED_EXPIRY)

    with pytest.raises(MarketError):
        _conflict_proof(credential=other).verify()


def test_conflict_proof_rejects_a_second_authorization() -> None:
    rental = _rental()
    proof = _conflict_proof(rental=rental)

    with pytest.raises(MarketError):
        proof.model_copy(update={"second_authorization": rental.authorization}).verify()


@pytest.mark.parametrize(
    "proof",
    [
        base64.b64encode(b"\x00" * 252).decode("ascii")[:-4] + "AAA=",
        base64.b64encode(b"\x00" * 251).decode("ascii"),
        "!" * 336,
    ],
)
def test_bond_proof_must_be_canonical_fixed_length_base64(proof: str) -> None:
    with pytest.raises((MarketError, ValidationError)):
        _conflict_proof(proof=proof)


@pytest.mark.parametrize("nick", ["not-a-nick", "J" * 16, MAKER_NICK + "x"])
def test_report_nicks_must_be_bounded_joinmarket_nicks(nick: str) -> None:
    with pytest.raises(ValidationError):
        _conflict_proof(maker_nick=nick)


def test_tampered_signed_report_or_allocation_is_rejected() -> None:
    proof = _conflict_proof()
    flipped = proof.second.signature[:-2] + ("00" if proof.second.signature[-2:] != "00" else "01")

    with pytest.raises(MarketError):
        proof.model_copy(
            update={"second": proof.second.model_copy(update={"signature": flipped})}
        ).verify()

    other_allocation = _rental(period=PERIOD + 1).allocation
    with pytest.raises(MarketError):
        proof.model_copy(update={"first": other_allocation}).verify()


# -- local corroboration, the only source of timing ---------------------------


def test_conflict_needs_a_live_matching_offer_inside_the_rented_period(tmp_path) -> None:
    raw = canonical(_conflict_proof())
    cache = MarketFaultCache(tmp_path)
    assert cache.ingest(raw)

    # Right bond, wrong certificate: nothing observed yet.
    stale = [_offer("maker-one", cert_pubkey=_pub(RENTER), cert_expiry=RENTED_EXPIRY * 2016)]
    assert cache.excluded_nicks(stale, network="regtest", height=_height(PERIOD)) == set()
    assert not cache.excludes_verified_bond(BOND, height=_height(PERIOD))
    assert not (tmp_path / "market_faults.json").exists()

    live = [_offer("maker-one"), _offer("maker-two")]
    assert cache.excluded_nicks(live, network="regtest", height=_height(PERIOD)) == {
        "maker-one",
        "maker-two",
    }
    assert cache.excludes_verified_bond(BOND, height=_height(PERIOD))
    assert (tmp_path / "market_faults.json").exists()


def test_observation_survives_into_the_next_period_but_the_bare_proof_does_not(tmp_path) -> None:
    raw = canonical(_conflict_proof())
    cache = MarketFaultCache(tmp_path)
    assert cache.ingest(raw)
    assert cache.excluded_nicks([_offer("maker")], network="regtest", height=_height(PERIOD))

    restored = MarketFaultCache(tmp_path)
    assert restored.excluded_nicks(
        [_offer("maker")], network="regtest", height=_height(PERIOD + 1)
    ) == {"maker"}
    assert not restored.excludes_verified_bond(BOND, height=_height(PERIOD + 2))

    late = MarketFaultCache()
    assert late.ingest(raw)
    assert (
        late.excluded_nicks([_offer("maker")], network="regtest", height=_height(PERIOD + 1))
        == set()
    )
    assert not late.excludes_verified_bond(BOND, height=_height(PERIOD + 1))


def test_a_proof_arriving_after_the_period_never_becomes_retrospective(tmp_path) -> None:
    """A certificate issued after the lease verifies identically; timing is local."""
    raw = canonical(_conflict_proof())
    cache = MarketFaultCache(tmp_path)
    assert cache.ingest(raw)

    offers = [_offer("maker")]
    assert cache.excluded_nicks(offers, network="regtest", height=_height(PERIOD + 1)) == set()
    assert cache.excluded_nicks(offers, network="regtest", height=_height(PERIOD + 2)) == set()
    assert not (tmp_path / "market_faults.json").exists()


def test_restored_observations_must_have_been_recorded_inside_the_period(tmp_path) -> None:
    raw = canonical(_conflict_proof())
    cache = MarketFaultCache(tmp_path)
    assert cache.ingest(raw)
    assert cache.excluded_nicks([_offer("maker")], network="regtest", height=_height(PERIOD))

    path = tmp_path / "market_faults.json"
    for mutation in (
        lambda text: text.replace(f'"height":{_height(PERIOD)}', f'"height":{_height(PERIOD + 1)}'),
        lambda text: text.replace(f'"height":{_height(PERIOD)}', '"height":"34273"'),
        lambda text: text.replace('"observations"', '"unused"'),
    ):
        original = path.read_text()
        path.write_text(mutation(original))
        assert not MarketFaultCache(tmp_path).excludes_verified_bond(BOND, height=_height(PERIOD))
        path.write_text(original)

    assert MarketFaultCache(tmp_path).excludes_verified_bond(BOND, height=_height(PERIOD))


def test_uncorroborated_conflict_cannot_hide_static_fault_evidence(tmp_path) -> None:
    rental = _rental()
    delivery = sign_document(
        Delivery(allocation=document_hash(rental.allocation.body), credential={"version": 1}),
        SELLER,
    )
    static = FaultProof(
        reason="invalid-delivery",
        authorization=rental.authorization,
        first=rental.allocation,
        second=delivery,
    )
    conflict = _conflict_proof(rental=rental)
    assert fault_proof_id(conflict) != fault_proof_id(static)

    cache = MarketFaultCache(tmp_path)
    assert cache.ingest(canonical(conflict))
    assert cache.ingest(canonical(static))

    # The conflict is still unproven, yet the immediately valid fault applies.
    offers = [_offer("maker", cert_pubkey=_pub(RENTER), cert_expiry=RENTED_EXPIRY * 2016)]
    assert cache.excluded_nicks(offers, network="regtest", height=_height(PERIOD)) == {"maker"}
    assert MarketFaultCache(tmp_path).excludes_verified_bond(BOND, height=_height(PERIOD))


def test_incomplete_cache_format_does_not_create_a_sanction(tmp_path) -> None:
    rental = _rental()
    static = FaultProof(
        reason="invalid-delivery",
        authorization=rental.authorization,
        first=rental.allocation,
        second=sign_document(
            Delivery(allocation=document_hash(rental.allocation.body), credential={"version": 1}),
            SELLER,
        ),
    )
    encoded = base64.b64encode(canonical(static)).decode("ascii")
    (tmp_path / "market_faults.json").write_text(
        '{"proofs":{"regtest":["' + encoded + '"]},"version":1}'
    )

    cache = MarketFaultCache(tmp_path)
    assert not cache.excludes_verified_bond(BOND, height=_height(PERIOD))


def test_unverified_offer_cannot_corroborate_a_conflict() -> None:
    cache = MarketFaultCache()
    assert cache.ingest(canonical(_conflict_proof()))

    unverified = [_offer("maker", verified=None)]
    assert cache.excluded_nicks(unverified, network="regtest", height=_height(PERIOD)) == set()


def test_future_local_observation_cannot_sanction_before_it_was_recorded(tmp_path) -> None:
    observed_height = _height(PERIOD) + 100
    cache = MarketFaultCache(tmp_path)
    assert cache.ingest(canonical(_conflict_proof()))
    assert cache.excluded_nicks([_offer("maker")], network="regtest", height=observed_height)

    restored = MarketFaultCache(tmp_path)
    assert not restored.excludes_verified_bond(BOND, height=observed_height - 1)
    assert not restored.excluded_nicks(
        [_offer("maker")], network="regtest", height=observed_height - 1
    )
    assert restored.excludes_verified_bond(BOND, height=observed_height)


def test_conflict_candidates_cannot_evict_unpromoted_static_evidence(monkeypatch) -> None:
    import jmcore.market_faults as faults

    monkeypatch.setattr(faults, "_MAX_ENTRIES", 1)
    rental = _rental()
    static = FaultProof(
        reason="invalid-delivery",
        authorization=rental.authorization,
        first=rental.allocation,
        second=sign_document(
            Delivery(allocation=document_hash(rental.allocation.body), credential={"version": 1}),
            SELLER,
        ),
    )
    cache = MarketFaultCache()
    assert cache.ingest(canonical(static))
    assert not cache.ingest(canonical(_conflict_proof()))
    assert cache.excludes_verified_bond(BOND, height=_height(PERIOD))
