"""
Tests for swap data models.
"""

from __future__ import annotations

import secrets

import pytest

from taker.swap.models import (
    DEFAULT_SWAP_RELAYS,
    MAX_LOCKTIME_DELTA,
    MIN_LOCKTIME_DELTA,
    MIN_POW_BITS,
    NOSTR_D_TAG,
    NOSTR_EVENT_KIND_DM,
    NOSTR_EVENT_KIND_OFFER,
    ReverseSwapRequest,
    ReverseSwapResponse,
    SwapInput,
    SwapProvider,
    SwapState,
)


class TestSwapState:
    """Tests for SwapState enum."""

    def test_all_states_are_strings(self) -> None:
        for state in SwapState:
            assert isinstance(state, str)
            # StrEnum members ARE strings, so str(state) == state
            assert str(state) == state

    def test_idle_is_default_start(self) -> None:
        assert SwapState.IDLE == "idle"

    def test_terminal_states(self) -> None:
        assert SwapState.CLAIMED == "claimed"
        assert SwapState.REFUNDED == "refunded"
        assert SwapState.FAILED == "failed"


class TestSwapProvider:
    """Tests for SwapProvider model."""

    @pytest.fixture
    def provider(self) -> SwapProvider:
        return SwapProvider(
            offer_id="b" * 64,
            pubkey="a" * 64,
            percentage_fee=0.5,
            mining_fee=1500,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
        )

    def test_calculate_fee(self, provider: SwapProvider) -> None:
        fee = provider.calculate_fee(100_000)
        # 0.5% of 100k = 500, + 1500 mining = 2000
        assert fee == 2000

    def test_calculate_fee_small_amount(self, provider: SwapProvider) -> None:
        fee = provider.calculate_fee(20_000)
        # 0.5% of 20k = 100, + 1500 mining = 1600
        assert fee == 1600

    def test_calculate_invoice_amount(self, provider: SwapProvider) -> None:
        """Invoice amount should be enough to cover desired on-chain + fees."""
        desired = 50_000
        invoice = provider.calculate_invoice_amount(desired)

        # Verify: onchain = invoice * (1 - 0.5/100) - 1500
        onchain = invoice * (1 - 0.5 / 100) - 1500
        assert onchain >= desired
        # Should not overshoot by more than 1 sat (rounding)
        assert onchain - desired < 2

    def test_calculate_invoice_amount_round_trip(self, provider: SwapProvider) -> None:
        """invoice_amount -> onchain should be >= desired amount."""
        for desired in [20_000, 50_000, 100_000, 500_000, 1_000_000]:
            invoice = provider.calculate_invoice_amount(desired)
            pct_fee = int(invoice * provider.percentage_fee / 100)
            onchain = invoice - pct_fee - provider.mining_fee
            assert onchain >= desired, f"Desired {desired}, got onchain {onchain}"


class TestReverseSwapRequest:
    """Tests for ReverseSwapRequest serialization."""

    def test_alias_serialization(self) -> None:
        """Aliases should be used in JSON output."""
        req = ReverseSwapRequest(
            invoiceAmount=50000,
            preimageHash="ab" * 32,
            claimPublicKey="02" + "cc" * 32,
        )
        data = req.model_dump(by_alias=True)
        assert "pairId" in data
        assert "invoiceAmount" in data
        assert "preimageHash" in data
        assert "claimPublicKey" in data
        assert data["pairId"] == "BTC/BTC"

    def test_populate_by_name(self) -> None:
        """Should accept Python-style names too."""
        req = ReverseSwapRequest(
            invoice_amount=50000,
            preimage_hash="ab" * 32,
            claim_public_key="02" + "cc" * 32,
        )
        assert req.invoice_amount == 50000


class TestReverseSwapResponse:
    """Tests for ReverseSwapResponse deserialization."""

    def test_from_provider_response(self) -> None:
        """Simulate parsing a provider's JSON response."""
        resp = ReverseSwapResponse(
            id="abc123",
            invoice="lnbc50000n1...",
            lockupAddress="bcrt1q...",
            redeemScript="82" + "00" * 50,
            timeoutBlockHeight=800080,
            onchainAmount=48500,
        )
        assert resp.id == "abc123"
        assert resp.timeout_block_height == 800080
        assert resp.onchain_amount == 48500
        assert resp.lockup_address == "bcrt1q..."

    def test_optional_miner_fee_invoice(self) -> None:
        resp = ReverseSwapResponse(
            id="xyz",
            invoice="lnbc...",
            lockupAddress="bcrt1q...",
            redeemScript="82" + "00" * 50,
            timeoutBlockHeight=100,
            onchainAmount=10000,
        )
        assert resp.miner_fee_invoice is None


class TestSwapInput:
    """Tests for SwapInput dataclass."""

    @pytest.fixture
    def swap_input(self) -> SwapInput:
        preimage = secrets.token_bytes(32)
        privkey = secrets.token_bytes(32)
        # Build a minimal valid witness script for scriptpubkey derivation
        ws = b"\x82\x01\x20\x87\x63\xa9\x14" + b"\x00" * 20 + b"\x88\x21" + b"\x00" * 33
        ws += b"\x67\x75\x03\x80\x35\x0c\xb1\x75\x21" + b"\x00" * 33 + b"\x68\xac"
        return SwapInput(
            txid="aa" * 32,
            vout=0,
            value=50_000,
            witness_script=ws,
            preimage=preimage,
            claim_privkey=privkey,
            lockup_address="bcrt1qtest",
            timeout_block_height=800_080,
            swap_id="test_swap_id",
        )

    def test_scriptpubkey_is_34_bytes(self, swap_input: SwapInput) -> None:
        spk = swap_input.scriptpubkey
        assert len(spk) == 34
        assert spk[0] == 0x00
        assert spk[1] == 0x20

    def test_scriptpubkey_hex(self, swap_input: SwapInput) -> None:
        hex_spk = swap_input.scriptpubkey_hex
        assert len(hex_spk) == 68  # 34 bytes * 2
        assert hex_spk.startswith("0020")

    def test_to_utxo_dict(self, swap_input: SwapInput) -> None:
        d = swap_input.to_utxo_dict()
        assert d["txid"] == "aa" * 32
        assert d["vout"] == 0
        assert d["value"] == 50_000
        assert "scriptpubkey" in d
        assert isinstance(d["scriptpubkey"], str)

    def test_redeem_script_hex_default(self) -> None:
        si = SwapInput(
            txid="bb" * 32,
            vout=1,
            value=100_000,
            witness_script=b"\x00" * 34,
            preimage=b"\x00" * 32,
            claim_privkey=b"\x00" * 32,
            lockup_address="bcrt1q...",
            timeout_block_height=100,
            swap_id="test",
        )
        assert si.redeem_script_hex == ""


class TestConstants:
    """Tests for protocol constants."""

    def test_nostr_event_kinds(self) -> None:
        assert NOSTR_EVENT_KIND_OFFER == 30315
        assert NOSTR_EVENT_KIND_DM == 25582

    def test_nostr_d_tag_format(self) -> None:
        assert NOSTR_D_TAG == "electrum-swapserver-5"

    def test_locktime_bounds(self) -> None:
        assert MIN_LOCKTIME_DELTA == 60
        assert MAX_LOCKTIME_DELTA == 100
        assert MIN_LOCKTIME_DELTA < MAX_LOCKTIME_DELTA

    def test_min_pow_bits(self) -> None:
        assert MIN_POW_BITS == 5

    def test_default_relays(self) -> None:
        assert len(DEFAULT_SWAP_RELAYS) == 5
        for relay in DEFAULT_SWAP_RELAYS:
            assert relay.startswith("wss://")
