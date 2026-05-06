"""WabiSabi-style anonymous credential primitives for ZKP coordination.

Thin Pythonic wrapper over the ``nwabisabi`` Rust extension module
(maturin-built, abi3-py311). Keeps the Rust crate as the single source
of truth for the wire format while giving the rest of the codebase a
typed, dataclass-friendly surface to work with.

Why a wrapper instead of using the Rust types directly?
    * The Rust binding uses opaque ``bytes`` for every wire DTO so the
      crate stays Python-agnostic. The wrapper assigns names + types to
      those bytes (``IssuerParameters``, ``ZeroAmountRequest``, ...)
      so callers do not pass raw blobs around the codebase.
    * ``ValidationHandle`` is non-serializable by design (holds a
      Strobe state). The wrapper keeps it inside ``CredentialRequest``
      so callers cannot accidentally drop it before validation.
    * Defaults are pinned to JMP-0005's parameters (``MAX_AMOUNT =
      2**51``, ``RANGE_PROOF_WIDTH = 51``) instead of the crate's
      Wasabi-tuned defaults (``2**27``).

The Rust extension exposes a fixed two-credential vector
(``CREDENTIAL_NUMBER = 2``) at the protocol layer; that is reflected
in :data:`CREDENTIAL_NUMBER` here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# The Rust extension is built from the sibling ``nwabisabi`` crate
# (cargo feature ``python``) and installed as a Python wheel via
# maturin. Importing it here lets mypy + ruff fail fast if the wheel
# is missing instead of deferring to runtime.
import nwabisabi as _rs

# JMP-0005: 51-bit Bulletproof range, max credential value = 2^51 sat.
# Pinned here (and not at the call site) so a rounding mistake in one
# component cannot disagree with another.
MAX_AMOUNT: Final[int] = (1 << 51) - 1
RANGE_PROOF_WIDTH: Final[int] = 51

# Fixed by the protocol (CMZ'14 + WabiSabi composition); changing this
# requires a coordinated upstream change in the Rust crate.
CREDENTIAL_NUMBER: Final[int] = 2


# Wire DTOs --------------------------------------------------------------
#
# Each is a ``NewType``-style wrapper around ``bytes`` rather than a
# plain alias so that mypy catches accidental crossings (e.g. passing
# a response where a request is expected). The dataclass-with-slots
# pattern keeps the per-instance overhead at one ``bytes`` reference.


@dataclass(frozen=True, slots=True)
class IssuerParameters:
    """Bincode-encoded ``CredentialIssuerParameters`` blob."""

    raw: bytes


@dataclass(frozen=True, slots=True)
class IssuerSecretKey:
    """Bincode-encoded ``CredentialIssuerSecretKey`` blob."""

    raw: bytes


@dataclass(frozen=True, slots=True)
class ZeroAmountRequest:
    """Wire payload for input registration (zero credentials, real inputs)."""

    raw: bytes


@dataclass(frozen=True, slots=True)
class RealAmountRequest:
    """Wire payload for output registration / reissuance (real credentials)."""

    raw: bytes


@dataclass(frozen=True, slots=True)
class CredentialsResponse:
    """Coordinator-to-client response containing the freshly issued MACs."""

    raw: bytes


# Issued credentials ------------------------------------------------------


class Credential:
    """Wallet-side handle to a freshly issued credential.

    Wraps the opaque Rust ``Credential`` class. Persistable via
    :meth:`to_bytes` / :meth:`from_bytes` so a wallet restart between
    rounds does not drop in-flight credentials.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: _rs.Credential) -> None:
        self._inner = inner

    @classmethod
    def from_bytes(cls, blob: bytes) -> Credential:
        return cls(_rs.Credential.from_bytes(blob))

    def to_bytes(self) -> bytes:
        return self._inner.to_bytes()

    @property
    def value(self) -> int:
        return self._inner.value()

    # Internal accessor for the wrapper to hand the raw object back to
    # the Rust client when building a real-amount request. Not part of
    # the public API.
    def _native(self) -> _rs.Credential:
        return self._inner


# Round-trip request bundle ----------------------------------------------


@dataclass(frozen=True, slots=True)
class CredentialRequest:
    """A request paired with the validation handle needed to verify the response.

    The handle is single-use: passing the request to the coordinator and
    later calling :meth:`Client.handle_response` consumes it. The
    binding raises ``RuntimeError`` on a second use, but it is the
    caller's responsibility to scope the bundle so the handle does not
    outlive the round trip.

    For zero-amount requests use the :class:`ZeroAmountRequest` variant
    via the ``Client.request_zero_amount`` helper; for real amounts use
    the :class:`RealAmountRequest` variant.
    """

    payload: ZeroAmountRequest | RealAmountRequest
    validation: _rs.ValidationHandle  # opaque, non-serializable


# Issuer (coordinator side) ----------------------------------------------


class Issuer:
    """Coordinator-side credential issuer.

    Owns a long-lived secret key and a per-round balance. ``reset`` is
    intended to be called once per CoinJoin round so issuer state does
    not leak across coordination sessions.

    Issuer and client *must* agree on ``max_amount`` /
    ``range_proof_width`` or the issuer rejects every real-amount
    request with ``RuntimeError: Invalid bit commitment``. The
    defaults pin both sides to JMP-0005's ``2**51`` so the wrapper is
    safe out of the box.
    """

    __slots__ = ("_inner",)

    def __init__(
        self,
        secret_key: IssuerSecretKey,
        initial_balance: int = 0,
        max_amount: int = MAX_AMOUNT,
        range_proof_width: int = RANGE_PROOF_WIDTH,
    ) -> None:
        inner = _rs.CredentialIssuer(secret_key.raw, initial_balance)
        inner.configure(max_amount, range_proof_width)
        self._inner = inner

    @classmethod
    def generate(
        cls,
        initial_balance: int = 0,
        max_amount: int = MAX_AMOUNT,
        range_proof_width: int = RANGE_PROOF_WIDTH,
    ) -> Issuer:
        """Build an issuer from a freshly generated secret key."""
        return cls(
            IssuerSecretKey(_rs.generate_issuer_secret_key()),
            initial_balance,
            max_amount,
            range_proof_width,
        )

    def parameters(self) -> IssuerParameters:
        return IssuerParameters(self._inner.parameters())

    @property
    def balance(self) -> int:
        return self._inner.balance()

    def reset(self, new_balance: int = 0) -> None:
        self._inner.reset(new_balance)

    def handle_zero_amount(self, request: ZeroAmountRequest) -> CredentialsResponse:
        """Issue zero-value credentials for an input registration."""
        return CredentialsResponse(self._inner.handle_request(request.raw, False))

    def handle_real_amount(self, request: RealAmountRequest) -> CredentialsResponse:
        """Issue real-value credentials for an output registration."""
        return CredentialsResponse(self._inner.handle_request(request.raw, True))


def derive_issuer_parameters(secret_key: IssuerSecretKey) -> IssuerParameters:
    """Compute the public parameters for a secret key without instantiating an issuer."""
    return IssuerParameters(_rs.derive_issuer_parameters(secret_key.raw))


# Client (maker / taker side) --------------------------------------------


class Client:
    """Maker- or taker-side WabiSabi client.

    Configured against a coordinator's :class:`IssuerParameters` and
    pinned to JMP-0005's ``MAX_AMOUNT`` / ``RANGE_PROOF_WIDTH`` by
    default. Re-configuration is exposed for tests; production callers
    should not deviate from the spec defaults.
    """

    __slots__ = ("_inner",)

    def __init__(
        self,
        parameters: IssuerParameters,
        max_amount: int = MAX_AMOUNT,
        range_proof_width: int = RANGE_PROOF_WIDTH,
    ) -> None:
        client = _rs.WabiSabiClient(parameters.raw)
        client.configure(max_amount, range_proof_width)
        self._inner = client

    def request_zero_amount(self) -> CredentialRequest:
        """Build a zero-amount request (input registration phase)."""
        payload, handle = self._inner.create_request_for_zero_amount()
        return CredentialRequest(ZeroAmountRequest(payload), handle)

    def request_real_amount(
        self,
        amounts: list[int],
        credentials_to_present: list[Credential],
    ) -> CredentialRequest:
        """Build a real-amount request (output registration / reissuance).

        ``amounts`` must be exactly :data:`CREDENTIAL_NUMBER` non-negative
        values summing to ``sum(c.value for c in credentials_to_present)``.
        Validation of those invariants happens inside the Rust binding;
        we delegate rather than duplicating the check here so the error
        message stays authoritative.
        """
        payload, handle = self._inner.create_request(
            amounts,
            [c._native() for c in credentials_to_present],  # noqa: SLF001
        )
        return CredentialRequest(RealAmountRequest(payload), handle)

    def handle_response(
        self,
        request: CredentialRequest,
        response: CredentialsResponse,
    ) -> list[Credential]:
        """Validate a coordinator response and return the issued credentials."""
        raw = self._inner.handle_response(response.raw, request.validation)
        return [Credential(c) for c in raw]


__all__ = [
    "CREDENTIAL_NUMBER",
    "Client",
    "Credential",
    "CredentialRequest",
    "CredentialsResponse",
    "Issuer",
    "IssuerParameters",
    "IssuerSecretKey",
    "MAX_AMOUNT",
    "RANGE_PROOF_WIDTH",
    "RealAmountRequest",
    "ZeroAmountRequest",
    "derive_issuer_parameters",
]
