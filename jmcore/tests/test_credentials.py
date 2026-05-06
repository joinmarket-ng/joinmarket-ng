"""Unit tests for the WabiSabi credentials wrapper.

These exercise the full client/issuer round trip through the Rust
extension to make sure the typed Python surface stays in sync with the
binding. Skipped automatically when the Rust extension is not built so
``pytest`` keeps working in environments where the wheel hasn't been
installed yet (e.g. a fresh ``pip install -e jmcore`` without
``maturin develop`` in the sibling crate).
"""

from __future__ import annotations

import pytest

# The binding is an optional build-time dependency: jmcore the library
# imports it lazily, but the test suite uses real round trips. Skip
# (rather than fail) if the wheel hasn't been installed so contributors
# without a Rust toolchain can still run the rest of jmcore's tests.
nwabisabi = pytest.importorskip("nwabisabi", reason="nwabisabi extension not built")

from jmcore.credentials import (  # noqa: E402
    CREDENTIAL_NUMBER,
    MAX_AMOUNT,
    Client,
    Credential,
    CredentialsResponse,
    Issuer,
    IssuerSecretKey,
    RealAmountRequest,
    ZeroAmountRequest,
    derive_issuer_parameters,
)

# Helpers ----------------------------------------------------------------


def _zero_round_trip(issuer: Issuer, client: Client) -> list[Credential]:
    """Mint a fresh batch of zero-value credentials for use in later tests."""
    request = client.request_zero_amount()
    assert isinstance(request.payload, ZeroAmountRequest)
    response = issuer.handle_zero_amount(request.payload)
    assert isinstance(response, CredentialsResponse)
    return client.handle_response(request, response)


# Tests ------------------------------------------------------------------


def test_protocol_constants_match_jmp_0005() -> None:
    """JMP-0005 pins the credential vector and value bounds; the
    wrapper must mirror them so a spec drift becomes a CI failure
    rather than a silent runtime mismatch.
    """
    assert CREDENTIAL_NUMBER == 2
    assert MAX_AMOUNT == (1 << 51) - 1


def test_issuer_generate_then_recover_parameters() -> None:
    """``derive_issuer_parameters`` is the deterministic public projection of
    the secret key; an issuer instantiated from the same secret key must
    publish identical parameters so a coordinator restart does not
    invalidate clients holding cached parameters.
    """
    secret = IssuerSecretKey(nwabisabi.generate_issuer_secret_key())
    derived = derive_issuer_parameters(secret)
    issuer = Issuer(secret)
    assert issuer.parameters().raw == derived.raw


def test_zero_amount_round_trip_returns_two_credentials() -> None:
    """Smoke test for the input-registration path."""
    issuer = Issuer.generate()
    client = Client(issuer.parameters())
    credentials = _zero_round_trip(issuer, client)
    assert len(credentials) == CREDENTIAL_NUMBER
    assert all(c.value == 0 for c in credentials)


def test_real_amount_round_trip_preserves_value() -> None:
    """Reissue zero credentials against a real-value vector. The issuer
    balance increase exactly equals the sum of the requested amounts,
    proving the balance proof and range proof both verified end-to-end.
    """
    issuer = Issuer.generate(initial_balance=0)
    client = Client(issuer.parameters())
    zero_creds = _zero_round_trip(issuer, client)

    # Bump the issuer ceiling so the new outputs fit. The amounts vector
    # length is fixed at CREDENTIAL_NUMBER by the binding.
    amounts = [40_000, 60_000]
    issuer.reset(sum(amounts))

    request = client.request_real_amount(amounts, zero_creds)
    assert isinstance(request.payload, RealAmountRequest)

    response = issuer.handle_real_amount(request.payload)
    issued = client.handle_response(request, response)

    assert sorted(c.value for c in issued) == sorted(amounts)


def test_validation_handle_is_single_use() -> None:
    """Reusing a validation handle must raise. This guards against bugs
    where a caller tries to reapply a coordinator response (which would
    otherwise silently mint duplicate credentials in some adversarial
    scenarios).
    """
    issuer = Issuer.generate()
    client = Client(issuer.parameters())
    request = client.request_zero_amount()
    response = issuer.handle_zero_amount(request.payload)

    client.handle_response(request, response)
    with pytest.raises(RuntimeError, match="already consumed"):
        client.handle_response(request, response)


def test_credential_persists_through_serialization() -> None:
    """Wallets must be able to checkpoint in-flight credentials to disk.
    A round trip through ``to_bytes`` / ``from_bytes`` must preserve
    both the value and the ability to use the credential as a
    presentation in the next reissuance.
    """
    issuer = Issuer.generate()
    client = Client(issuer.parameters())
    creds = _zero_round_trip(issuer, client)

    blob = creds[0].to_bytes()
    rebuilt = Credential.from_bytes(blob)
    assert rebuilt.value == creds[0].value
    assert rebuilt.to_bytes() == blob


def test_request_zero_amount_handles_distinct_per_call() -> None:
    """Two consecutive zero-amount requests must produce distinct
    payloads (fresh randomness) even from the same client. Catches a
    regression where the binding accidentally reused an RNG state.
    """
    issuer = Issuer.generate()
    client = Client(issuer.parameters())
    a = client.request_zero_amount()
    b = client.request_zero_amount()
    assert a.payload.raw != b.payload.raw
