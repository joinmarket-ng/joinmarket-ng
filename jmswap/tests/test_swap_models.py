"""Tests for jmswap.models."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from jmswap.errors import SwapStateError
from jmswap.models import (
    ProviderTerms,
    SwapContract,
    SwapDirection,
    SwapRequest,
    SwapState,
    can_transition,
)

PUBKEY = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
CLIENT = "02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
PREIMAGE_HASH = "aa" * 32


def test_direction_is_reverse() -> None:
    assert SwapDirection.SWAP_OUT.is_reverse is True
    assert SwapDirection.SWAP_IN.is_reverse is False


def test_provider_terms_fee_and_limits() -> None:
    terms = ProviderTerms(
        provider_id="abc",
        provider_pubkey=PUBKEY,
        percentage_fee=Decimal("0.5"),
        base_fee=300,
        min_amount=10_000,
        max_amount=1_000_000,
    )
    assert terms.supports(SwapDirection.SWAP_IN)
    assert terms.within_limits(50_000)
    assert not terms.within_limits(5_000)
    # 0.5% of 100000 = 500, + base 300 = 800
    assert terms.fee_for(100_000) == 800


def test_provider_terms_rejects_bad_pubkey() -> None:
    with pytest.raises(ValidationError):
        ProviderTerms(provider_id="x", provider_pubkey="00" * 33)


def test_swap_request_validation() -> None:
    req = SwapRequest(
        direction=SwapDirection.SWAP_OUT,
        amount=50_000,
        preimage_hash=PREIMAGE_HASH,
        client_pubkey=CLIENT,
    )
    assert req.amount == 50_000
    with pytest.raises(ValidationError):
        SwapRequest(
            direction=SwapDirection.SWAP_OUT,
            amount=1,
            preimage_hash="aa",  # too short
            client_pubkey=CLIENT,
        )


def test_state_machine_transitions() -> None:
    assert can_transition(SwapState.CREATED, SwapState.AWAITING_LOCKUP)
    assert not can_transition(SwapState.CREATED, SwapState.CLAIMED)
    assert SwapState.CLAIMED.is_terminal
    assert not SwapState.LOCKUP_SEEN.is_terminal


def test_contract_advance_enforces_state_machine() -> None:
    contract = SwapContract(
        swap_id="s1",
        direction=SwapDirection.SWAP_IN,
        network="regtest",
        server_pubkey=PUBKEY,
        client_pubkey=CLIENT,
        preimage_hash=PREIMAGE_HASH,
        locktime=800_000,
        onchain_amount=100_000,
        funding_address="bcrt1p...",
    )
    contract.advance(SwapState.AWAITING_LOCKUP)
    contract.advance(SwapState.LOCKUP_SEEN)
    contract.advance(SwapState.LOCKUP_CONFIRMED)
    contract.advance(SwapState.CLAIMED)
    assert contract.state == SwapState.CLAIMED
    with pytest.raises(SwapStateError):
        contract.advance(SwapState.REFUNDED)


def test_contract_advance_same_state_noop() -> None:
    contract = SwapContract(
        swap_id="s2",
        direction=SwapDirection.SWAP_OUT,
        network="regtest",
        server_pubkey=PUBKEY,
        client_pubkey=CLIENT,
        preimage_hash=PREIMAGE_HASH,
        locktime=800_000,
        onchain_amount=100_000,
        funding_address="bcrt1p...",
    )
    contract.advance(SwapState.CREATED)  # no-op
    assert contract.state == SwapState.CREATED
