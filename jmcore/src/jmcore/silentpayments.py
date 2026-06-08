"""
Silent Payments (BIP352) primitives for JoinMarket.

This module implements the cryptographic core of BIP352 Silent Payments:

- ``sp``/``tsp`` bech32m address encoding and decoding
- Receiver key handling (scan/spend key pairs)
- Sender-side output derivation (``create_outputs``)
- Receiver-side scanning of transactions (``scan_transaction``)
- Extraction of input public keys from the BIP352 "Inputs For Shared Secret
  Derivation" list (P2TR, P2WPKH, P2SH-P2WPKH, P2PKH)

The module intentionally operates on raw key material (32-byte scalars and
33-byte compressed / 32-byte x-only public keys) so that it stays independent
of the HD wallet layer. BIP32 key derivation (``m/352'/coin'/account'/...``)
lives in the wallet component, which feeds the derived scan/spend keys here.

All elliptic-curve operations use ``coincurve`` (libsecp256k1), matching the
rest of jmcore. Bech32m encoding is implemented locally because the pinned
``bech32`` dependency only ships the BIP173 (bech32) checksum constant and
enforces the 90-character segwit limit, neither of which works for the
117+ character silent payment addresses.

Reference: https://github.com/bitcoin/bips/blob/master/bip-0352.mediawiki
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from bech32 import CHARSET, bech32_hrp_expand, bech32_polymod, convertbits
from coincurve import PublicKey
from pydantic import BaseModel, ConfigDict, field_validator

from jmcore.bitcoin import NetworkType, decode_varint, tagged_hash
from jmcore.constants import SECP256K1_N

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# Per-group recipient limit (BIP352). Scanners stop incrementing k at this value.
K_MAX = 2323

# NUMS point H from BIP341, used as the "provably unspendable" taproot internal
# key. Taproot script-path inputs using H are skipped for shared-secret derivation.
NUMS_H = bytes.fromhex("50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0")

# Human-readable prefixes for silent payment addresses (BIP352).
_HRP_MAINNET = "sp"
_HRP_TESTNET = "tsp"


class SilentPaymentError(Exception):
    """Raised on invalid silent payment data or unsupported operations."""


# =============================================================================
# bech32m (BIP350) encoding, without the 90-char segwit limit
# =============================================================================

_BECH32M_CONST = 0x2BC830A3


def _bech32m_create_checksum(hrp: str, data: Sequence[int]) -> list[int]:
    values = bech32_hrp_expand(hrp) + list(data)
    polymod = bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ _BECH32M_CONST
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _bech32m_encode(hrp: str, data: Sequence[int]) -> str:
    combined = list(data) + _bech32m_create_checksum(hrp, data)
    return hrp + "1" + "".join(CHARSET[d] for d in combined)


def _bech32m_decode(bech: str) -> tuple[str, list[int]]:
    if any(ord(c) < 33 or ord(c) > 126 for c in bech):
        raise SilentPaymentError("Invalid characters in silent payment address")
    if bech.lower() != bech and bech.upper() != bech:
        raise SilentPaymentError("Mixed case silent payment address")
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        raise SilentPaymentError("Invalid silent payment address separator")
    hrp = bech[:pos]
    try:
        data = [CHARSET.find(c) for c in bech[pos + 1 :]]
    except ValueError as exc:  # pragma: no cover - defensive
        raise SilentPaymentError("Invalid silent payment address data") from exc
    if -1 in data:
        raise SilentPaymentError("Invalid character in silent payment address data")
    values = bech32_hrp_expand(hrp) + data
    if bech32_polymod(values) != _BECH32M_CONST:
        raise SilentPaymentError("Invalid silent payment address checksum")
    return hrp, data[:-6]


# =============================================================================
# Scalar / point helpers (coincurve)
# =============================================================================


def _is_valid_scalar(value: int) -> bool:
    return 0 < value < SECP256K1_N


def _scalar_to_bytes(value: int) -> bytes:
    return (value % SECP256K1_N).to_bytes(32, "big")


def _privkey_to_pubkey(scalar: int) -> PublicKey:
    return PublicKey.from_secret(_scalar_to_bytes(scalar))


def _scalar_mul_point(scalar: int, point: PublicKey) -> PublicKey:
    if not _is_valid_scalar(scalar % SECP256K1_N):
        raise SilentPaymentError("Scalar out of range for point multiplication")
    return point.multiply(_scalar_to_bytes(scalar))


def _negate_point(point: PublicKey) -> PublicKey:
    return point.multiply((SECP256K1_N - 1).to_bytes(32, "big"))


def _sum_points(points: Iterable[PublicKey]) -> PublicKey | None:
    """Sum public keys. Returns ``None`` if the result is the point at infinity."""
    points = list(points)
    if not points:
        return None
    try:
        return points[0].combine(points[1:])
    except ValueError:
        return None


def _xonly(point: PublicKey) -> bytes:
    return point.format(compressed=True)[1:]


def _has_even_y(point: PublicKey) -> bool:
    return point.format(compressed=True)[0] == 0x02


def _lift_x(xonly: bytes) -> PublicKey:
    """Lift a 32-byte x-only key to the point with even Y.

    Raises ``SilentPaymentError`` (never a bare ``ValueError``) when ``xonly`` is
    not a valid curve point, so callers scanning attacker-controlled outputs can
    skip them instead of crashing.
    """
    if len(xonly) != 32:
        raise SilentPaymentError("x-only public key must be 32 bytes")
    try:
        return PublicKey(b"\x02" + xonly)
    except ValueError as exc:
        raise SilentPaymentError("x-only key is not a valid curve point") from exc


def _ser_uint32(value: int) -> bytes:
    return value.to_bytes(4, "big")


# =============================================================================
# Input handling
# =============================================================================


class SilentPaymentInput(BaseModel):
    """A transaction input used for silent payment shared-secret derivation.

    Fields mirror the data available from a parsed transaction / prevout. The
    ``txid`` is the big-endian (display) transaction id, matching wallet UTXO
    references; the outpoint serialization reverses it internally per BIP352.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    txid: str
    vout: int
    scriptpubkey: bytes
    script_sig: bytes = b""
    witness: list[bytes] = []
    private_key: int | None = None

    @field_validator("txid")
    @classmethod
    def _check_txid(cls, value: str) -> str:
        if len(value) != 64:
            raise ValueError("txid must be 32-byte hex")
        bytes.fromhex(value)
        return value

    def outpoint(self) -> bytes:
        """36-byte COutPoint: txid (internal little-endian) || vout (LE)."""
        return bytes.fromhex(self.txid)[::-1] + self.vout.to_bytes(4, "little")

    def is_taproot(self) -> bool:
        spk = self.scriptpubkey
        return len(spk) == 34 and spk[0] == 0x51 and spk[1] == 0x20


def parse_witness(witness_hex: str | bytes) -> list[bytes]:
    """Deserialize a serialized input witness into its stack items."""
    data = bytes.fromhex(witness_hex) if isinstance(witness_hex, str) else witness_hex
    if not data:
        return []
    offset = 0
    count, consumed = decode_varint(data[offset:])
    offset += consumed
    stack: list[bytes] = []
    for _ in range(count):
        length, consumed = decode_varint(data[offset:])
        offset += consumed
        stack.append(data[offset : offset + length])
        offset += length
    return stack


def _is_p2pkh(spk: bytes) -> bool:
    return (
        len(spk) == 25
        and spk[0] == 0x76
        and spk[1] == 0xA9
        and spk[2] == 0x14
        and spk[23] == 0x88
        and spk[24] == 0xAC
    )


def _is_p2sh(spk: bytes) -> bool:
    return len(spk) == 23 and spk[0] == 0xA9 and spk[1] == 0x14 and spk[22] == 0x87


def _is_p2wpkh(spk: bytes) -> bool:
    return len(spk) == 22 and spk[0] == 0x00 and spk[1] == 0x14


def _is_p2tr(spk: bytes) -> bool:
    return len(spk) == 34 and spk[0] == 0x51 and spk[1] == 0x20


def extract_input_pubkey(vin: SilentPaymentInput) -> PublicKey | None:
    """Extract the public key used for shared-secret derivation from an input.

    Returns ``None`` for inputs that are not on the BIP352 "Inputs For Shared
    Secret Derivation" list, that use uncompressed keys, or that use the NUMS
    point H as their taproot internal key.
    """
    spk = vin.scriptpubkey

    if _is_p2pkh(spk):
        spk_hash = spk[3:23]
        sig = vin.script_sig
        # Scan the scriptSig with a 33-byte window for the compressed pubkey,
        # tolerating non-standard (malleated) scriptSigs per BIP352.
        for i in range(len(sig), 0, -1):
            if i - 33 >= 0:
                candidate = sig[i - 33 : i]
                if (
                    hashlib.new("ripemd160", hashlib.sha256(candidate).digest()).digest()
                    == spk_hash
                ):
                    try:
                        return PublicKey(candidate)
                    except Exception:  # noqa: BLE001 - invalid candidate, keep scanning
                        continue
        return None

    if _is_p2sh(spk):
        redeem_script = vin.script_sig[1:] if vin.script_sig else b""
        if _is_p2wpkh(redeem_script) and vin.witness:
            return _compressed_pubkey_or_none(vin.witness[-1])
        return None

    if _is_p2wpkh(spk):
        if vin.witness:
            return _compressed_pubkey_or_none(vin.witness[-1])
        return None

    if _is_p2tr(spk):
        stack = list(vin.witness)
        if not stack:
            return None
        if len(stack) > 1 and stack[-1] and stack[-1][0] == 0x50:
            # Drop the annex.
            stack.pop()
        if len(stack) > 1:
            control_block = stack[-1]
            internal_key = control_block[1:33]
            if internal_key == NUMS_H:
                return None
        try:
            return _lift_x(spk[2:])
        except SilentPaymentError:
            return None

    return None


def _compressed_pubkey_or_none(data: bytes) -> PublicKey | None:
    if len(data) != 33:
        return None
    try:
        return PublicKey(data)
    except Exception:  # noqa: BLE001 - invalid pubkey
        return None


def compute_input_hash(outpoints: Sequence[bytes], sum_pubkey: PublicKey) -> int:
    """Compute ``input_hash`` = hash_BIP0352/Inputs(outpoint_L || A) as a scalar."""
    lowest = min(outpoints)
    digest = tagged_hash("BIP0352/Inputs", lowest + sum_pubkey.format(compressed=True))
    return int.from_bytes(digest, "big")


# =============================================================================
# Addresses
# =============================================================================


def _hrp_for_network(network: str | NetworkType) -> str:
    network = NetworkType(network) if isinstance(network, str) else network
    return _HRP_MAINNET if network == NetworkType.MAINNET else _HRP_TESTNET


class SilentPaymentAddress(BaseModel):
    """A decoded silent payment address (scan and spend public keys)."""

    model_config = ConfigDict(frozen=True)

    scan_pubkey: bytes
    spend_pubkey: bytes

    @field_validator("scan_pubkey", "spend_pubkey")
    @classmethod
    def _check_pubkey(cls, value: bytes) -> bytes:
        if len(value) != 33:
            raise ValueError("public key must be 33-byte compressed")
        PublicKey(value)  # validates point is on curve
        return value

    def encode(self, network: str | NetworkType = "mainnet", version: int = 0) -> str:
        """Encode as an ``sp1``/``tsp1`` bech32m silent payment address."""
        payload = self.scan_pubkey + self.spend_pubkey
        data = convertbits(payload, 8, 5)
        if data is None:  # pragma: no cover - defensive
            raise SilentPaymentError("Failed to convert payload bits")
        return _bech32m_encode(_hrp_for_network(network), [version, *data])

    @classmethod
    def decode(cls, address: str) -> tuple[SilentPaymentAddress, str]:
        """Decode an address, returning the address and its HRP (``sp``/``tsp``)."""
        hrp, data = _bech32m_decode(address)
        if hrp not in (_HRP_MAINNET, _HRP_TESTNET):
            raise SilentPaymentError(f"Unexpected silent payment HRP: {hrp}")
        if not data:
            raise SilentPaymentError("Empty silent payment address data")
        version = data[0]
        decoded = convertbits(data[1:], 5, 8, False)
        if decoded is None:
            raise SilentPaymentError("Invalid silent payment address payload")
        payload = bytes(decoded)
        if version == 0:
            if len(payload) != 66:
                raise SilentPaymentError("v0 silent payment payload must be 66 bytes")
        elif version == 31:
            raise SilentPaymentError("Silent payment version 31 is reserved")
        else:
            payload = payload[:66]
            if len(payload) < 66:
                raise SilentPaymentError("Silent payment payload too short")
        return cls(scan_pubkey=payload[:33], spend_pubkey=payload[33:66]), hrp


def derive_silent_payment_address(
    scan_pubkey: bytes,
    spend_pubkey: bytes,
    network: str | NetworkType = "mainnet",
) -> str:
    """Build a silent payment address string from raw scan/spend public keys."""
    return SilentPaymentAddress(scan_pubkey=scan_pubkey, spend_pubkey=spend_pubkey).encode(network)


def is_silent_payment_address(address: str) -> bool:
    """Return ``True`` if ``address`` is a well-formed silent payment address.

    Detects ``sp1``/``tsp1`` (BIP352) addresses by decoding them. Used to reject
    silent payment destinations for CoinJoins: a BIP352 receiver derives the
    output key from the sum of *all* of a transaction's inputs, but in a CoinJoin
    the inputs come from several parties whose private keys no single sender
    knows, so a silent payment cannot be paid through a CoinJoin (see JMP-0005).
    """
    if not isinstance(address, str):
        return False
    lowered = address.lower()
    if not (lowered.startswith(f"{_HRP_MAINNET}1") or lowered.startswith(f"{_HRP_TESTNET}1")):
        return False
    try:
        SilentPaymentAddress.decode(address)
    except (SilentPaymentError, ValueError):
        return False
    return True


def create_label_tweak(scan_privkey: int, m: int) -> int:
    """Compute the label tweak scalar hash_BIP0352/Label(ser256(b_scan) || ser32(m))."""
    digest = tagged_hash("BIP0352/Label", _scalar_to_bytes(scan_privkey) + _ser_uint32(m))
    return int.from_bytes(digest, "big") % SECP256K1_N


def create_labeled_address(
    scan_privkey: int,
    spend_pubkey: bytes,
    m: int,
    network: str | NetworkType = "mainnet",
) -> str:
    """Create a labeled silent payment address for label integer ``m``."""
    scan_pub = _privkey_to_pubkey(scan_privkey)
    label_point = _privkey_to_pubkey(create_label_tweak(scan_privkey, m))
    b_m = PublicKey(spend_pubkey).combine([label_point])
    return SilentPaymentAddress(
        scan_pubkey=scan_pub.format(compressed=True),
        spend_pubkey=b_m.format(compressed=True),
    ).encode(network)


# =============================================================================
# Sender
# =============================================================================


def create_outputs(
    input_private_keys: Sequence[tuple[int, bool]],
    outpoints: Sequence[bytes],
    recipients: Sequence[SilentPaymentAddress],
) -> list[bytes]:
    """Derive silent payment taproot outputs (x-only keys) for ``recipients``.

    Args:
        input_private_keys: ``(scalar, is_taproot)`` pairs for each eligible
            input. Taproot keys are negated to even-Y parity per BIP352.
        outpoints: 36-byte serialized outpoints of all transaction inputs.
        recipients: decoded silent payment addresses (may repeat / share scan keys).

    Returns:
        List of 32-byte x-only taproot output keys. Empty list on failure
        (e.g. private key sum is zero or a group exceeds K_max).
    """
    negated: list[int] = []
    for scalar, is_taproot in input_private_keys:
        k = scalar % SECP256K1_N
        if is_taproot and not _has_even_y(_privkey_to_pubkey(k)):
            k = SECP256K1_N - k
        negated.append(k)

    a_sum = sum(negated) % SECP256K1_N
    if a_sum == 0:
        return []

    input_hash = compute_input_hash(outpoints, _privkey_to_pubkey(a_sum))
    if not _is_valid_scalar(input_hash):
        return []

    # Group recipient spend keys by scan key, preserving order.
    groups: dict[bytes, list[bytes]] = {}
    for recipient in recipients:
        groups.setdefault(recipient.scan_pubkey, []).append(recipient.spend_pubkey)

    if any(len(spend_keys) > K_MAX for spend_keys in groups.values()):
        return []

    outputs: list[bytes] = []
    for scan_pubkey, spend_keys in groups.items():
        scan_point = PublicKey(scan_pubkey)
        ecdh = _scalar_mul_point((input_hash * a_sum) % SECP256K1_N, scan_point)
        ecdh_ser = ecdh.format(compressed=True)
        for k, spend_pubkey in enumerate(spend_keys):
            t_k = int.from_bytes(
                tagged_hash("BIP0352/SharedSecret", ecdh_ser + _ser_uint32(k)), "big"
            )
            if not _is_valid_scalar(t_k):
                return []
            p_km = PublicKey(spend_pubkey).combine([_privkey_to_pubkey(t_k)])
            outputs.append(_xonly(p_km))
    return outputs


# =============================================================================
# Receiver
# =============================================================================


class FoundOutput(BaseModel):
    """A silent payment output detected during scanning."""

    model_config = ConfigDict(frozen=True)

    pubkey_xonly: bytes
    tweak: int
    label_tweak: int = 0

    def output_private_key(self, spend_privkey: int) -> int:
        """Full private key (b_spend + tweak + label_tweak) mod n for spending."""
        return (spend_privkey + self.tweak + self.label_tweak) % SECP256K1_N


def compute_ecdh_secret(scan_privkey: int, input_hash: int, sum_pubkey: PublicKey) -> PublicKey:
    """ecdh_shared_secret = input_hash * b_scan * A_sum."""
    return _scalar_mul_point((input_hash * scan_privkey) % SECP256K1_N, sum_pubkey)


def scan_transaction(
    scan_privkey: int,
    spend_pubkey: bytes,
    inputs: Sequence[SilentPaymentInput],
    taproot_outputs: Sequence[bytes],
    labels: dict[bytes, int] | None = None,
) -> list[FoundOutput]:
    """Scan a transaction for silent payment outputs belonging to the receiver.

    Args:
        scan_privkey: the receiver's scan private key (``b_scan``).
        spend_pubkey: the receiver's spend public key (``B_spend``, 33 bytes).
        inputs: all transaction inputs (non-eligible ones are ignored).
        taproot_outputs: x-only (32-byte) taproot output keys of the transaction.
        labels: optional precomputed map of label point (33-byte compressed) ->
            label tweak scalar. The change label (m=0) should be included.

    Returns:
        List of detected outputs with their per-output tweak (and label tweak).
    """
    eligible = [pk for vin in inputs if (pk := extract_input_pubkey(vin)) is not None]
    if not eligible:
        return []
    sum_pubkey = _sum_points(eligible)
    if sum_pubkey is None:
        return []

    input_hash = compute_input_hash([vin.outpoint() for vin in inputs], sum_pubkey)
    if not _is_valid_scalar(input_hash):
        return []

    ecdh = compute_ecdh_secret(scan_privkey, input_hash, sum_pubkey)
    ecdh_ser = ecdh.format(compressed=True)
    spend_point = PublicKey(spend_pubkey)
    labels = labels or {}

    remaining = list(taproot_outputs)
    found: list[FoundOutput] = []
    k = 0
    while remaining and k < K_MAX:
        t_k = int.from_bytes(tagged_hash("BIP0352/SharedSecret", ecdh_ser + _ser_uint32(k)), "big")
        if not _is_valid_scalar(t_k):
            break
        p_k = spend_point.combine([_privkey_to_pubkey(t_k)])
        p_k_xonly = _xonly(p_k)
        match = _match_output(p_k, p_k_xonly, t_k, remaining, labels)
        if match is None:
            break
        output, found_output = match
        remaining.remove(output)
        found.append(found_output)
        k += 1
    return found


def _match_output(
    p_k: PublicKey,
    p_k_xonly: bytes,
    t_k: int,
    outputs: Sequence[bytes],
    labels: dict[bytes, int],
) -> tuple[bytes, FoundOutput] | None:
    for output in outputs:
        if output == p_k_xonly:
            return output, FoundOutput(pubkey_xonly=output, tweak=t_k)
        if not labels:
            continue
        # A non-curve x-only output can never be a (labeled) silent payment
        # output; skip it instead of letting an attacker-mined output crash the
        # whole scan (and thus block detection of later blocks).
        try:
            output_point = _lift_x(output)
        except SilentPaymentError:
            continue
        # label_point = output - P_k, then retry with the negated output (Y parity).
        for candidate_point in (output_point, _negate_point(output_point)):
            label_point = candidate_point.combine([_negate_point(p_k)])
            label_ser = label_point.format(compressed=True)
            if label_ser in labels:
                p_km = p_k.combine([label_point])
                return output, FoundOutput(
                    pubkey_xonly=_xonly(p_km),
                    tweak=t_k,
                    label_tweak=labels[label_ser],
                )
    return None
