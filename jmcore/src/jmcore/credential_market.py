"""Versioned, transport-independent credential contracts and fault evidence.

Only signed contradictions are punishable. Payment claims, chain lookup failures,
and timeouts are deliberately outside the evidence language.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Annotated, Any, Literal, TypeVar

from bitcointx.core.key import CKey, CPubKey
from pydantic import BaseModel, ConfigDict, Field, field_validator

from jmcore.crypto import (
    NICK_MAX_ENCODED,
    bitcoin_message_hash_bytes,
    get_ascii_cert_msg,
    get_cert_msg,
    verify_bitcoin_message_signature,
    verify_fidelity_bond_proof,
    verify_strict_ecdsa,
)
from jmcore.external_podle import ExternalPoDLE, ExternalPoDLEOutpoint

MAX_MARKET_BYTES = 16384
MAX_MARKET_LISTING_BYTES = 2048
# Sellers push listings and re-announce them before they expire, so observers
# never poll. Listings are indicative; the binding quote is private.
MARKET_LISTING_LIFETIME_SECONDS = 3600
MAX_MARKET_LISTING_LIFETIME_SECONDS = 7200
MARKET_PODLE_RETRIES = 3
BLOCKS_PER_PERIOD = 2016
BOND_PROOF_BYTES = 252
BOND_PROOF_CHARS = 336
Hex32 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Pubkey = Annotated[str, Field(pattern=r"^(02|03)[0-9a-f]{64}$")]
Signature = Annotated[str, Field(pattern=r"^[0-9a-f]{16,144}$")]
Nick = Annotated[str, Field(pattern=rf"^J[0-9][1-9A-HJ-NP-Za-km-zO]{{{NICK_MAX_ENCODED}}}$")]
BondProof = Annotated[
    str,
    Field(min_length=BOND_PROOF_CHARS, max_length=BOND_PROOF_CHARS, pattern=r"^[A-Za-z0-9+/]+$"),
]
Network = Literal["mainnet", "testnet", "signet", "regtest"]
Product = Literal["podle", "bond"]
ModelT = TypeVar("ModelT", bound="MarketModel")


class MarketError(ValueError):
    """Untrusted market data failed validation."""


class MarketModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("version", mode="before", check_fields=False)
    @classmethod
    def integer_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Version must be an integer")
        return value


def _json_tree(value: Any, depth: int = 0) -> None:
    if depth > 12:
        raise MarketError("Market document nesting limit exceeded")
    if value is None or type(value) is bool:
        return
    if type(value) is int and abs(value) <= 2**53 - 1:
        return
    if isinstance(value, str) and value.isascii():
        return
    if isinstance(value, list):
        for item in value:
            _json_tree(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(k, str) and k.isascii() for k in value):
        for item in value.values():
            _json_tree(item, depth + 1)
        return
    raise MarketError("Market JSON requires ASCII strings and exact integers")


def canonical(value: BaseModel | dict[str, Any]) -> bytes:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    _json_tree(data)
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if len(encoded) > MAX_MARKET_BYTES:
        raise MarketError("Market document size limit exceeded")
    return encoded


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MarketError("Duplicate JSON key")
        result[key] = value
    return result


def decode_document(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_MARKET_BYTES:
        raise MarketError("Market document size limit exceeded")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise MarketError("Expected a JSON object")
        _json_tree(value)
        return value
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise MarketError("Invalid market JSON") from exc


def document_hash(value: BaseModel | dict[str, Any]) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def period_at_height(height: int) -> int:
    """Intervals end at the inclusive legacy certificate expiry block."""
    if type(height) is not int or height < 1:
        raise MarketError("A confirmed chain height is required")
    return (height - 1) // 2016


class SignedDocument(MarketModel):
    body: dict[str, Any]
    signature: Signature

    def verified(self, model: type[ModelT], public_key: str) -> ModelT:
        try:
            if type(self.body.get("version")) is not int or self.body["version"] != 1:
                raise MarketError("Missing or unsupported signed document version")
            expected_kind = model.model_fields.get("kind")
            if expected_kind is not None and self.body.get("kind") != expected_kind.default:
                raise MarketError("Signed document kind mismatch")
            digest = bitcoin_message_hash_bytes(b"JMP-MARKET-V1|" + canonical(self.body))
            if not verify_strict_ecdsa(
                digest, bytes.fromhex(self.signature), bytes.fromhex(public_key)
            ):
                raise MarketError("Invalid market signature")
            return model.model_validate(self.body)
        except (ValueError, TypeError) as exc:
            raise MarketError("Invalid signed market document") from exc


def sign_document(body: MarketModel, key: CKey) -> SignedDocument:
    digest = bitcoin_message_hash_bytes(b"JMP-MARKET-V1|" + canonical(body))
    return SignedDocument(body=body.model_dump(mode="json"), signature=key.sign(digest).hex())


class BondReference(MarketModel):
    network: Network
    outpoint: ExternalPoDLEOutpoint
    pubkey: Pubkey
    locktime: int = Field(ge=500000000, le=0xFFFFFFFF)

    @field_validator("pubkey")
    @classmethod
    def valid_point(cls, value: str) -> str:
        if not CPubKey(bytes.fromhex(value)).is_fullyvalid():
            raise ValueError("Invalid bond public key")
        return value


class MarketAuthorization(MarketModel):
    kind: Literal["authorization"] = "authorization"
    version: Literal[1] = 1
    policy: Literal["signed-faults-v1"] = "signed-faults-v1"
    bond: BondReference
    period: int = Field(ge=0, le=65534)
    seller_pubkey: Pubkey


def verify_authorization(document: SignedDocument) -> MarketAuthorization:
    # The self-declared outpoint is not trusted here. Sanction application must
    # match the bond key and script to an independently verified CoinJoin bond.
    candidate = MarketAuthorization.model_validate(document.body)
    return document.verified(MarketAuthorization, candidate.bond.pubkey)


class Allocation(MarketModel):
    kind: Literal["allocation"] = "allocation"
    version: Literal[1] = 1
    authorization: Hex32
    allocation_id: Hex32
    buyer_tag: Hex32
    product: Product
    resource: Hex32
    certificate_pubkey: Pubkey | None = None


def bond_resource(bond: BondReference, period: int) -> str:
    return document_hash({"bond": bond.model_dump(mode="json"), "period": period})


def verify_allocation(
    authorization: SignedDocument, document: SignedDocument
) -> tuple[MarketAuthorization, Allocation]:
    authority = verify_authorization(authorization)
    allocation = document.verified(Allocation, authority.seller_pubkey)
    if allocation.authorization != document_hash(authorization.body):
        raise MarketError("Allocation authorization mismatch")
    if allocation.product == "bond":
        if allocation.resource != bond_resource(authority.bond, authority.period):
            raise MarketError("Bond allocation resource mismatch")
        if allocation.certificate_pubkey is None:
            raise MarketError("Bond allocation requires renter certificate public key")
    elif allocation.certificate_pubkey is not None:
        raise MarketError("PoDLE allocation cannot contain a certificate key")
    return authority, allocation


class ExclusiveBondLease(MarketModel):
    """The owner's signed promise that one hot key alone represents the bond.

    ``policy`` fixes the meaning: during ``period`` the owner will not make any
    other certificate key usable for this bond, including keys certified earlier
    with a long expiry.  The promise authorises exclusion through the end of
    ``period + 1``, the last interval in which the rented certificate is live.
    """

    kind: Literal["exclusive-bond-lease"] = "exclusive-bond-lease"
    version: Literal[1] = 1
    policy: Literal["exclusive-period-v1"] = "exclusive-period-v1"
    bond: BondReference
    period: int = Field(ge=0, le=65534)
    cert_pubkey: Pubkey


def sign_bond_lease(
    bond: BondReference, period: int, cert_pubkey: str, owner_key: CKey
) -> SignedDocument:
    """Sign an exclusivity promise with the bond's own spending key."""
    if owner_key.pub.hex() != bond.pubkey:
        raise MarketError("Wrong bond spending key")
    try:
        lease = ExclusiveBondLease(bond=bond, period=period, cert_pubkey=cert_pubkey)
    except ValueError as exc:
        raise MarketError("Invalid exclusive bond lease") from exc
    return sign_document(lease, owner_key)


class BondCredential(MarketModel):
    version: Literal[1] = 1
    bond: BondReference
    cert_pubkey: Pubkey
    cert_expiry: int = Field(ge=1, le=65535)
    cert_signature: Signature
    lease: SignedDocument

    def verify(self) -> ExclusiveBondLease:
        pubkey = bytes.fromhex(self.cert_pubkey)
        sig = bytes.fromhex(self.cert_signature)
        owner = bytes.fromhex(self.bond.pubkey)
        messages = (
            get_cert_msg(pubkey, self.cert_expiry),
            get_ascii_cert_msg(pubkey, self.cert_expiry),
        )
        if not any(verify_bitcoin_message_signature(msg, sig, owner) for msg in messages):
            raise MarketError("Invalid bond certificate signature")
        lease = self.lease.verified(ExclusiveBondLease, self.bond.pubkey)
        if (
            lease.bond != self.bond
            or lease.cert_pubkey != self.cert_pubkey
            or lease.period != self.cert_expiry - 1
        ):
            raise MarketError("Exclusive bond lease does not cover this certificate")
        return lease


class Delivery(MarketModel):
    kind: Literal["delivery"] = "delivery"
    version: Literal[1] = 1
    allocation: Hex32
    credential: dict[str, Any]


def validate_credential(
    authority: MarketAuthorization, allocation: Allocation, credential: dict[str, Any]
) -> ExternalPoDLE | BondCredential:
    if type(credential.get("version")) is not int or credential["version"] != 1:
        raise MarketError("Missing or unsupported credential version")
    if allocation.product == "podle":
        podle = ExternalPoDLE.model_validate(credential)
        if podle.index >= MARKET_PODLE_RETRIES:
            raise MarketError("Market PoDLE index exceeds the standard maker retry range")
        if podle.network != authority.bond.network or podle.commitment != allocation.resource:
            raise MarketError("Delivered PoDLE does not match allocation")
        return podle
    bond = BondCredential.model_validate(credential)
    if (
        bond.bond != authority.bond
        or bond.cert_pubkey != allocation.certificate_pubkey
        or bond.cert_expiry != authority.period + 1
    ):
        raise MarketError("Delivered bond certificate does not match allocation")
    bond.verify()
    return bond


class CredentialPackage(MarketModel):
    authorization: SignedDocument
    allocation: SignedDocument
    delivery: SignedDocument

    def verify(self) -> ExternalPoDLE | BondCredential:
        authority, allocation = verify_allocation(self.authorization, self.allocation)
        delivery = self.delivery.verified(Delivery, authority.seller_pubkey)
        if delivery.allocation != document_hash(self.allocation.body):
            raise MarketError("Delivery allocation mismatch")
        return validate_credential(authority, allocation, delivery.credential)


class CertificateConflictReport(MarketModel):
    """A renter's signed claim that the owner also ran an ordinary maker offer.

    The report carries the rented credential and the contradicting public bond
    proof verbatim so that any verifier re-derives the contradiction instead of
    trusting the renter.  It carries no usable statement about *when* the
    conflicting offer was seen; only a local observation can establish that.
    """

    kind: Literal["certificate-conflict"] = "certificate-conflict"
    version: Literal[1] = 1
    allocation: Hex32
    credential: dict[str, Any]
    proof: BondProof
    maker_nick: Nick
    taker_nick: Nick


class ConflictingCertificate(MarketModel):
    """The rival certificate a verified conflict proof accuses, without timing."""

    bond: BondReference
    period: int = Field(ge=0, le=65534)
    cert_pubkey: Pubkey
    cert_expiry_height: int = Field(ge=1)
    maker_nick: Nick


def _decode_bond_proof(value: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise MarketError("Invalid fidelity bond proof encoding") from exc
    if len(raw) != BOND_PROOF_BYTES or base64.b64encode(raw).decode("ascii") != value:
        raise MarketError("Fidelity bond proof is not canonical base64")
    return raw


class FaultProof(MarketModel):
    kind: Literal["fault"] = "fault"
    version: Literal[1] = 1
    reason: Literal["double-allocation", "invalid-delivery", "conflicting-bond-certificate"]
    authorization: SignedDocument
    first: SignedDocument
    second: SignedDocument
    second_authorization: SignedDocument | None = None

    def verify(self) -> MarketAuthorization:
        return self.verify_detailed()[0]

    def verify_detailed(self) -> tuple[MarketAuthorization, ConflictingCertificate | None]:
        """Verify the proof and describe the accused rival certificate, if any.

        The result is purely cryptographic.  A conflicting certificate is *not*
        evidence that the owner equivocated while the lease was live: a legitimate
        certificate issued after the rented period verifies identically here.
        """
        authority, first = verify_allocation(self.authorization, self.first)
        if self.reason == "conflicting-bond-certificate":
            return authority, self._conflicting_certificate(authority, first)
        return self._verify_allocation_fault(authority, first), None

    def _conflicting_certificate(
        self, authority: MarketAuthorization, first: Allocation
    ) -> ConflictingCertificate:
        if self.second_authorization is not None:
            raise MarketError("Unexpected second authorization")
        if first.product != "bond" or first.certificate_pubkey is None:
            raise MarketError("Certificate conflicts require a finalized bond allocation")
        report = self.second.verified(CertificateConflictReport, first.certificate_pubkey)
        if report.allocation != document_hash(self.first.body):
            raise MarketError("Conflict report does not cover the rented allocation")
        try:
            rented = validate_credential(authority, first, report.credential)
        except ValueError as exc:
            raise MarketError("Conflict report does not carry the rented credential") from exc
        if not isinstance(rented, BondCredential):
            raise MarketError("Conflict report requires a rented bond credential")
        _decode_bond_proof(report.proof)
        valid, data, _error = verify_fidelity_bond_proof(
            report.proof, report.maker_nick, report.taker_nick
        )
        if not valid or data is None:
            raise MarketError("Conflicting offer proof is not a valid fidelity bond proof")
        bond = authority.bond
        if (
            data.get("utxo_txid") != bond.outpoint.txid
            or data.get("utxo_vout") != bond.outpoint.vout
            or data.get("utxo_pub") != bond.pubkey
            or data.get("locktime") != bond.locktime
        ):
            raise MarketError("Conflicting offer does not use the rented bond")
        if data.get("cert_pub") == rented.cert_pubkey:
            raise MarketError("Renewing the same certificate key is not a conflict")
        expiry = data.get("cert_expiry")
        if type(expiry) is not int or expiry < BLOCKS_PER_PERIOD * (authority.period + 1):
            raise MarketError("Conflicting certificate expired before the rented period ended")
        try:
            return ConflictingCertificate(
                bond=bond,
                period=authority.period,
                cert_pubkey=str(data.get("cert_pub")),
                cert_expiry_height=expiry,
                maker_nick=report.maker_nick,
            )
        except ValueError as exc:
            raise MarketError("Unusable conflicting certificate") from exc

    def _verify_allocation_fault(
        self, authority: MarketAuthorization, first: Allocation
    ) -> MarketAuthorization:
        if self.reason == "invalid-delivery":
            if self.second_authorization is not None:
                raise MarketError("Unexpected second authorization")
            delivery = self.second.verified(Delivery, authority.seller_pubkey)
            if delivery.allocation != document_hash(self.first.body):
                raise MarketError("Unrelated delivery is not fault evidence")
            try:
                validate_credential(authority, first, delivery.credential)
            except ValueError:
                return authority
            raise MarketError("Valid delivery is not fault evidence")

        other_authority, second = verify_allocation(
            self.second_authorization or self.authorization, self.second
        )
        if (
            authority.bond != other_authority.bond
            or (first.product == "bond" and authority.period != other_authority.period)
            or first.product != second.product
            or first.resource != second.resource
            or (
                first.allocation_id == second.allocation_id
                and first.buyer_tag == second.buyer_tag
                and first.certificate_pubkey == second.certificate_pubkey
            )
        ):
            raise MarketError("No conflicting finalized allocations")
        return max((authority, other_authority), key=lambda item: item.period)


def fault_proof_id(proof: FaultProof) -> str:
    """One cache identity per excluded bond/period, not per evidence permutation.

    Certificate conflicts are the exception: they only become an exclusion after
    a local observation, so each candidate gets its own identity and cannot
    displace immediately valid evidence about the same bond and period.
    """
    authority, conflict = proof.verify_detailed()
    resource = bond_resource(authority.bond, authority.period)
    if conflict is None:
        return resource
    return f"{resource}:{document_hash(proof)}"


class MarketListing(MarketModel):
    kind: Literal["listing"] = "listing"
    version: Literal[1] = 1
    network: Network
    period: int = Field(ge=0, le=65534)
    seller_pubkey: Pubkey
    encryption_pubkey: Hex32
    products: list[Product] = Field(min_length=1, max_length=2)
    price_sats: int = Field(ge=1, le=2100000000000000)
    expires_at: int = Field(ge=1, le=2**53 - 1)


def accept_listing(raw: bytes, network: str, now: int) -> tuple[SignedDocument, MarketListing]:
    signed = SignedDocument.model_validate(decode_document(raw))
    candidate = MarketListing.model_validate(signed.body)
    listing = signed.verified(MarketListing, candidate.seller_pubkey)
    if (
        listing.network != network
        or not now < listing.expires_at <= now + MAX_MARKET_LISTING_LIFETIME_SECONDS
    ):
        raise MarketError("Listing network or expiry mismatch")
    return signed, listing


class PaymentTerms(MarketModel):
    rail: Literal["lightning"]
    request: str = Field(min_length=1, max_length=4096)
    amount_sats: int = Field(ge=1, le=2100000000000000)


class MarketQuote(MarketModel):
    kind: Literal["quote"] = "quote"
    version: Literal[1] = 1
    authorization: SignedDocument
    quote_id: Hex32
    buyer_pubkey: Hex32
    product: Product
    resource: Hex32
    certificate_pubkey: Pubkey | None = None
    payment: PaymentTerms
    created_at: int = Field(ge=1, le=2**53 - 1)
    expires_at: int = Field(ge=1, le=2**53 - 1)

    def check(
        self, *, network: str, height: int, now: int, max_price_sats: int
    ) -> MarketAuthorization:
        authority = verify_authorization(self.authorization)
        if authority.bond.network != network or authority.period != period_at_height(height):
            raise MarketError("Quote network or retarget period mismatch")
        if not self.created_at <= now < self.expires_at <= self.created_at + 900:
            raise MarketError("Quote expired or has invalid reservation window")
        if self.payment.amount_sats > max_price_sats:
            raise MarketError("Quote exceeds buyer price limit")
        if self.product == "bond" and (
            self.resource != bond_resource(authority.bond, authority.period)
            or self.certificate_pubkey is None
        ):
            raise MarketError("Invalid bond quote")
        if self.product == "podle" and self.certificate_pubkey is not None:
            raise MarketError("Invalid PoDLE quote")
        return authority
