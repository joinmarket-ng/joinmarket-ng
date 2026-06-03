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


class _FakeKeyMaterial:
    """Public swap key material, mirroring jmwallet.SwapKeyMaterial."""

    def __init__(self, index: int, preimage: bytes, claim_privkey: bytes) -> None:
        import hashlib

        from coincurve import PrivateKey

        self.index = index
        self.preimage = preimage
        self.preimage_hash = hashlib.sha256(preimage).digest()
        self.claim_pubkey = PrivateKey(claim_privkey).public_key.format(compressed=True)


class FakeKeyProvider:
    """Deterministic in-test stand-in for the wallet's swap key authority.

    The taker depends only on the SwapKeyProvider protocol, so tests inject
    this instead of a full WalletService. It keeps the claim private key
    internal (as the real wallet does) and only exposes public material plus
    the finished claim witness.
    """

    def __init__(self) -> None:
        self._secrets: dict[int, tuple[bytes, bytes]] = {}
        self._next_index = 1

    def _ensure(self, index: int) -> tuple[bytes, bytes]:
        if index not in self._secrets:
            self._secrets[index] = (secrets.token_bytes(32), secrets.token_bytes(32))
        return self._secrets[index]

    def derive_swap_storage_key(self) -> bytes:
        return b"\x07" * 32

    def create_swap_key_material(self) -> _FakeKeyMaterial:
        index = self._next_index
        self._next_index += 1
        preimage, claim_privkey = self._ensure(index)
        return _FakeKeyMaterial(index, preimage, claim_privkey)

    def derive_swap_key_material(self, index: int) -> _FakeKeyMaterial:
        preimage, claim_privkey = self._ensure(index)
        return _FakeKeyMaterial(index, preimage, claim_privkey)

    def build_swap_claim_witness(
        self,
        tx: Any,
        input_index: int,
        witness_script: bytes,
        value: int,
        swap_index: int,
    ) -> list[bytes]:
        from coincurve import PrivateKey
        from jmwallet.wallet.signing import sign_p2wsh_input

        preimage, claim_privkey = self._ensure(swap_index)
        signature = sign_p2wsh_input(
            tx=tx,
            input_index=input_index,
            witness_script=witness_script,
            value=value,
            private_key=PrivateKey(claim_privkey),
        )
        return [signature, preimage, witness_script]


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

    def test_lockup_poll_interval_is_configurable(self) -> None:
        assert SwapClient().lockup_poll_interval == 2.0
        assert SwapClient(lockup_poll_interval=0.5).lockup_poll_interval == 0.5

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

    @pytest.mark.asyncio
    async def test_rejects_shorted_onchain_amount(self) -> None:
        from taker.swap.models import ReverseSwapResponse

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

            client = SwapClient(network="regtest", max_swap_fee_pct=1.0)
            client._generate_swap_secrets = MagicMock()  # type: ignore[method-assign]
            client._preimage_hash = b"\x00" * 32
            client._claim_pubkey = b"\x02" + b"\x00" * 32
            # Skip cryptographic script verification for this economic check.
            client._verify_swap_response = MagicMock()  # type: ignore[method-assign]

            # invoice_amount = 100_000 + 2000 = 102_000. With a 1% cap plus the
            # 1500 sat mining fee, the smallest acceptable on-chain amount is
            # ~99_480. The provider promises far less, so it must be rejected
            # BEFORE any invoice is paid.
            async def fake_create_swap(provider: object, invoice_amount: int) -> object:
                return ReverseSwapResponse(
                    id="shorted",
                    invoice="lnbcrt1mock",
                    lockup_address="bcrt1qmock",
                    redeem_script="00",
                    timeout_block_height=800_080,
                    onchain_amount=50_000,
                )

            client._create_reverse_swap = fake_create_swap  # type: ignore[method-assign]

            with pytest.raises(ValueError, match="shorts the on-chain amount"):
                await client.acquire_swap_input(
                    desired_amount_sats=100_000,
                    current_block_height=800_000,
                )


class TestSwapClientDiscoverProvider:
    @pytest.mark.asyncio
    async def test_discover_via_nostr(self) -> None:
        mock_provider = MagicMock(
            offer_id="aa" * 32,
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
            offer_id="11" * 32,
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
            offer_id="22" * 32,
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

            client = SwapClient(network="signet", preferred_offer_id="22" * 32)
            provider = await client.discover_provider(target_amount_sats=100_000)
            assert provider is preferred

    @pytest.mark.asyncio
    async def test_preferred_offer_id_prefix_selected_when_unique(self) -> None:
        provider_a = MagicMock(
            offer_id="8b7a324427723d113ad6314afa44d85f5e10e04e904d0df5a68fd4510932e03c",
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
            offer_id="c70d7bc9de7e98280c039e6b741c075b66f3f56e4798e3d9c3b4c93dc0511f27",
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

            client = SwapClient(network="signet", preferred_offer_id="8b7a324427723d11")
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
        client = SwapClient(network="regtest", key_provider=FakeKeyProvider())
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
        ws = b"\x82" + bytes(100)

        swap_input = MagicMock()
        swap_input.witness_script = ws
        swap_input.preimage = preimage
        swap_input.swap_index = 7
        swap_input.scriptpubkey = b"\x00\x20" + bytes(32)

        data = client.get_claim_witness_data(swap_input)
        assert data["witness_script"] == ws
        assert data["preimage"] == preimage
        assert data["swap_index"] == 7


class TestSwapClientBlockchainWatching:
    def _setup_client_for_lockup(
        self,
        backend: AsyncMock | MagicMock,
    ) -> tuple[SwapClient, Any, bytes, int, str]:
        from taker.swap.models import ReverseSwapResponse

        client = SwapClient(network="regtest", backend=backend, key_provider=FakeKeyProvider())
        client._generate_swap_secrets()
        assert client._preimage is not None
        assert client._swap_index is not None
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

        return client, response, client._preimage, client._swap_index, expected_spk_hex

    @pytest.mark.asyncio
    async def test_lockup_timeout_raises(self) -> None:
        backend = AsyncMock()
        client, response, _, _, _ = self._setup_client_for_lockup(backend)
        backend.scan_external_address = AsyncMock(return_value=[])

        with pytest.raises(TimeoutError, match="Lockup transaction not seen"):
            await client._wait_for_lockup(response, timeout=0.1)

    @pytest.mark.asyncio
    async def test_lockup_detected_when_utxo_appears_during_polling(self) -> None:
        backend = AsyncMock()
        client, response, _, _, expected_spk_hex = self._setup_client_for_lockup(backend)

        unseen: list[object] = []
        seen = [
            MagicMock(
                txid="11" * 32,
                vout=1,
                value=response.onchain_amount,
                confirmations=0,
                scriptpubkey=expected_spk_hex,
            )
        ]
        backend.scan_external_address = AsyncMock(side_effect=[unseen, unseen, seen])

        with patch("taker.swap.client.asyncio.sleep", new=AsyncMock()):
            swap_input = await client._wait_for_lockup(response, timeout=5.0)

        assert swap_input.txid == "11" * 32
        assert swap_input.vout == 1
        assert swap_input.value == response.onchain_amount

    @pytest.mark.asyncio
    async def test_short_lockup_utxo_is_ignored(self) -> None:
        backend = AsyncMock()
        client, response, _, _, expected_spk_hex = self._setup_client_for_lockup(backend)

        # A provider that locks fewer sats than promised must not be accepted:
        # the taker would otherwise top up the shortfall from its own wallet.
        short = [
            MagicMock(
                txid="22" * 32,
                vout=0,
                value=response.onchain_amount - 10_000,
                confirmations=1,
                scriptpubkey=expected_spk_hex,
            )
        ]
        full = [
            MagicMock(
                txid="33" * 32,
                vout=2,
                value=response.onchain_amount,
                confirmations=1,
                scriptpubkey=expected_spk_hex,
            )
        ]
        backend.scan_external_address = AsyncMock(side_effect=[short, short, full])

        with patch("taker.swap.client.asyncio.sleep", new=AsyncMock()):
            swap_input = await client._wait_for_lockup(response, timeout=5.0)

        # The short UTXO is skipped; the correctly-valued one is accepted.
        assert swap_input.txid == "33" * 32
        assert swap_input.value == response.onchain_amount

    @pytest.mark.asyncio
    async def test_only_short_lockup_times_out(self) -> None:
        backend = AsyncMock()
        client, response, _, _, expected_spk_hex = self._setup_client_for_lockup(backend)

        short = [
            MagicMock(
                txid="22" * 32,
                vout=0,
                value=response.onchain_amount - 1,
                confirmations=1,
                scriptpubkey=expected_spk_hex,
            )
        ]
        backend.scan_external_address = AsyncMock(return_value=short)

        # A purely short lockup must never be accepted; it times out into a
        # safe abort (which the caller turns into a payment cancellation).
        with pytest.raises(TimeoutError, match="Lockup transaction not seen"):
            await client._wait_for_lockup(response, timeout=0.1)


class TestCancelPendingPayment:
    """``SwapClient.cancel_pending_payment`` (post-lockup CoinJoin failure)."""

    @pytest.mark.asyncio
    async def test_no_op_when_no_task(self) -> None:
        client = SwapClient()
        # Should not raise even when there is no in-flight payment task.
        await client.cancel_pending_payment()
        assert client._main_payment_task is None

    @pytest.mark.asyncio
    async def test_cancels_in_flight_task(self) -> None:
        import asyncio

        client = SwapClient()

        async def _slow_payment() -> None:
            # Simulate an in-flight LND REST call.
            await asyncio.sleep(60)

        client._main_payment_task = asyncio.create_task(_slow_payment())
        # Yield once so the task actually starts.
        await asyncio.sleep(0)
        assert not client._main_payment_task.done()

        await client.cancel_pending_payment()

        assert client._main_payment_task is None

    @pytest.mark.asyncio
    async def test_no_op_when_task_already_done(self) -> None:
        import asyncio

        client = SwapClient()

        async def _instant() -> None:
            return None

        task = asyncio.create_task(_instant())
        await task  # let it complete
        client._main_payment_task = task

        await client.cancel_pending_payment()

        # Done tasks are ignored; reference remains untouched.
        assert client._main_payment_task is task


class TestHoldInvoiceTimeout:
    """The main hold invoice must stay in flight far longer than the prepay."""

    def test_default_hold_invoice_timeout(self) -> None:
        client = SwapClient()
        # A reverse hold invoice only settles after the CoinJoin reveals the
        # preimage on-chain, so the default must be much larger than the
        # 60s prepay timeout.
        assert client.hold_invoice_timeout == 3600.0

    def test_custom_hold_invoice_timeout_stored(self) -> None:
        client = SwapClient(hold_invoice_timeout=1800.0)
        assert client.hold_invoice_timeout == 1800.0

    @pytest.mark.asyncio
    async def test_pay_invoice_threads_timeout(self) -> None:
        client = SwapClient(
            lnd_rest_url="https://lnd.example:8080",
            lnd_cert_path="/tmp/tls.cert",
            lnd_macaroon_path="/tmp/admin.macaroon",
        )

        lnd_client = MagicMock()
        lnd_client.pay_invoice = AsyncMock(return_value={"payment_preimage_hex": "ab" * 32})
        lnd_client.close = AsyncMock()

        with (
            patch("taker.swap.ln_client.LndConnection"),
            patch("taker.swap.ln_client.LndRestClient", return_value=lnd_client),
        ):
            await client._pay_invoice("lnbc1...", timeout_seconds=1800)

        _, kwargs = lnd_client.pay_invoice.call_args
        assert kwargs["timeout_seconds"] == 1800

    @pytest.mark.asyncio
    async def test_pay_invoice_default_timeout_is_short(self) -> None:
        client = SwapClient(
            lnd_rest_url="https://lnd.example:8080",
            lnd_cert_path="/tmp/tls.cert",
            lnd_macaroon_path="/tmp/admin.macaroon",
        )

        lnd_client = MagicMock()
        lnd_client.pay_invoice = AsyncMock(return_value={"payment_preimage_hex": "ab" * 32})
        lnd_client.close = AsyncMock()

        with (
            patch("taker.swap.ln_client.LndConnection"),
            patch("taker.swap.ln_client.LndRestClient", return_value=lnd_client),
        ):
            await client._pay_invoice("lnbc1...")

        _, kwargs = lnd_client.pay_invoice.call_args
        # Prepay-style default keeps the short timeout.
        assert kwargs["timeout_seconds"] == 60
