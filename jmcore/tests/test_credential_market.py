"""Tests for signed credential-market contracts and fault evidence."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Literal

import pytest
from bitcointx.core.key import CKey
from pydantic import ValidationError

from jmcore.bitcoin import hash160
from jmcore.credential_market import (
    MAX_MARKET_BYTES,
    Allocation,
    BondCredential,
    BondReference,
    CredentialPackage,
    Delivery,
    FaultProof,
    MarketAuthorization,
    MarketError,
    MarketModel,
    SignedDocument,
    bond_resource,
    canonical,
    decode_document,
    document_hash,
    fault_proof_id,
    period_at_height,
    sign_document,
    verify_allocation,
    verify_authorization,
)
from jmcore.crypto import bitcoin_message_hash_bytes, get_cert_msg
from jmcore.external_podle import ExternalPoDLE, ExternalPoDLEOutpoint
from jmcore.podle import generate_podle


def _key(byte: int) -> CKey:
    return CKey(bytes([byte]) * 32)


def _pubkey(key: CKey) -> str:
    return bytes(key.pub).hex()


def _hex(value: int) -> str:
    return f"{value:064x}"


def _changed_hex(value: str) -> str:
    return ("0" if value[0] != "0" else "1") + value[1:]


@dataclass(frozen=True)
class MarketKeys:
    owner: CKey
    seller: CKey
    other_seller: CKey
    certificate: CKey
    bond: BondReference


@pytest.fixture
def market_keys() -> MarketKeys:
    owner = _key(0x11)
    return MarketKeys(
        owner=owner,
        seller=_key(0x22),
        other_seller=_key(0x33),
        certificate=_key(0x44),
        bond=BondReference(
            network="regtest",
            outpoint=ExternalPoDLEOutpoint(txid="aa" * 32, vout=7),
            pubkey=_pubkey(owner),
            locktime=600_000_000,
        ),
    )


def _authorization(
    owner: CKey,
    seller: CKey,
    bond: BondReference,
    *,
    period: int = 17,
) -> SignedDocument:
    return sign_document(
        MarketAuthorization(bond=bond, period=period, seller_pubkey=_pubkey(seller)),
        owner,
    )


def _allocation(
    authorization: SignedDocument,
    seller: CKey,
    *,
    product: Literal["podle", "bond"],
    resource: str,
    allocation_id: int,
    buyer_tag: int,
    certificate: CKey | None = None,
) -> SignedDocument:
    return sign_document(
        Allocation(
            authorization=document_hash(authorization.body),
            allocation_id=_hex(allocation_id),
            buyer_tag=_hex(buyer_tag),
            product=product,
            resource=resource,
            certificate_pubkey=_pubkey(certificate) if certificate is not None else None,
        ),
        seller,
    )


def _podle_credential(*, network: str = "regtest", index: int = 0) -> dict[str, object]:
    proof = generate_podle(b"\x55" * 32, f"{'bb' * 32}:1", index=index)
    return {
        "version": 1,
        "network": network,
        "outpoint": {"txid": "bb" * 32, "vout": 1},
        "P": proof.p.hex(),
        "P2": proof.p2.hex(),
        "sig": proof.sig.hex(),
        "e": proof.e.hex(),
        "commitment": proof.commitment.hex(),
        "index": index,
        "scriptpubkey": (b"\x00\x14" + hash160(proof.p)).hex(),
        "blockheight": 101,
    }


def _bond_credential(
    owner: CKey,
    bond: BondReference,
    certificate: CKey,
    *,
    expiry: int,
) -> BondCredential:
    cert_pubkey = bytes(certificate.pub)
    signature = owner.sign(
        bitcoin_message_hash_bytes(get_cert_msg(cert_pubkey, expiry)),
        _ecdsa_sig_grind_low_r=False,
    )
    return BondCredential(
        bond=bond,
        cert_pubkey=cert_pubkey.hex(),
        cert_expiry=expiry,
        cert_signature=signature.hex(),
    )


def _delivery(
    allocation: SignedDocument,
    seller: CKey,
    credential: dict[str, object],
) -> SignedDocument:
    return sign_document(
        Delivery(allocation=document_hash(allocation.body), credential=credential),
        seller,
    )


def _sign_with_extra_entropy(body: MarketModel, key: CKey, entropy: int) -> SignedDocument:
    digest = bitcoin_message_hash_bytes(b"JMP-MARKET-V1|" + canonical(body))
    signature = key.sign(
        digest,
        _ecdsa_sig_grind_low_r=False,
        _ecdsa_sig_extra_entropy=entropy,
    ).hex()
    return SignedDocument(body=body.model_dump(mode="json"), signature=signature)


class Quote(MarketModel):
    kind: Literal["quote"] = "quote"
    version: Literal[1] = 1
    resource: str


def test_honest_podle_and_bond_credential_packages_verify(market_keys: MarketKeys) -> None:
    authority = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)

    podle = _podle_credential()
    podle_allocation = _allocation(
        authority,
        market_keys.seller,
        product="podle",
        resource=str(podle["commitment"]),
        allocation_id=1,
        buyer_tag=101,
    )
    podle_delivery = _delivery(podle_allocation, market_keys.seller, podle)
    verified_podle = CredentialPackage(
        authorization=authority,
        allocation=podle_allocation,
        delivery=podle_delivery,
    ).verify()

    bond_allocation = _allocation(
        authority,
        market_keys.seller,
        product="bond",
        resource=bond_resource(market_keys.bond, 17),
        allocation_id=2,
        buyer_tag=102,
        certificate=market_keys.certificate,
    )
    bond = _bond_credential(
        market_keys.owner,
        market_keys.bond,
        market_keys.certificate,
        expiry=18,
    )
    bond_delivery = _delivery(
        bond_allocation,
        market_keys.seller,
        bond.model_dump(mode="json"),
    )
    verified_bond = CredentialPackage(
        authorization=authority,
        allocation=bond_allocation,
        delivery=bond_delivery,
    ).verify()

    assert isinstance(verified_podle, ExternalPoDLE)
    assert verified_podle.commitment == podle["commitment"]
    assert verified_bond == bond


def test_signed_double_allocation_is_fault_evidence(market_keys: MarketKeys) -> None:
    authority = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)
    resource = bond_resource(market_keys.bond, 17)
    first = _allocation(
        authority,
        market_keys.seller,
        product="bond",
        resource=resource,
        allocation_id=1,
        buyer_tag=101,
        certificate=market_keys.certificate,
    )
    second = _allocation(
        authority,
        market_keys.seller,
        product="bond",
        resource=resource,
        allocation_id=2,
        buyer_tag=102,
        certificate=market_keys.certificate,
    )
    proof = FaultProof(
        reason="double-allocation",
        authorization=authority,
        first=first,
        second=second,
    )

    assert proof.verify() == verify_authorization(authority)
    assert fault_proof_id(proof) == resource


def test_different_authorized_seller_keys_can_prove_double_allocation(
    market_keys: MarketKeys,
) -> None:
    first_authority = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)
    second_authority = _authorization(
        market_keys.owner,
        market_keys.other_seller,
        market_keys.bond,
    )
    resource = bond_resource(market_keys.bond, 17)
    first = _allocation(
        first_authority,
        market_keys.seller,
        product="bond",
        resource=resource,
        allocation_id=1,
        buyer_tag=101,
        certificate=market_keys.certificate,
    )
    second = _allocation(
        second_authority,
        market_keys.other_seller,
        product="bond",
        resource=resource,
        allocation_id=2,
        buyer_tag=102,
        certificate=market_keys.certificate,
    )

    proof = FaultProof(
        reason="double-allocation",
        authorization=first_authority,
        first=first,
        second=second,
        second_authorization=second_authority,
    )

    assert proof.verify() == verify_authorization(first_authority)


def test_seller_signed_invalid_delivery_is_fault_evidence(market_keys: MarketKeys) -> None:
    authority = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)
    allocation = _allocation(
        authority,
        market_keys.seller,
        product="bond",
        resource=bond_resource(market_keys.bond, 17),
        allocation_id=1,
        buyer_tag=101,
        certificate=market_keys.certificate,
    )
    invalid_delivery = _delivery(allocation, market_keys.seller, {"version": 1})
    proof = FaultProof(
        reason="invalid-delivery",
        authorization=authority,
        first=allocation,
        second=invalid_delivery,
    )

    assert proof.verify() == verify_authorization(authority)


def test_tampered_authorization_and_allocation_signatures_are_rejected(
    market_keys: MarketKeys,
) -> None:
    authority = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)
    tampered_authority = authority.model_copy(
        update={"signature": _changed_hex(authority.signature)}
    )
    with pytest.raises(MarketError):
        verify_authorization(tampered_authority)

    allocation = _allocation(
        authority,
        market_keys.seller,
        product="bond",
        resource=bond_resource(market_keys.bond, 17),
        allocation_id=1,
        buyer_tag=101,
        certificate=market_keys.certificate,
    )
    tampered_allocation = allocation.model_copy(
        update={"signature": _changed_hex(allocation.signature)}
    )
    with pytest.raises(MarketError):
        verify_allocation(authority, tampered_allocation)


def test_tampered_network_bond_certificate_and_resource_are_rejected(
    market_keys: MarketKeys,
) -> None:
    authority = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)
    podle = _podle_credential(network="testnet")
    podle_allocation = _allocation(
        authority,
        market_keys.seller,
        product="podle",
        resource=str(podle["commitment"]),
        allocation_id=1,
        buyer_tag=101,
    )
    with pytest.raises(MarketError, match="PoDLE"):
        CredentialPackage(
            authorization=authority,
            allocation=podle_allocation,
            delivery=_delivery(podle_allocation, market_keys.seller, podle),
        ).verify()

    mismatched_bond = market_keys.bond.model_copy(
        update={"outpoint": ExternalPoDLEOutpoint(txid="cc" * 32, vout=7)}
    )
    wrong_certificate = _bond_credential(
        market_keys.owner,
        mismatched_bond,
        market_keys.certificate,
        expiry=18,
    )
    bond_allocation = _allocation(
        authority,
        market_keys.seller,
        product="bond",
        resource=bond_resource(market_keys.bond, 17),
        allocation_id=2,
        buyer_tag=102,
        certificate=market_keys.certificate,
    )
    with pytest.raises(MarketError, match="certificate"):
        CredentialPackage(
            authorization=authority,
            allocation=bond_allocation,
            delivery=_delivery(
                bond_allocation,
                market_keys.seller,
                wrong_certificate.model_dump(mode="json"),
            ),
        ).verify()

    wrong_resource = _allocation(
        authority,
        market_keys.seller,
        product="bond",
        resource=_hex(999),
        allocation_id=3,
        buyer_tag=103,
        certificate=market_keys.certificate,
    )
    with pytest.raises(MarketError, match="resource"):
        verify_allocation(authority, wrong_resource)


def test_buyer_plaintext_edit_cannot_turn_invalid_podle_into_evidence(
    market_keys: MarketKeys,
) -> None:
    authority = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)
    podle = _podle_credential()
    allocation = _allocation(
        authority,
        market_keys.seller,
        product="podle",
        resource=str(podle["commitment"]),
        allocation_id=1,
        buyer_tag=101,
    )
    delivery = _delivery(allocation, market_keys.seller, podle)
    edited_body = copy.deepcopy(delivery.body)
    edited_body["credential"]["commitment"] = "00" * 32
    buyer_edited_delivery = SignedDocument(body=edited_body, signature=delivery.signature)

    with pytest.raises(MarketError):
        FaultProof(
            reason="invalid-delivery",
            authorization=authority,
            first=allocation,
            second=buyer_edited_delivery,
        ).verify()


def test_quote_is_not_an_allocation(market_keys: MarketKeys) -> None:
    authority = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)
    quote = sign_document(Quote(resource="bond-price"), market_keys.seller)

    with pytest.raises(MarketError):
        verify_allocation(authority, quote)


def test_replayed_allocation_with_different_valid_ecdsa_signature_is_not_fault(
    market_keys: MarketKeys,
) -> None:
    authority = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)
    body = Allocation(
        authorization=document_hash(authority.body),
        allocation_id=_hex(1),
        buyer_tag=_hex(101),
        product="bond",
        resource=bond_resource(market_keys.bond, 17),
        certificate_pubkey=_pubkey(market_keys.certificate),
    )
    first = _sign_with_extra_entropy(body, market_keys.seller, entropy=1)
    replay = _sign_with_extra_entropy(body, market_keys.seller, entropy=2)

    assert first.signature != replay.signature
    with pytest.raises(MarketError, match="No conflicting"):
        FaultProof(
            reason="double-allocation",
            authorization=authority,
            first=first,
            second=replay,
        ).verify()


def test_valid_delivery_no_response_and_unrelated_allocation_are_not_fault(
    market_keys: MarketKeys,
) -> None:
    authority = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)
    podle = _podle_credential()
    allocation = _allocation(
        authority,
        market_keys.seller,
        product="podle",
        resource=str(podle["commitment"]),
        allocation_id=1,
        buyer_tag=101,
    )
    delivery = _delivery(allocation, market_keys.seller, podle)
    with pytest.raises(MarketError, match="Valid delivery"):
        FaultProof(
            reason="invalid-delivery",
            authorization=authority,
            first=allocation,
            second=delivery,
        ).verify()

    with pytest.raises(ValidationError):
        FaultProof(reason="invalid-delivery", authorization=authority, first=allocation)

    unrelated = _allocation(
        authority,
        market_keys.seller,
        product="podle",
        resource=_hex(202),
        allocation_id=2,
        buyer_tag=102,
    )
    with pytest.raises(MarketError, match="No conflicting"):
        FaultProof(
            reason="double-allocation",
            authorization=authority,
            first=allocation,
            second=unrelated,
        ).verify()


def test_forged_victim_outpoint_cannot_claim_victim_bond_key() -> None:
    victim = _key(0x66)
    attacker = _key(0x77)
    victim_outpoint = ExternalPoDLEOutpoint(txid="dd" * 32, vout=3)
    claimed_victim_bond = BondReference(
        network="regtest",
        outpoint=victim_outpoint,
        pubkey=_pubkey(victim),
        locktime=600_000_000,
    )
    forged = _authorization(attacker, _key(0x88), claimed_victim_bond)

    with pytest.raises(MarketError):
        verify_authorization(forged)

    declared_attacker_bond = claimed_victim_bond.model_copy(update={"pubkey": _pubkey(attacker)})
    declared = _authorization(attacker, _key(0x88), declared_attacker_bond)
    authority = verify_authorization(declared)
    assert authority.bond.outpoint == victim_outpoint
    assert authority.bond.pubkey != _pubkey(victim)


def test_same_allocation_id_with_different_buyer_tags_is_fault_evidence(
    market_keys: MarketKeys,
) -> None:
    authority = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)
    resource = bond_resource(market_keys.bond, 17)
    first = _allocation(
        authority,
        market_keys.seller,
        product="bond",
        resource=resource,
        allocation_id=1,
        buyer_tag=101,
        certificate=market_keys.certificate,
    )
    second = _allocation(
        authority,
        market_keys.seller,
        product="bond",
        resource=resource,
        allocation_id=1,
        buyer_tag=102,
        certificate=market_keys.certificate,
    )

    assert FaultProof(
        reason="double-allocation",
        authorization=authority,
        first=first,
        second=second,
    ).verify() == verify_authorization(authority)


@pytest.mark.parametrize(
    ("height", "expected_period"),
    [(1, 0), (2016, 0), (2017, 1), (4032, 1), (4033, 2)],
)
def test_period_at_height_inclusive_expiry_boundaries(height: int, expected_period: int) -> None:
    assert period_at_height(height) == expected_period


@pytest.mark.parametrize("height", [0, -1, True, 1.0])
def test_period_at_height_rejects_non_confirmed_heights(height: object) -> None:
    with pytest.raises(MarketError):
        period_at_height(height)  # type: ignore[arg-type]


def test_bond_certificate_expiry_bounds(market_keys: MarketKeys) -> None:
    credential = _bond_credential(
        market_keys.owner,
        market_keys.bond,
        market_keys.certificate,
        expiry=1,
    )
    assert credential.cert_expiry == 1
    data = credential.model_dump(mode="json")
    upper_bound = BondCredential.model_validate({**data, "cert_expiry": 65535})
    assert upper_bound.cert_expiry == 65535

    for invalid_expiry in (0, 65536):
        with pytest.raises(ValidationError):
            BondCredential.model_validate({**data, "cert_expiry": invalid_expiry})


@pytest.mark.parametrize(
    "raw",
    [
        b'{"value":1,"value":2}',
        b'{"value":1.5}',
        b'{"value":"\\u00e9"}',
        b"x" * (MAX_MARKET_BYTES + 1),
    ],
)
def test_decode_document_rejects_ambiguous_or_noncanonical_json(raw: bytes) -> None:
    with pytest.raises(MarketError):
        decode_document(raw)


def test_decode_document_rejects_deep_nesting() -> None:
    nested: object = 0
    for _ in range(13):
        nested = [nested]

    with pytest.raises(MarketError):
        decode_document(json.dumps({"nested": nested}).encode("ascii"))


def test_podle_resale_across_periods_uses_later_fault_period(market_keys: MarketKeys) -> None:
    old = _authorization(market_keys.owner, market_keys.seller, market_keys.bond, period=17)
    new = _authorization(market_keys.owner, market_keys.other_seller, market_keys.bond, period=18)
    first = _allocation(
        old, market_keys.seller, product="podle", resource=_hex(99), allocation_id=1, buyer_tag=2
    )
    second = _allocation(
        new,
        market_keys.other_seller,
        product="podle",
        resource=_hex(99),
        allocation_id=3,
        buyer_tag=4,
    )
    proof = FaultProof(
        reason="double-allocation",
        authorization=old,
        first=first,
        second=second,
        second_authorization=new,
    )
    assert proof.verify().period == 18
    reversed_proof = proof.model_copy(
        update={"authorization": new, "first": second, "second": first, "second_authorization": old}
    )
    assert fault_proof_id(proof) == fault_proof_id(reversed_proof)


@pytest.mark.parametrize("field,value", [("kind", None), ("version", None), ("version", True)])
def test_signed_metadata_cannot_use_implicit_defaults(
    market_keys: MarketKeys, field: str, value: object
) -> None:
    authorization = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)
    body = dict(authorization.body)
    if value is None:
        del body[field]
    else:
        body[field] = value
    signature = market_keys.owner.sign(
        bitcoin_message_hash_bytes(b"JMP-MARKET-V1|" + canonical(body))
    )
    malformed = SignedDocument(body=body, signature=signature.hex())
    with pytest.raises(ValueError):
        verify_authorization(malformed)


def test_jmp_0012_authorization_vector() -> None:
    owner = CKey((1).to_bytes(32, "big"))
    seller = CKey((2).to_bytes(32, "big"))
    authorization = MarketAuthorization(
        bond=BondReference(
            network="regtest",
            outpoint=ExternalPoDLEOutpoint(txid="11" * 32, vout=0),
            pubkey=owner.pub.hex(),
            locktime=2000000000,
        ),
        period=1,
        seller_pubkey=seller.pub.hex(),
    )
    assert document_hash(authorization) == (
        "6eb514a0e99991234c97b6b66d975e560d9c669e387397e4bfa67ee135922c16"
    )
    signed = SignedDocument(
        body=authorization.model_dump(mode="json"),
        signature=(
            "30440220513dc0a8b155bcf580f67c80610721acd5c0ac6c32f49b7ecaf42b0aced7b6af"
            "02206f3eff4fd4d6ab95bd5e2d29d8f6bb8438e7965828f7fcdb44bbb4f1c0aec2bf"
        ),
    )
    assert verify_authorization(signed) == authorization


def test_signed_out_of_standard_retry_range_is_invalid_delivery(market_keys: MarketKeys) -> None:
    authority = _authorization(market_keys.owner, market_keys.seller, market_keys.bond)
    credential = _podle_credential(index=3)
    assert ExternalPoDLE.model_validate(credential).index == 3
    allocation = _allocation(
        authority,
        market_keys.seller,
        product="podle",
        resource=str(credential["commitment"]),
        allocation_id=7,
        buyer_tag=8,
    )
    delivery = _delivery(allocation, market_keys.seller, credential)
    package = CredentialPackage(authorization=authority, allocation=allocation, delivery=delivery)
    with pytest.raises(MarketError, match="retry range"):
        package.verify()
    assert (
        FaultProof(
            reason="invalid-delivery", authorization=authority, first=allocation, second=delivery
        )
        .verify()
        .bond
        == market_keys.bond
    )
