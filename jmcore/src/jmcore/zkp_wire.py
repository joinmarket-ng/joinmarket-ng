"""Wire codecs for JMP-0005 ``!zkpparams`` PRIVMSG bodies.

JMP-0005 Phase ZK-1 has the taker broadcast the WabiSabi credential
issuer parameters to every maker right after ``!pubkey`` and before
``!auth``. The spec freezes the wire as::

    !zkpparams <epoch_id_hex> <issuer_pubkey_hex>

The two tokens are whitespace-separated hex strings:

* ``epoch_id_hex`` is exactly 64 hex chars (a uniformly random 32-byte
  identifier scoping every credential issued in this run).
* ``issuer_pubkey_hex`` is the bincode-serialised
  ``CredentialIssuerParameters`` blob from the ``nwabisabi`` Rust
  crate, hex-encoded. The frozen JMP-0005 wire calls this field
  ``issuer_pubkey_hex`` and documents it as a 33-byte compressed
  pubkey ``X = x * G``, but the WabiSabi reference implementation
  needs the full parameter set (``X`` plus a per-issuer auxiliary
  generator) for the maker-side client to construct credential
  requests. We carry the full ``derive_issuer_parameters`` output
  (currently 66 bytes) so makers can plug it straight into
  ``WabiSabiClient(parameters_bytes)`` without a separate negotiation
  step. Decoders accept any non-empty hex blob and defer the actual
  KVAC validation to the credential-handling layer; this keeps the
  wire codec deliberately untyped about the inner cryptographic
  structure.

Format choice (hex + whitespace) follows the precedent set by
:mod:`jmcore.attestation_wire`: the values are fixed-width hex
quantities and the parser collapses to a single ``str.split``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

EPOCH_ID_LEN: Final[int] = 32
_EPOCH_ID_HEX_LEN: Final[int] = EPOCH_ID_LEN * 2


@dataclass(frozen=True)
class ZkpParamsPayload:
    """Decoded ``!zkpparams`` body."""

    epoch_id: bytes
    issuer_params: bytes


class ZkpWireError(ValueError):
    """Wire-format-level decoding failure for ZKP messages.

    A dedicated subclass lets the protocol handler distinguish "peer
    sent malformed ``!zkpparams``" from "our caller passed bad
    arguments". Messages are short, deterministic, and never carry
    secrets so they can be logged verbatim.
    """


def encode_zkpparams(payload: ZkpParamsPayload) -> str:
    """Serialise a :class:`ZkpParamsPayload` to its on-wire string.

    Validates input shape so misuse (wrong-length epoch, empty issuer
    blob) surfaces at encode time rather than as opaque parser errors
    on the receiving side.
    """
    if len(payload.epoch_id) != EPOCH_ID_LEN:
        raise ZkpWireError(f"epoch_id must be {EPOCH_ID_LEN} bytes, got {len(payload.epoch_id)}")
    if not payload.issuer_params:
        raise ZkpWireError("issuer_params must be non-empty")
    return f"{payload.epoch_id.hex()} {payload.issuer_params.hex()}"


def decode_zkpparams(wire: str) -> ZkpParamsPayload:
    """Parse a ``!zkpparams`` body produced by :func:`encode_zkpparams`.

    The decoder is intentionally permissive about the issuer blob:
    we cannot verify its KVAC structure without a full
    ``WabiSabiClient`` round-trip, and rejecting at the wire layer
    would couple this module to the credential implementation. The
    caller is expected to attempt construction of a client and treat
    failure there as a "drop this peer" event, exactly like an
    ``!ioauth`` signature failure.
    """
    if not wire or not wire.strip():
        raise ZkpWireError("empty zkpparams body")
    tokens = wire.split()
    if len(tokens) != 2:
        raise ZkpWireError(f"expected 2 tokens, got {len(tokens)}")
    epoch_hex, params_hex = tokens
    if len(epoch_hex) != _EPOCH_ID_HEX_LEN:
        raise ZkpWireError(f"epoch_id_hex must be {_EPOCH_ID_HEX_LEN} chars, got {len(epoch_hex)}")
    try:
        epoch_id = bytes.fromhex(epoch_hex)
    except ValueError as exc:
        raise ZkpWireError(f"epoch_id_hex not valid hex: {exc}") from exc
    if not params_hex:
        raise ZkpWireError("issuer_pubkey_hex must be non-empty")
    try:
        issuer_params = bytes.fromhex(params_hex)
    except ValueError as exc:
        raise ZkpWireError(f"issuer_pubkey_hex not valid hex: {exc}") from exc
    return ZkpParamsPayload(epoch_id=epoch_id, issuer_params=issuer_params)
