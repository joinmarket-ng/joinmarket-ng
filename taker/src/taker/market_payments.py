"""Validation helpers for experimental credential-market payment terms.

The market settles over Lightning only. These helpers validate payment requests
and settlements. They never send a payment or acknowledge delivery of a
credential.
"""

from __future__ import annotations

import hashlib
import hmac

from bech32 import CHARSET, bech32_decode
from jmcore.credential_market import PaymentTerms
from jmcore.models import NetworkType

from taker._vendor.bolt11 import decode
from taker._vendor.bolt11.models.features import FeatureExtra, FeatureState

_NETWORKS = frozenset({"mainnet", "testnet", "signet", "regtest"})
_LIGHTNING_CURRENCIES = {
    "mainnet": "bc",
    "testnet": "tb",
    # BOLT11 defines a distinct signet prefix (lntbs), separate from testnet.
    "signet": "tbs",
    "regtest": "bcrt",
}
_SINGLETON_TAGS = frozenset({"d", "h", "p", "s", "n", "f", "x", "c", "m", "9"})
_FIXED_LENGTH_TAGS = {"p": 52, "h": 52, "s": 52, "n": 53}


class PaymentValidationError(ValueError):
    """Raised when a payment request or claimed settlement cannot be accepted."""


def validate_payment_terms(
    terms: PaymentTerms, network: str | NetworkType, now: int, expires_at: int
) -> str:
    """Validate a Lightning payment request and return ``ln:<payment_hash>``.

    Any other rail is refused here, before a quote can reserve it or a buyer can
    be handed a payable destination.
    """
    network_value = _network_value(network)
    now_value = _timestamp(now, "now")
    quote_expiry = _timestamp(expires_at, "expires_at")
    if now_value >= quote_expiry:
        raise PaymentValidationError("Payment quote has expired")
    if terms.rail != "lightning":
        raise PaymentValidationError("Unsupported payment rail")
    return _validate_lightning_request(
        terms.request, terms.amount_sats, network_value, now_value, quote_expiry
    )


def verify_lightning_preimage(
    terms: PaymentTerms,
    network: str | NetworkType,
    preimage: bytes,
    now: int,
    expires_at: int,
) -> str:
    """Validate a Lightning preimage against an invoice payment hash.

    A preimage proves neither payer identity nor actual settlement. The invoice
    provider knows its preimage, so a seller must obtain explicit local-wallet
    acknowledgement before delivery. Never use a remotely supplied preimage to
    trigger credential delivery.
    """
    payment_id = validate_payment_terms(terms, network, now, expires_at)
    if not payment_id.startswith("ln:"):
        raise PaymentValidationError("Lightning settlement requires a Lightning payment request")
    if not isinstance(preimage, bytes) or len(preimage) != 32:
        raise PaymentValidationError("Lightning preimage must be exactly 32 bytes")

    payment_hash = payment_id.removeprefix("ln:")
    actual_hash = hashlib.sha256(preimage).hexdigest()
    if not hmac.compare_digest(actual_hash, payment_hash):
        raise PaymentValidationError("Lightning preimage does not match payment hash")
    return payment_id


def payment_uri(terms: PaymentTerms) -> str:
    """Return a displayable Lightning payment URI without mutating terms."""
    if terms.rail != "lightning":
        raise PaymentValidationError("Unsupported payment rail")
    return f"lightning:{terms.request}"


def _validate_lightning_request(
    request: str,
    amount_sats: int,
    network: str,
    now: int,
    quote_expiry: int,
) -> str:
    _validate_bolt11_structure(request)
    try:
        invoice = decode(request)
    except Exception as exc:
        raise PaymentValidationError("Invalid BOLT11 invoice") from exc

    if invoice.currency != _LIGHTNING_CURRENCIES[network]:
        raise PaymentValidationError("BOLT11 invoice network does not match payment network")
    if invoice.amount_msat is None:
        raise PaymentValidationError("BOLT11 invoice must specify an amount")
    if int(invoice.amount_msat) != amount_sats * 1_000:
        raise PaymentValidationError("BOLT11 invoice amount does not match payment terms")
    if invoice.date > now:
        raise PaymentValidationError("BOLT11 invoice timestamp is in the future")
    if now >= invoice.expiry_time:
        raise PaymentValidationError("BOLT11 invoice has expired")
    if invoice.expiry_time < quote_expiry:
        raise PaymentValidationError("BOLT11 invoice expires before the payment quote")

    features = invoice.features
    if features is not None:
        for feature, state in features.feature_list.items():
            if isinstance(feature, FeatureExtra) and state is FeatureState.required:
                raise PaymentValidationError("BOLT11 invoice requires an unsupported feature")

    try:
        payment_hash = invoice.payment_hash
        valid_hash = len(payment_hash) == 64 and bytes.fromhex(payment_hash)
    except (TypeError, ValueError) as exc:
        raise PaymentValidationError("Invalid BOLT11 payment hash") from exc
    if not valid_hash:
        raise PaymentValidationError("Invalid BOLT11 payment hash")
    return f"ln:{payment_hash.lower()}"


def _validate_bolt11_structure(request: str) -> None:
    """Reject singleton-tag ambiguity that the upstream parser intentionally skips."""
    if not isinstance(request, str) or not request or request != request.strip():
        raise PaymentValidationError("Invalid BOLT11 invoice")
    _, data = bech32_decode(request)
    if data is None or len(data) < 7 + 104:
        raise PaymentValidationError("Invalid BOLT11 invoice")

    tags_end = len(data) - 104  # BOLT11 signatures are always 65 bytes = 104 u5 values.
    position = 7  # Timestamp is 35 bits = 7 u5 values.
    seen: set[str] = set()
    while position < tags_end:
        if position + 3 > tags_end:
            raise PaymentValidationError("Invalid BOLT11 invoice")
        tag = CHARSET[data[position]]
        length = data[position + 1] * 32 + data[position + 2]
        position += 3
        if position + length > tags_end:
            raise PaymentValidationError("Invalid BOLT11 invoice")

        tag_data = data[position : position + length]
        if tag in _SINGLETON_TAGS and tag in seen:
            raise PaymentValidationError("BOLT11 invoice contains duplicate singleton tags")
        seen.add(tag)
        _validate_fixed_length_tag(tag, tag_data)
        position += length

    if position != tags_end:
        raise PaymentValidationError("Invalid BOLT11 invoice")


def _validate_fixed_length_tag(tag: str, tag_data: list[int]) -> None:
    expected_length = _FIXED_LENGTH_TAGS.get(tag)
    if expected_length is None:
        return
    if len(tag_data) != expected_length:
        raise PaymentValidationError("Invalid BOLT11 fixed-length tag")
    padding_bits = (len(tag_data) * 5) % 8
    if padding_bits and tag_data[-1] & ((1 << padding_bits) - 1):
        raise PaymentValidationError("Invalid BOLT11 fixed-length tag padding")


def _network_value(network: str | NetworkType) -> str:
    network_value = network.value if isinstance(network, NetworkType) else network
    if network_value not in _NETWORKS:
        raise PaymentValidationError("Unsupported payment network")
    return network_value


def _timestamp(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PaymentValidationError(f"{field} must be a non-negative integer")
    return value
