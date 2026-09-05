"""Validation helpers for experimental credential-market payment terms.

These helpers validate payment requests and settlements. They never send a
payment, broadcast a transaction, or acknowledge delivery of a credential.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Protocol

from bech32 import CHARSET, bech32_decode
from jmcore.bitcoin import address_to_scriptpubkey_for_network
from jmcore.credential_market import PaymentTerms
from jmcore.models import NetworkType

from taker._vendor.bolt11 import decode
from taker._vendor.bolt11.models.features import FeatureExtra, FeatureState

_NETWORKS = frozenset({"mainnet", "testnet", "signet", "regtest"})
_LIGHTNING_CURRENCIES = {
    "mainnet": "bc",
    "testnet": "tb",
    # BOLT11 uses lntb for both testnet and signet.
    "signet": "tb",
    "regtest": "bcrt",
}
_SINGLETON_TAGS = frozenset({"d", "h", "p", "s", "n", "f", "x", "c", "m", "9"})
_FIXED_LENGTH_TAGS = {"p": 52, "h": 52, "s": 52, "n": 53}
_TXID_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_HEX_RE = re.compile(r"[0-9a-fA-F]+\Z")


class PaymentValidationError(ValueError):
    """Raised when a payment request or claimed settlement cannot be accepted."""


class _UTXOLike(Protocol):
    @property
    def txid(self) -> object: ...

    @property
    def vout(self) -> object: ...

    @property
    def value(self) -> object: ...

    @property
    def confirmations(self) -> object: ...

    @property
    def scriptpubkey(self) -> object: ...


class _BlockchainBackendLike(Protocol):
    async def get_utxo(self, txid: str, vout: int) -> _UTXOLike | None: ...


def validate_payment_terms(
    terms: PaymentTerms, network: str | NetworkType, now: int, expires_at: int
) -> str:
    """Validate a payment request and return its stable payment identifier.

    Lightning identifiers are ``ln:<payment_hash>``. On-chain identifiers are
    ``chain:<network>:<scriptpubkey>`` and identify the requested destination,
    not a payment transaction.
    """
    network_value = _network_value(network)
    now_value = _timestamp(now, "now")
    quote_expiry = _timestamp(expires_at, "expires_at")
    if now_value >= quote_expiry:
        raise PaymentValidationError("Payment quote has expired")

    if terms.rail == "lightning":
        return _validate_lightning_request(
            terms.request, terms.amount_sats, network_value, now_value, quote_expiry
        )
    if terms.rail == "onchain":
        return _validate_onchain_request(terms, network_value)
    raise PaymentValidationError("Unsupported payment rail")


async def verify_onchain_payment(
    backend: _BlockchainBackendLike,
    terms: PaymentTerms,
    network: str | NetworkType,
    outpoint: str,
) -> str:
    """Verify an exact, unspent on-chain settlement output from backend authority."""
    network_value = _network_value(network)
    txid, vout = _parse_outpoint(outpoint)
    payment_id = _validate_onchain_request(terms, network_value)

    _, _, script_hex = payment_id.split(":", 2)
    expected_scriptpubkey = bytes.fromhex(script_hex)
    try:
        utxo = await backend.get_utxo(txid, vout)
    except Exception as exc:
        raise PaymentValidationError("Blockchain UTXO lookup failed") from exc
    if utxo is None:
        raise PaymentValidationError("Settlement output is unavailable or spent")

    _validate_backend_utxo(
        utxo,
        txid,
        vout,
        expected_scriptpubkey,
        terms.amount_sats,
        terms.min_confirmations,
    )
    return f"chain:{network_value}:{txid}:{vout}"


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
    """Return a displayable BIP21 or Lightning payment URI without mutating terms."""
    if terms.rail == "lightning":
        return f"lightning:{terms.request}"
    if terms.rail == "onchain":
        return f"bitcoin:{terms.request}?amount={_sats_to_btc(terms.amount_sats)}"
    raise PaymentValidationError("Unsupported payment rail")


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


def _validate_onchain_request(terms: PaymentTerms, network: str) -> str:
    if terms.rail != "onchain":
        raise PaymentValidationError("On-chain settlement requires an on-chain payment request")
    _positive_int(terms.min_confirmations, "min_confirmations")
    try:
        scriptpubkey = address_to_scriptpubkey_for_network(terms.request, network)
    except Exception as exc:
        raise PaymentValidationError("Invalid on-chain address for the selected network") from exc
    if not scriptpubkey:
        raise PaymentValidationError("Invalid on-chain address for the selected network")
    return f"chain:{network}:{scriptpubkey.hex()}"


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


def _validate_backend_utxo(
    utxo: _UTXOLike,
    txid: str,
    vout: int,
    expected_scriptpubkey: bytes,
    amount_sats: int,
    min_confirmations: int,
) -> None:
    backend_txid = _txid(utxo.txid, "backend UTXO txid")
    backend_vout = _nonnegative_int(utxo.vout, "backend UTXO vout")
    value = _positive_int(utxo.value, "backend UTXO value")
    confirmations = _nonnegative_int(utxo.confirmations, "backend UTXO confirmations")
    scriptpubkey = _scriptpubkey(utxo.scriptpubkey)

    if backend_txid != txid or backend_vout != vout:
        raise PaymentValidationError("Blockchain backend returned a different settlement output")
    if scriptpubkey != expected_scriptpubkey:
        raise PaymentValidationError("Settlement output script does not match payment request")
    if value != amount_sats:
        raise PaymentValidationError("Settlement output value does not exactly match payment terms")
    if confirmations < min_confirmations:
        raise PaymentValidationError("Settlement output has insufficient confirmations")


def _network_value(network: str | NetworkType) -> str:
    network_value = network.value if isinstance(network, NetworkType) else network
    if network_value not in _NETWORKS:
        raise PaymentValidationError("Unsupported payment network")
    return network_value


def _parse_outpoint(outpoint: str) -> tuple[str, int]:
    if not isinstance(outpoint, str):
        raise PaymentValidationError("Settlement outpoint must be txid:vout")
    parts = outpoint.split(":")
    if len(parts) != 2:
        raise PaymentValidationError("Settlement outpoint must be txid:vout")
    txid = _txid(parts[0], "settlement txid")
    return txid, _nonnegative_decimal_int(parts[1], "settlement vout", maximum=0xFFFFFFFF)


def _txid(value: object, field: str) -> str:
    if not isinstance(value, str) or _TXID_RE.fullmatch(value) is None:
        raise PaymentValidationError(f"Invalid {field}")
    return value.lower()


def _scriptpubkey(value: object) -> bytes:
    if not isinstance(value, str) or _HEX_RE.fullmatch(value) is None or len(value) % 2:
        raise PaymentValidationError("Invalid backend UTXO scriptpubkey")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise PaymentValidationError("Invalid backend UTXO scriptpubkey") from exc


def _timestamp(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PaymentValidationError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PaymentValidationError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PaymentValidationError(f"{field} must be a non-negative integer")
    return value


def _nonnegative_decimal_int(value: object, field: str, maximum: int) -> int:
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdecimal():
        raise PaymentValidationError(f"Invalid {field}")
    integer = int(value)
    if integer > maximum:
        raise PaymentValidationError(f"Invalid {field}")
    return integer


def _sats_to_btc(amount_sats: int) -> str:
    whole, fractional = divmod(amount_sats, 100_000_000)
    if fractional == 0:
        return str(whole)
    return f"{whole}.{fractional:08d}".rstrip("0")
