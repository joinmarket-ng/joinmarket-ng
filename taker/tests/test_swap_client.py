"""Tests for the swap client module."""

from __future__ import annotations

import secrets
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taker.swap.client import SwapClient
from taker.swap.models import SwapState
from taker.swap.script import SwapScript


def _make_keypair() -> tuple[bytes, bytes]:
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


class TestSwapClientInit:
    def test_default_state(self) -> None:
        client = SwapClient()
        assert client.state == SwapState.IDLE
        assert client.network == "mainnet"

    def test_custom_params(self) -> None:
        client = SwapClient(network="regtest", max_swap_fee_pct=2.0, min_pow_bits=20)
        assert client.network == "regtest"
        assert client.max_swap_fee_pct == 2.0
        assert client.min_pow_bits == 20

    def test_invoice_none_before_swap(self) -> None:
        assert SwapClient().invoice is None

    def test_swap_id_none_before_swap(self) -> None:
        assert SwapClient().swap_id is None


class TestSwapClientValidation:
    @pytest.mark.asyncio
    async def test_pads_below_provider_minimum(self) -> None:
        provider_mock = MagicMock(
            pubkey="test",
            percentage_fee=0.5,
            mining_fee=1500,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            relays=["wss://relay.example.com"],
            pow_bits=0,
            calculate_fee=lambda x: int(x * 0.005) + 1500,
            calculate_invoice_amount=lambda x: x + 2000,
        )

        with patch("taker.swap.client.NostrSwapDiscovery") as mock_discovery_cls:
            mock_discovery = AsyncMock()
            mock_discovery.discover_providers = AsyncMock(return_value=[provider_mock])
            mock_discovery_cls.return_value = mock_discovery

            client = SwapClient(network="regtest")
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
                    desired_amount_sats=1_000,
                    current_block_height=800_000,
                )

            assert recorded == [22_000]

    @pytest.mark.asyncio
    async def test_rejects_above_provider_max(self) -> None:
        provider_mock = MagicMock(
            pubkey="test",
            percentage_fee=0.5,
            mining_fee=1500,
            min_amount=20_000,
            max_reverse_amount=100_000,
            relays=["wss://relay.example.com"],
            pow_bits=0,
            calculate_fee=lambda x: int(x * 0.005) + 1500,
            calculate_invoice_amount=lambda x: x + 2000,
        )
        with patch("taker.swap.client.NostrSwapDiscovery") as mock_discovery_cls:
            mock_discovery = AsyncMock()
            mock_discovery.discover_providers = AsyncMock(return_value=[provider_mock])
            mock_discovery_cls.return_value = mock_discovery

            client = SwapClient(network="regtest")
            with pytest.raises(ValueError, match="exceeds provider"):
                await client.acquire_swap_input(
                    desired_amount_sats=200_000,
                    current_block_height=800_000,
                )


class TestSwapClientDiscoverProvider:
    @pytest.mark.asyncio
    async def test_discover_via_nostr(self) -> None:
        mock_provider = MagicMock(
            pubkey="aabbccdd" * 8,
            percentage_fee=0.4,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            relays=["wss://relay.example.com"],
            pow_bits=6,
            calculate_fee=lambda amount: int(amount * 0.004) + 150,
        )
        with patch("taker.swap.client.NostrSwapDiscovery") as mock_discovery_cls:
            mock_discovery = AsyncMock()
            mock_discovery.discover_providers = AsyncMock(return_value=[mock_provider])
            mock_discovery_cls.return_value = mock_discovery

            client = SwapClient(network="mainnet")
            provider = await client.discover_provider(target_amount_sats=100_000)

            assert provider is mock_provider
            assert client.provider is mock_provider
            mock_discovery.discover_providers.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_preferred_pubkey_selected(self) -> None:
        better_fee = MagicMock(
            pubkey="11" * 32,
            percentage_fee=0.2,
            mining_fee=100,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            relays=["wss://relay.example.com"],
            pow_bits=5,
            calculate_fee=lambda amount: int(amount * 0.002) + 100,
        )
        preferred = MagicMock(
            pubkey="22" * 32,
            percentage_fee=0.5,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            relays=["wss://relay.example.com"],
            pow_bits=5,
            calculate_fee=lambda amount: int(amount * 0.005) + 150,
        )

        with patch("taker.swap.client.NostrSwapDiscovery") as mock_discovery_cls:
            mock_discovery = AsyncMock()
            mock_discovery.discover_providers = AsyncMock(return_value=[better_fee, preferred])
            mock_discovery_cls.return_value = mock_discovery

            client = SwapClient(network="signet", preferred_provider_pubkey="22" * 32)
            provider = await client.discover_provider(target_amount_sats=100_000)
            assert provider is preferred

    @pytest.mark.asyncio
    async def test_preferred_pubkey_prefix_selected_when_unique(self) -> None:
        provider_a = MagicMock(
            pubkey="8b7a324427723d113ad6314afa44d85f5e10e04e904d0df5a68fd4510932e03c",
            percentage_fee=0.5,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            relays=["wss://relay.example.com"],
            pow_bits=5,
            calculate_fee=lambda amount: int(amount * 0.005) + 150,
        )
        provider_b = MagicMock(
            pubkey="c70d7bc9de7e98280c039e6b741c075b66f3f56e4798e3d9c3b4c93dc0511f27",
            percentage_fee=0.2,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            relays=["wss://relay.example.com"],
            pow_bits=5,
            calculate_fee=lambda amount: int(amount * 0.002) + 150,
        )

        with patch("taker.swap.client.NostrSwapDiscovery") as mock_discovery_cls:
            mock_discovery = AsyncMock()
            mock_discovery.discover_providers = AsyncMock(return_value=[provider_b, provider_a])
            mock_discovery_cls.return_value = mock_discovery

            client = SwapClient(network="signet", preferred_provider_pubkey="8b7a324427723d11")
            provider = await client.discover_provider(target_amount_sats=100_000)
            assert provider is provider_a

    @pytest.mark.asyncio
    async def test_discover_no_providers_raises(self) -> None:
        with patch("taker.swap.client.NostrSwapDiscovery") as mock_discovery_cls:
            mock_discovery = AsyncMock()
            mock_discovery.discover_providers = AsyncMock(return_value=[])
            mock_discovery_cls.return_value = mock_discovery

            client = SwapClient(network="mainnet")
            with pytest.raises(ConnectionError, match="No swap providers found"):
                await client.discover_provider()


class TestSwapClientVerification:
    def test_verify_valid_response(self) -> None:
        client = SwapClient(network="regtest")
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
        client._verify_swap_response(response, current_height)
        assert client._swap_script is not None


class TestSwapClientGetClaimWitnessData:
    def test_returns_required_fields(self) -> None:
        client = SwapClient()
        preimage = secrets.token_bytes(32)
        privkey = secrets.token_bytes(32)
        ws = b"\x82" + bytes(100)

        swap_input = MagicMock()
        swap_input.witness_script = ws
        swap_input.preimage = preimage
        swap_input.claim_privkey = privkey
        swap_input.scriptpubkey = b"\x00\x20" + bytes(32)

        data = client.get_claim_witness_data(swap_input)
        assert data["witness_script"] == ws
        assert data["preimage"] == preimage
        assert data["claim_privkey"] == privkey


class TestSwapClientBlockchainWatching:
    def _setup_client_for_lockup(
        self,
        backend: AsyncMock | MagicMock,
    ) -> tuple[SwapClient, Any, bytes, bytes, str]:
        from taker.swap.models import ReverseSwapResponse

        client = SwapClient(network="regtest", backend=backend)
        client._generate_swap_secrets()
        assert client._preimage is not None
        assert client._claim_privkey is not None
        assert client._preimage_hash is not None
        assert client._claim_pubkey is not None

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
    async def test_lockup_timeout_raises(self) -> None:
        backend = AsyncMock()
        client, response, _, _, _ = self._setup_client_for_lockup(backend)
        backend.get_utxos = AsyncMock(return_value=[])

        with pytest.raises(TimeoutError, match="Lockup transaction not seen"):
            await client._wait_for_lockup(response, timeout=0.1)
