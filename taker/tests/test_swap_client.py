"""
Tests for the swap client module.

Uses mocked HTTP transport to test the swap acquisition flow
without requiring a real swap provider or Bitcoin node.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taker.swap.client import SwapClient
from taker.swap.models import SwapState
from taker.swap.script import SwapScript

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_keypair() -> tuple[bytes, bytes]:
    """Generate a secp256k1 keypair (privkey, compressed pubkey)."""
    from coincurve import PrivateKey

    privkey = secrets.token_bytes(32)
    pubkey = PrivateKey(privkey).public_key.format(compressed=True)
    return privkey, pubkey


def _make_swap_response(
    preimage_hash: bytes,
    claim_pubkey: bytes,
    current_block_height: int,
    onchain_amount: int = 48_000,
    timeout_delta: int = 80,
) -> dict[str, object]:
    """Build a valid provider response dict for createswap."""
    _, refund_pubkey = _make_keypair()
    timeout = current_block_height + timeout_delta

    script = SwapScript(
        preimage_hash=preimage_hash,
        claim_pubkey=claim_pubkey,
        refund_pubkey=refund_pubkey,
        timeout_blockheight=timeout,
    )
    ws = script.witness_script()
    lockup_address = script.p2wsh_address("regtest")

    return {
        "id": preimage_hash.hex(),
        "invoice": f"lnbcrt{onchain_amount}n1mock",
        "lockupAddress": lockup_address,
        "redeemScript": ws.hex(),
        "timeoutBlockHeight": timeout,
        "onchainAmount": onchain_amount,
    }


def _make_lockup_tx_hex(lockup_address: str, value_sats: int) -> tuple[str, str]:
    """Create a minimal fake transaction hex with a P2WSH output.

    Returns (txid, tx_hex) tuple. The hex is simplified but parseable
    enough for the swap client to find the output.
    """
    # We need a real parseable transaction. Build a minimal one.
    # For testing, we'll return data that the _find_swap_output method
    # can work with via the mocked parse_transaction.
    fake_txid = secrets.token_hex(32)
    fake_hex = "0200000001" + "00" * 100  # Placeholder
    return fake_txid, fake_hex


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSwapClientInit:
    """Tests for SwapClient initialization."""

    def test_default_state(self) -> None:
        client = SwapClient(provider_url="http://localhost:9999")
        assert client.state == SwapState.IDLE
        assert client.provider_url == "http://localhost:9999"
        assert client.network == "mainnet"

    def test_custom_params(self) -> None:
        client = SwapClient(
            provider_url="http://swap.example.com",
            network="regtest",
            max_swap_fee_pct=2.0,
            min_pow_bits=20,
        )
        assert client.network == "regtest"
        assert client.max_swap_fee_pct == 2.0
        assert client.min_pow_bits == 20

    def test_invoice_none_before_swap(self) -> None:
        client = SwapClient(provider_url="http://localhost:9999")
        assert client.invoice is None

    def test_swap_id_none_before_swap(self) -> None:
        client = SwapClient(provider_url="http://localhost:9999")
        assert client.swap_id is None


class TestSwapClientValidation:
    """Tests for input validation in acquire_swap_input."""

    @pytest.mark.asyncio
    async def test_pads_below_provider_minimum(self) -> None:
        """Amounts below the provider minimum are padded up, not rejected."""
        mock_pairs = {
            "percentage_fee": 0.5,
            "mining_fee": 1500,
            "min_amount": 20_000,
            "max_reverse_amount": 5_000_000,
        }
        provider_mock = MagicMock(
            pubkey="test",
            percentage_fee=0.5,
            mining_fee=1500,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            http_url="http://localhost:9999",
            pow_bits=0,
            calculate_fee=lambda x: int(x * 0.005) + 1500,
            calculate_invoice_amount=lambda x: x + 2000,
        )

        with patch("taker.swap.client.HTTPSwapTransport") as mock_transport_cls:
            mock_transport = AsyncMock()
            mock_transport.get_pairs = AsyncMock(return_value=mock_pairs)
            mock_transport_cls.return_value = mock_transport
            mock_transport_cls.provider_from_pairs = MagicMock(return_value=provider_mock)

            client = SwapClient(
                provider_url="http://localhost:9999",
                network="regtest",
            )
            # Patch _create_reverse_swap and everything after to isolate the clamping check
            client._generate_swap_secrets = MagicMock()  # type: ignore[method-assign]
            client._preimage_hash = b"\x00" * 32
            client._claim_pubkey = b"\x02" + b"\x00" * 32

            recorded: list[int] = []

            async def fake_create_swap(provider: object, invoice_amount: int) -> object:
                recorded.append(invoice_amount)
                raise ValueError("stop after recording invoice_amount")

            client._create_reverse_swap = fake_create_swap  # type: ignore[method-assign]

            with pytest.raises(ValueError, match="stop after recording"):
                await client.acquire_swap_input(
                    desired_amount_sats=1_000,  # well below min_amount of 20_000
                    current_block_height=800_000,
                )

            # calculate_invoice_amount(20_000) = 22_000
            assert recorded == [22_000], f"Expected invoice_amount=22000, got {recorded}"

    @pytest.mark.asyncio
    async def test_rejects_above_provider_max(self) -> None:
        """Should fail if amount exceeds provider's max_reverse_amount."""
        # Mock the provider discovery to return a provider with low max
        mock_pairs = {
            "percentage_fee": 0.5,
            "mining_fee": 1500,
            "min_amount": 20_000,
            "max_reverse_amount": 100_000,
        }

        with patch("taker.swap.client.HTTPSwapTransport") as mock_transport_cls:
            mock_transport = AsyncMock()
            mock_transport.get_pairs = AsyncMock(return_value=mock_pairs)
            mock_transport_cls.return_value = mock_transport
            mock_transport_cls.provider_from_pairs = MagicMock(
                return_value=MagicMock(
                    pubkey="test",
                    percentage_fee=0.5,
                    mining_fee=1500,
                    min_amount=20_000,
                    max_reverse_amount=100_000,
                    http_url="http://localhost:9999",
                    pow_bits=0,
                    calculate_fee=lambda x: int(x * 0.005) + 1500,
                    calculate_invoice_amount=lambda x: x + 2000,
                )
            )

            client = SwapClient(
                provider_url="http://localhost:9999",
                network="regtest",
            )
            with pytest.raises(ValueError, match="exceeds provider"):
                await client.acquire_swap_input(
                    desired_amount_sats=200_000,
                    current_block_height=800_000,
                )


class TestSwapClientSecrets:
    """Tests for cryptographic secret generation."""

    def test_generate_swap_secrets(self) -> None:
        client = SwapClient(provider_url="http://localhost:9999")
        client._generate_swap_secrets()

        assert client._preimage is not None
        assert len(client._preimage) == 32

        assert client._preimage_hash is not None
        assert len(client._preimage_hash) == 32
        assert client._preimage_hash == hashlib.sha256(client._preimage).digest()

        assert client._claim_privkey is not None
        assert len(client._claim_privkey) == 32

        assert client._claim_pubkey is not None
        assert len(client._claim_pubkey) == 33

    def test_secrets_are_random(self) -> None:
        """Each call should generate different secrets."""
        client = SwapClient(provider_url="http://localhost:9999")
        client._generate_swap_secrets()
        preimage1 = client._preimage

        client._generate_swap_secrets()
        preimage2 = client._preimage

        assert preimage1 != preimage2


class TestSwapClientDerivePublicKey:
    """Tests for public key derivation."""

    def test_derive_pubkey(self) -> None:
        privkey = secrets.token_bytes(32)
        pubkey = SwapClient._derive_pubkey(privkey)
        assert len(pubkey) == 33
        assert pubkey[0] in (0x02, 0x03)

    def test_derive_pubkey_deterministic(self) -> None:
        privkey = secrets.token_bytes(32)
        pub1 = SwapClient._derive_pubkey(privkey)
        pub2 = SwapClient._derive_pubkey(privkey)
        assert pub1 == pub2


class TestSwapClientVerification:
    """Tests for swap response verification."""

    def test_verify_valid_response(self) -> None:
        """Valid provider response should pass verification."""
        client = SwapClient(
            provider_url="http://localhost:9999",
            network="regtest",
        )
        client._generate_swap_secrets()

        current_height = 800_000
        assert client._preimage_hash is not None
        assert client._claim_pubkey is not None

        response_data = _make_swap_response(
            preimage_hash=client._preimage_hash,
            claim_pubkey=client._claim_pubkey,
            current_block_height=current_height,
        )

        from taker.swap.models import ReverseSwapResponse

        response = ReverseSwapResponse(**response_data)  # type: ignore[arg-type]

        # Should not raise
        client._verify_swap_response(response, current_height)
        assert client._swap_script is not None

    def test_verify_response_bad_lockup_address(self) -> None:
        """Response with wrong lockup address should fail."""
        client = SwapClient(
            provider_url="http://localhost:9999",
            network="regtest",
        )
        client._generate_swap_secrets()
        assert client._preimage_hash is not None
        assert client._claim_pubkey is not None

        current_height = 800_000
        response_data = _make_swap_response(
            preimage_hash=client._preimage_hash,
            claim_pubkey=client._claim_pubkey,
            current_block_height=current_height,
        )

        from taker.swap.models import ReverseSwapResponse

        # Tamper with the lockup address
        response_data["lockupAddress"] = "bcrt1qwrongaddress"
        response = ReverseSwapResponse(**response_data)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="[Ll]ockup address"):
            client._verify_swap_response(response, current_height)


class TestSwapClientGetClaimWitnessData:
    """Tests for get_claim_witness_data."""

    def test_returns_required_fields(self) -> None:
        client = SwapClient(provider_url="http://localhost:9999")

        preimage = secrets.token_bytes(32)
        privkey = secrets.token_bytes(32)
        ws = b"\x82" + bytes(100)

        swap_input = MagicMock()
        swap_input.witness_script = ws
        swap_input.preimage = preimage
        swap_input.claim_privkey = privkey
        swap_input.scriptpubkey = b"\x00\x20" + bytes(32)

        data = client.get_claim_witness_data(swap_input)
        assert "witness_script" in data
        assert "preimage" in data
        assert "claim_privkey" in data
        assert "scriptpubkey" in data
        assert data["witness_script"] == ws
        assert data["preimage"] == preimage
        assert data["claim_privkey"] == privkey


class TestSwapClientDiscoverProvider:
    """Tests for discover_provider() method."""

    @pytest.mark.asyncio
    async def test_discover_via_direct_url(self) -> None:
        """When provider_url is set, discover_provider uses HTTP transport."""
        mock_pairs = {
            "percentage_fee": 0.5,
            "mining_fee": 150,
            "min_amount": 20_000,
            "max_reverse_amount": 5_000_000,
        }
        mock_provider = MagicMock(
            pubkey="deadbeef" * 8,
            percentage_fee=0.5,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            http_url="http://localhost:9999",
            pow_bits=0,
        )

        with patch("taker.swap.client.HTTPSwapTransport") as mock_transport_cls:
            mock_transport = AsyncMock()
            mock_transport.get_pairs = AsyncMock(return_value=mock_pairs)
            mock_transport_cls.return_value = mock_transport
            mock_transport_cls.provider_from_pairs = MagicMock(return_value=mock_provider)

            client = SwapClient(provider_url="http://localhost:9999", network="regtest")
            provider = await client.discover_provider()

            assert provider is mock_provider
            assert client.provider is mock_provider
            assert client.state == SwapState.IDLE
            mock_transport.get_pairs.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discover_caches_provider(self) -> None:
        """discover_provider caches the result on the client."""
        mock_provider = MagicMock(
            pubkey="deadbeef" * 8,
            percentage_fee=0.5,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            http_url="http://localhost:9999",
            pow_bits=0,
        )

        with patch("taker.swap.client.HTTPSwapTransport") as mock_transport_cls:
            mock_transport = AsyncMock()
            mock_transport.get_pairs = AsyncMock(
                return_value={
                    "percentage_fee": 0.5,
                    "mining_fee": 150,
                    "min_amount": 20_000,
                    "max_reverse_amount": 5_000_000,
                }
            )
            mock_transport_cls.return_value = mock_transport
            mock_transport_cls.provider_from_pairs = MagicMock(return_value=mock_provider)

            client = SwapClient(provider_url="http://localhost:9999")
            await client.discover_provider()

            # Provider is cached
            assert client._provider is mock_provider
            assert client.provider is mock_provider

    @pytest.mark.asyncio
    async def test_discover_via_nostr(self) -> None:
        """When no provider_url, falls back to Nostr discovery."""
        mock_provider = MagicMock(
            pubkey="aabbccdd" * 8,
            percentage_fee=0.4,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            http_url="http://swap.example.com",
            pow_bits=6,
        )

        with patch("taker.swap.client.NostrSwapDiscovery") as mock_discovery_cls:
            mock_discovery = AsyncMock()
            mock_discovery.discover_providers = AsyncMock(return_value=[mock_provider])
            mock_discovery_cls.return_value = mock_discovery

            client = SwapClient(network="mainnet")
            provider = await client.discover_provider()

            assert provider is mock_provider
            assert client.provider is mock_provider
            mock_discovery.discover_providers.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discover_no_providers_raises(self) -> None:
        """When Nostr finds no providers, raises ConnectionError."""
        with patch("taker.swap.client.NostrSwapDiscovery") as mock_discovery_cls:
            mock_discovery = AsyncMock()
            mock_discovery.discover_providers = AsyncMock(return_value=[])
            mock_discovery_cls.return_value = mock_discovery

            client = SwapClient(network="mainnet")
            with pytest.raises(ConnectionError, match="No swap providers found"):
                await client.discover_provider()

    @pytest.mark.asyncio
    async def test_discover_resets_state_on_success(self) -> None:
        """State transitions: IDLE -> DISCOVERING -> IDLE on success."""
        states_seen: list[str] = []

        mock_provider = MagicMock(
            pubkey="aabbccdd" * 8,
            percentage_fee=0.5,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            http_url="http://localhost:9999",
            pow_bits=0,
        )

        async def spy_get_provider(self_inner: SwapClient) -> object:
            states_seen.append(str(self_inner.state))
            return mock_provider

        with patch.object(SwapClient, "_get_provider", spy_get_provider):
            client = SwapClient(provider_url="http://localhost:9999")
            assert client.state == SwapState.IDLE
            await client.discover_provider()
            assert states_seen == [SwapState.DISCOVERING]
            assert client.state == SwapState.IDLE

    @pytest.mark.asyncio
    async def test_acquire_reuses_discovered_provider(self) -> None:
        """acquire_swap_input skips re-discovery when provider is cached."""
        mock_provider = MagicMock(
            pubkey="deadbeef" * 8,
            percentage_fee=0.5,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            http_url="http://localhost:9999",
            pow_bits=0,
            calculate_fee=lambda x: int(x * 0.005) + 150,
            calculate_invoice_amount=lambda x: x + 300,
        )

        with patch("taker.swap.client.HTTPSwapTransport") as mock_transport_cls:
            mock_transport = AsyncMock()
            mock_transport.get_pairs = AsyncMock(
                return_value={
                    "percentage_fee": 0.5,
                    "mining_fee": 150,
                    "min_amount": 20_000,
                    "max_reverse_amount": 5_000_000,
                }
            )
            mock_transport_cls.return_value = mock_transport
            mock_transport_cls.provider_from_pairs = MagicMock(return_value=mock_provider)

            client = SwapClient(provider_url="http://localhost:9999", network="regtest")

            # Discover first
            await client.discover_provider()
            get_pairs_count = mock_transport.get_pairs.await_count

            # Now acquire — should NOT call get_pairs again
            client._generate_swap_secrets()
            client._preimage_hash = b"\x00" * 32
            client._claim_pubkey = b"\x02" + b"\x00" * 32

            recorded: list[int] = []

            async def fake_create_swap(provider: object, invoice_amount: int) -> object:
                recorded.append(invoice_amount)
                raise ValueError("stop after recording")

            client._create_reverse_swap = fake_create_swap  # type: ignore[method-assign]

            with pytest.raises(ValueError, match="stop after recording"):
                await client.acquire_swap_input(
                    desired_amount_sats=50_000,
                    current_block_height=800_000,
                )

            # get_pairs should not have been called again
            assert mock_transport.get_pairs.await_count == get_pairs_count


class TestSwapClientBlockchainWatching:
    """Tests for trustless lockup detection via blockchain backend."""

    def _setup_client_for_lockup(
        self,
        backend: AsyncMock | MagicMock,
    ) -> tuple[SwapClient, Any, bytes, bytes, str]:
        """Create a SwapClient ready for _wait_for_lockup testing.

        Returns (client, swap_response, preimage, claim_privkey, expected_spk_hex).
        """
        from taker.swap.models import ReverseSwapResponse

        client = SwapClient(
            provider_url="http://localhost:9999",
            network="regtest",
            backend=backend,
        )
        # Set up crypto material
        client._generate_swap_secrets()
        assert client._preimage is not None
        assert client._claim_privkey is not None
        assert client._preimage_hash is not None
        assert client._claim_pubkey is not None

        # Build a valid swap response (the script needs to be verifiable)
        _, refund_pubkey = _make_keypair()
        timeout = 800_100

        script = SwapScript(
            preimage_hash=client._preimage_hash,
            claim_pubkey=client._claim_pubkey,
            refund_pubkey=refund_pubkey,
            timeout_blockheight=timeout,
        )
        ws = script.witness_script()
        lockup_address = script.p2wsh_address("regtest")
        expected_spk_hex = script.p2wsh_scriptpubkey().hex()

        # Store the script on the client (normally done by _verify_swap_response)
        client._swap_script = script

        response = ReverseSwapResponse(
            id="test-swap-id",
            invoice="lnbcrt500000n1mock",
            lockup_address=lockup_address,
            redeem_script=ws.hex(),
            timeout_block_height=timeout,
            onchain_amount=48_000,
        )

        return client, response, client._preimage, client._claim_privkey, expected_spk_hex

    @pytest.mark.asyncio
    async def test_lockup_detected_on_first_poll(self) -> None:
        """UTXO found immediately on first backend poll."""
        from jmwallet.backends.base import UTXO

        backend = AsyncMock()
        client, response, preimage, claim_privkey, expected_spk = self._setup_client_for_lockup(
            backend
        )

        matching_utxo = UTXO(
            txid="a" * 64,
            vout=0,
            value=48_000,
            address=response.lockup_address,
            confirmations=1,
            scriptpubkey=expected_spk,
            height=800_001,
        )
        backend.get_utxos = AsyncMock(return_value=[matching_utxo])

        swap_input = await client._wait_for_lockup(response, timeout=10.0)

        assert swap_input.txid == "a" * 64
        assert swap_input.vout == 0
        assert swap_input.value == 48_000
        assert swap_input.preimage == preimage
        assert swap_input.claim_privkey == claim_privkey
        assert swap_input.lockup_address == response.lockup_address
        assert swap_input.swap_id == "test-swap-id"
        backend.get_utxos.assert_awaited_once_with([response.lockup_address])

    @pytest.mark.asyncio
    async def test_lockup_detected_after_retries(self) -> None:
        """UTXO appears after a few empty polls."""
        from jmwallet.backends.base import UTXO

        backend = AsyncMock()
        client, response, _, _, expected_spk = self._setup_client_for_lockup(backend)

        matching_utxo = UTXO(
            txid="b" * 64,
            vout=1,
            value=48_000,
            address=response.lockup_address,
            confirmations=0,
            scriptpubkey=expected_spk,
        )
        # First two polls return empty, third finds the UTXO
        backend.get_utxos = AsyncMock(side_effect=[[], [], [matching_utxo]])

        swap_input = await client._wait_for_lockup(response, timeout=30.0)

        assert swap_input.txid == "b" * 64
        assert swap_input.vout == 1
        assert swap_input.value == 48_000
        assert backend.get_utxos.await_count == 3

    @pytest.mark.asyncio
    async def test_lockup_timeout_raises(self) -> None:
        """TimeoutError when UTXO never appears."""
        backend = AsyncMock()
        client, response, _, _, _ = self._setup_client_for_lockup(backend)

        backend.get_utxos = AsyncMock(return_value=[])

        with pytest.raises(TimeoutError, match="Lockup transaction not seen"):
            await client._wait_for_lockup(response, timeout=0.1)

    @pytest.mark.asyncio
    async def test_lockup_ignores_wrong_scriptpubkey(self) -> None:
        """UTXOs at the address but with wrong scriptPubKey are ignored."""
        from jmwallet.backends.base import UTXO

        backend = AsyncMock()
        client, response, _, _, expected_spk = self._setup_client_for_lockup(backend)

        wrong_utxo = UTXO(
            txid="c" * 64,
            vout=0,
            value=48_000,
            address=response.lockup_address,
            confirmations=1,
            scriptpubkey="0020" + "ff" * 32,  # wrong scriptpubkey
        )
        backend.get_utxos = AsyncMock(return_value=[wrong_utxo])

        with pytest.raises(TimeoutError):
            await client._wait_for_lockup(response, timeout=0.1)

    @pytest.mark.asyncio
    async def test_lockup_no_backend_raises_runtime_error(self) -> None:
        """RuntimeError if no backend is configured."""
        from taker.swap.models import ReverseSwapResponse

        client = SwapClient(
            provider_url="http://localhost:9999",
            network="regtest",
            backend=None,  # no backend!
        )
        client._generate_swap_secrets()

        _, refund_pubkey = _make_keypair()
        assert client._preimage_hash is not None
        assert client._claim_pubkey is not None

        script = SwapScript(
            preimage_hash=client._preimage_hash,
            claim_pubkey=client._claim_pubkey,
            refund_pubkey=refund_pubkey,
            timeout_blockheight=800_100,
        )
        client._swap_script = script

        response = ReverseSwapResponse(
            id="no-backend-swap",
            invoice="lnbcrt1mock",
            lockup_address=script.p2wsh_address("regtest"),
            redeem_script=script.witness_script().hex(),
            timeout_block_height=800_100,
            onchain_amount=20_000,
        )

        with pytest.raises(RuntimeError, match="No blockchain backend configured"):
            await client._wait_for_lockup(response, timeout=5.0)

    @pytest.mark.asyncio
    async def test_lockup_poll_error_retries(self) -> None:
        """Transient backend errors are swallowed and retried."""
        from jmwallet.backends.base import UTXO

        backend = AsyncMock()
        client, response, _, _, expected_spk = self._setup_client_for_lockup(backend)

        matching_utxo = UTXO(
            txid="d" * 64,
            vout=0,
            value=48_000,
            address=response.lockup_address,
            confirmations=1,
            scriptpubkey=expected_spk,
        )
        # First poll raises, second succeeds
        backend.get_utxos = AsyncMock(side_effect=[ConnectionError("RPC timeout"), [matching_utxo]])

        swap_input = await client._wait_for_lockup(response, timeout=30.0)
        assert swap_input.txid == "d" * 64
        assert backend.get_utxos.await_count == 2

    @pytest.mark.asyncio
    async def test_lockup_picks_correct_utxo_among_many(self) -> None:
        """When multiple UTXOs exist at address, picks the one matching scriptPubKey."""
        from jmwallet.backends.base import UTXO

        backend = AsyncMock()
        client, response, _, _, expected_spk = self._setup_client_for_lockup(backend)

        decoy = UTXO(
            txid="e" * 64,
            vout=0,
            value=100_000,
            address=response.lockup_address,
            confirmations=2,
            scriptpubkey="0020" + "ab" * 32,  # different script
        )
        real = UTXO(
            txid="f" * 64,
            vout=1,
            value=48_000,
            address=response.lockup_address,
            confirmations=1,
            scriptpubkey=expected_spk,
        )
        backend.get_utxos = AsyncMock(return_value=[decoy, real])

        swap_input = await client._wait_for_lockup(response, timeout=10.0)
        assert swap_input.txid == "f" * 64
        assert swap_input.vout == 1
        assert swap_input.value == 48_000

    def test_backend_parameter_stored(self) -> None:
        """SwapClient stores the backend parameter."""
        mock_backend = MagicMock()
        client = SwapClient(
            provider_url="http://localhost:9999",
            backend=mock_backend,
        )
        assert client.backend is mock_backend

    def test_backend_default_none(self) -> None:
        """Backend defaults to None when not provided."""
        client = SwapClient(provider_url="http://localhost:9999")
        assert client.backend is None
