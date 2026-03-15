"""
Tests for NostrSwapRPC — encrypted DM-based RPC over Nostr relays.

Tests use a mock WebSocket server to simulate a Nostr relay, sending
back properly encrypted responses.  No real relay connections are made.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from taker.swap.models import NOSTR_EVENT_KIND_DM
from taker.swap.nip04 import (
    create_nip04_dm_event,
    nip04_decrypt,
    privkey_to_xonly_pubkey,
)
from taker.swap.nostr import NostrSwapRPC, _event_matches_network

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _random_keypair() -> tuple[bytes, str]:
    """Generate a random secp256k1 keypair (privkey, xonly_pubkey_hex)."""
    privkey = secrets.token_bytes(32)
    pubkey = privkey_to_xonly_pubkey(privkey)
    return privkey, pubkey


def _make_encrypted_response(
    server_privkey: bytes,
    client_pubkey_hex: str,
    response_data: dict[str, Any],
) -> dict[str, Any]:
    """Build a kind 25582 Nostr event containing an encrypted response.

    This simulates what a real swap provider would send back.
    """
    content_json = json.dumps(response_data, separators=(",", ":"))

    event = create_nip04_dm_event(
        content=content_json,
        our_privkey=server_privkey,
        recipient_pubkey_hex=client_pubkey_hex,
        kind=NOSTR_EVENT_KIND_DM,
    )
    return event


class FakeWSResponse:
    """Simulates an aiohttp WebSocket message."""

    def __init__(self, msg_type: aiohttp.WSMsgType, data: str = "") -> None:
        self.type = msg_type
        self.data = data


class FakeWebSocket:
    """Fake WebSocket that sends scripted messages and captures outgoing ones.

    Attributes:
        incoming: Queue of messages the "relay" will send to the client.
        sent: List of messages the client sent to the "relay".
    """

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[FakeWSResponse] = asyncio.Queue()
        self.sent: list[str] = []

    async def send_str(self, data: str) -> None:
        self.sent.append(data)

    async def receive(self) -> FakeWSResponse:
        try:
            return await asyncio.wait_for(self.incoming.get(), timeout=5.0)
        except TimeoutError:
            return FakeWSResponse(aiohttp.WSMsgType.CLOSED, "")

    def enqueue(self, msg_type: aiohttp.WSMsgType, data: str = "") -> None:
        """Add a message to the relay's outgoing queue."""
        self.incoming.put_nowait(FakeWSResponse(msg_type, data))

    def enqueue_eose(self, sub_id: str | None = None) -> None:
        """Enqueue an EOSE marker."""
        # We don't know the exact sub_id the client will use, so we
        # accept any by default.  If sub_id is given we match it.
        self._pending_eose_sub_id = sub_id
        self.enqueue(aiohttp.WSMsgType.TEXT, json.dumps(["EOSE", sub_id or "any"]))

    def enqueue_event(self, event: dict[str, Any]) -> None:
        """Enqueue a Nostr EVENT message."""
        self.enqueue(aiohttp.WSMsgType.TEXT, json.dumps(["EVENT", "sub", event]))


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def server_keypair() -> tuple[bytes, str]:
    """A "swap provider" keypair."""
    return _random_keypair()


# ---------------------------------------------------------------------------
# Tests: NostrSwapRPC Initialization
# ---------------------------------------------------------------------------


class TestNostrSwapRPCInit:
    """Tests for NostrSwapRPC construction."""

    def test_ephemeral_keypair_generated(self, server_keypair: tuple[bytes, str]) -> None:
        _, server_pub = server_keypair
        rpc = NostrSwapRPC(provider_pubkey=server_pub, relays=["wss://relay.example.com"])

        assert len(rpc._privkey) == 32
        assert len(rpc._pubkey) == 64
        bytes.fromhex(rpc._pubkey)  # Valid hex

    def test_different_instances_different_keys(self, server_keypair: tuple[bytes, str]) -> None:
        _, server_pub = server_keypair
        rpc1 = NostrSwapRPC(provider_pubkey=server_pub, relays=["wss://r1.example.com"])
        rpc2 = NostrSwapRPC(provider_pubkey=server_pub, relays=["wss://r1.example.com"])

        assert rpc1._privkey != rpc2._privkey
        assert rpc1._pubkey != rpc2._pubkey


class TestEventNetworkFiltering:
    """Tests for network tag filtering on Nostr offer events."""

    @pytest.mark.parametrize(
        ("tags", "network", "expected"),
        [
            ([], "mainnet", True),
            ([], "signet", False),
            ([["r", "net:mainnet"]], "mainnet", True),
            ([["r", "net:mainnet"]], "signet", False),
            ([["r", "net:signet"]], "signet", True),
            ([["r", "net:signet"]], "mainnet", False),
            ([["r", "wss://relay.example.com"]], "mainnet", True),
            ([["r", "wss://relay.example.com"]], "signet", False),
            (
                [["r", "net:mainnet"], ["r", "net:signet"]],
                "signet",
                True,
            ),
            (
                [["d", "electrum-swapserver-5"], ["r", "net:signet"]],
                "mainnet",
                False,
            ),
        ],
    )
    def test_event_matches_network(
        self,
        tags: list[list[str]],
        network: str,
        expected: bool,
    ) -> None:
        assert _event_matches_network(tags, network) is expected


class TestProxyConnectorNormalization:
    """Tests for proxy URL normalization before aiohttp_socks connector creation."""

    def test_proxy_connector_uses_normalized_url_and_rdns(self) -> None:
        from taker.swap.nostr import _proxy_connector_from_isolated_url

        with (
            patch("taker.swap.nostr.normalize_proxy_url") as mock_normalize,
            patch("aiohttp_socks.ProxyConnector.from_url") as mock_from_url,
        ):
            mock_normalize.return_value = MagicMock(url="socks5://127.0.0.1:9050", rdns=True)
            mock_from_url.return_value = MagicMock()

            _proxy_connector_from_isolated_url("socks5h://user:pass@127.0.0.1:9050")

            mock_from_url.assert_called_once_with("socks5://127.0.0.1:9050", rdns=True)


# ---------------------------------------------------------------------------
# Tests: NostrSwapRPC.call()
# ---------------------------------------------------------------------------


class TestNostrSwapRPCCall:
    """Tests for the full RPC call flow via mocked WebSocket."""

    @pytest.mark.asyncio
    async def test_successful_createswap(self, server_keypair: tuple[bytes, str]) -> None:
        """A full successful createswap RPC roundtrip."""
        server_priv, server_pub = server_keypair

        rpc = NostrSwapRPC(
            provider_pubkey=server_pub,
            relays=["wss://relay.example.com"],
        )

        fake_ws = FakeWebSocket()

        # Enqueue EOSE first so the client proceeds past subscription setup
        fake_ws.enqueue(
            aiohttp.WSMsgType.TEXT,
            json.dumps(["EOSE", "any_sub_id"]),
        )

        async def respond_after_request() -> None:
            """Wait for the client to send the request, then respond."""
            # Wait for: SUB (message 0), then EVENT (message 1)
            while len(fake_ws.sent) < 2:
                await asyncio.sleep(0.01)

            # The second message is the EVENT
            event_msg = json.loads(fake_ws.sent[1])
            assert event_msg[0] == "EVENT"
            client_event = event_msg[1]
            request_event_id = client_event["id"]

            # Decrypt to verify the request is correct
            decrypted = nip04_decrypt(client_event["content"], server_priv, rpc._pubkey)
            req_data = json.loads(decrypted)
            assert req_data["method"] == "createswap"

            # Build encrypted response
            response_data = {
                "id": "abc123",
                "invoice": "lnbcrt500000n1mock",
                "lockupAddress": "bcrt1qmockaddr",
                "redeemScript": "0x" + "ab" * 32,
                "timeoutBlockHeight": 800080,
                "onchainAmount": 48000,
                "reply_to": request_event_id,
            }
            response_event = _make_encrypted_response(server_priv, rpc._pubkey, response_data)
            fake_ws.enqueue_event(response_event)

        responder = asyncio.create_task(respond_after_request())

        # Patch aiohttp.ClientSession to return our fake WebSocket
        with patch("taker.swap.nostr.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_session.ws_connect = MagicMock(return_value=AsyncMock())
            mock_session.ws_connect.return_value.__aenter__ = AsyncMock(return_value=fake_ws)
            mock_session.ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await rpc.call(
                method="createswap",
                params={
                    "type": "reversesubmarine",
                    "pairId": "BTC/BTC",
                    "invoiceAmount": 50000,
                    "preimageHash": "aa" * 32,
                    "claimPublicKey": "02" + "bb" * 32,
                },
                timeout=10.0,
            )

        await responder

        assert result["id"] == "abc123"
        assert result["invoice"] == "lnbcrt500000n1mock"
        assert result["onchainAmount"] == 48000
        assert result["timeoutBlockHeight"] == 800080
        # reply_to should have been popped by _try_parse_response
        assert "reply_to" not in result

    @pytest.mark.asyncio
    async def test_timeout_when_no_response(self, server_keypair: tuple[bytes, str]) -> None:
        """Should raise TimeoutError if no matching response arrives."""
        _, server_pub = server_keypair

        rpc = NostrSwapRPC(
            provider_pubkey=server_pub,
            relays=["wss://relay.example.com"],
        )

        fake_ws = FakeWebSocket()
        # Only send EOSE, never a response
        fake_ws.enqueue(
            aiohttp.WSMsgType.TEXT,
            json.dumps(["EOSE", "any"]),
        )
        # Then close
        fake_ws.enqueue(aiohttp.WSMsgType.CLOSED)

        with patch("taker.swap.nostr.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_session.ws_connect = MagicMock(return_value=AsyncMock())
            mock_session.ws_connect.return_value.__aenter__ = AsyncMock(return_value=fake_ws)
            mock_session.ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises((TimeoutError, ConnectionError)):
                await rpc._call_via_relay(
                    "wss://relay.example.com",
                    "createswap",
                    {"invoiceAmount": 50000},
                    timeout=0.5,
                )

    @pytest.mark.asyncio
    async def test_provider_error_raises_valueerror(
        self, server_keypair: tuple[bytes, str]
    ) -> None:
        """Provider returning an error field should raise ValueError."""
        server_priv, server_pub = server_keypair

        rpc = NostrSwapRPC(
            provider_pubkey=server_pub,
            relays=["wss://relay.example.com"],
        )

        fake_ws = FakeWebSocket()

        # Enqueue EOSE
        fake_ws.enqueue(
            aiohttp.WSMsgType.TEXT,
            json.dumps(["EOSE", "any"]),
        )

        async def inject_error_response() -> None:
            """Wait for request, then send error response."""
            while len(fake_ws.sent) < 2:
                await asyncio.sleep(0.01)

            event_msg = json.loads(fake_ws.sent[1])
            request_id = event_msg[1]["id"]

            response_data = {
                "error": "insufficient liquidity",
                "reply_to": request_id,
            }
            response_event = _make_encrypted_response(server_priv, rpc._pubkey, response_data)
            fake_ws.enqueue_event(response_event)

        task = asyncio.create_task(inject_error_response())

        with patch("taker.swap.nostr.aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_session.ws_connect = MagicMock(return_value=AsyncMock())
            mock_session.ws_connect.return_value.__aenter__ = AsyncMock(return_value=fake_ws)
            mock_session.ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ValueError, match="insufficient liquidity"):
                await rpc._call_via_relay(
                    "wss://relay.example.com",
                    "createswap",
                    {"invoiceAmount": 50000},
                    timeout=10.0,
                )

        await task

    @pytest.mark.asyncio
    async def test_all_relays_fail_raises_connectionerror(
        self, server_keypair: tuple[bytes, str]
    ) -> None:
        """If all relays fail, should raise ConnectionError."""
        _, server_pub = server_keypair

        rpc = NostrSwapRPC(
            provider_pubkey=server_pub,
            relays=["wss://r1.fail", "wss://r2.fail"],
        )

        # Make _call_via_relay always fail
        async def always_fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise ConnectionError("relay down")

        rpc._call_via_relay = always_fail  # type: ignore[method-assign]

        with pytest.raises(ConnectionError, match="Unable to reach"):
            await rpc.call("createswap", {"invoiceAmount": 50000})


# ---------------------------------------------------------------------------
# Tests: Response parsing
# ---------------------------------------------------------------------------


class TestResponseParsing:
    """Tests for _try_parse_response."""

    def test_ignores_non_event_messages(self, server_keypair: tuple[bytes, str]) -> None:
        """Non-EVENT messages should return None."""
        _, server_pub = server_keypair
        rpc = NostrSwapRPC(provider_pubkey=server_pub, relays=[])

        result = rpc._try_parse_response(json.dumps(["EOSE", "sub1"]), "abc123")
        assert result is None

    def test_ignores_wrong_pubkey(self, server_keypair: tuple[bytes, str]) -> None:
        """Events from a different pubkey should be ignored."""
        server_priv, server_pub = server_keypair
        rpc = NostrSwapRPC(provider_pubkey=server_pub, relays=[])

        # Build event from a different key
        other_priv, other_pub = _random_keypair()
        event = create_nip04_dm_event("hello", other_priv, rpc._pubkey)

        raw = json.dumps(["EVENT", "sub", event])
        result = rpc._try_parse_response(raw, "abc123")
        assert result is None

    def test_ignores_mismatched_reply_to(self, server_keypair: tuple[bytes, str]) -> None:
        """Response with wrong reply_to should be ignored."""
        server_priv, server_pub = server_keypair
        rpc = NostrSwapRPC(provider_pubkey=server_pub, relays=[])

        response_data = {"id": "swap1", "reply_to": "wrong_event_id"}
        event = _make_encrypted_response(server_priv, rpc._pubkey, response_data)

        raw = json.dumps(["EVENT", "sub", event])
        result = rpc._try_parse_response(raw, "correct_event_id")
        assert result is None

    def test_accepts_matching_response(self, server_keypair: tuple[bytes, str]) -> None:
        """Correctly matched response should be returned without reply_to."""
        server_priv, server_pub = server_keypair
        rpc = NostrSwapRPC(provider_pubkey=server_pub, relays=[])

        response_data = {
            "id": "swap123",
            "invoice": "lnbcrt1mock",
            "reply_to": "event_abc",
        }
        event = _make_encrypted_response(server_priv, rpc._pubkey, response_data)

        raw = json.dumps(["EVENT", "sub", event])
        result = rpc._try_parse_response(raw, "event_abc")

        assert result is not None
        assert result["id"] == "swap123"
        assert result["invoice"] == "lnbcrt1mock"
        assert "reply_to" not in result

    def test_ignores_invalid_json_content(self, server_keypair: tuple[bytes, str]) -> None:
        """Invalid JSON should not raise, just return None."""
        _, server_pub = server_keypair
        rpc = NostrSwapRPC(provider_pubkey=server_pub, relays=[])

        result = rpc._try_parse_response("not json at all", "abc")
        assert result is None

    def test_ignores_wrong_kind(self, server_keypair: tuple[bytes, str]) -> None:
        """Events with wrong kind should be ignored."""
        server_priv, server_pub = server_keypair
        rpc = NostrSwapRPC(provider_pubkey=server_pub, relays=[])

        # Build event with kind 1 instead of 25582
        response_data = {"id": "swap1", "reply_to": "evt1"}
        event = create_nip04_dm_event(json.dumps(response_data), server_priv, rpc._pubkey, kind=1)

        raw = json.dumps(["EVENT", "sub", event])
        result = rpc._try_parse_response(raw, "evt1")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: Prepay invoice support in SwapClient
# ---------------------------------------------------------------------------


class TestSwapClientPrepayInvoice:
    """Tests for the minerFeeInvoice (prepay) support in SwapClient."""

    @pytest.mark.asyncio
    async def test_prepay_invoice_paid_before_main(self) -> None:
        """When minerFeeInvoice is present, both invoices are paid concurrently.

        The Electrum swap server uses bundled payments: neither the prepay nor the
        main hold invoice settles until both HTLCs arrive at the provider. We fire
        both as concurrent tasks and await only the prepay, which settles once both
        HTLCs are present. The main payment task stays in-flight until the CoinJoin
        reveals the preimage on-chain.
        """
        from taker.swap.client import SwapClient
        from taker.swap.models import ReverseSwapResponse

        client = SwapClient(
            network="regtest",
            lnd_rest_url="https://lnd:8080",
            lnd_cert_path="/tmp/tls.cert",
            lnd_macaroon_path="/tmp/admin.macaroon",
        )

        # Mock the entire flow up to the point where invoices are paid
        client._provider = MagicMock(
            pubkey="test",
            percentage_fee=0.5,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            http_url="http://localhost:9999",
            pow_bits=0,
            calculate_invoice_amount=lambda x: x + 500,
        )

        swap_resp = ReverseSwapResponse(
            id="swap1",
            invoice="lnbcrt_main_invoice",
            miner_fee_invoice="lnbcrt_prepay_invoice",
            lockup_address="bcrt1qmockaddr",
            redeem_script="ab" * 32,
            timeout_block_height=800080,
            onchain_amount=48000,
        )

        client._generate_swap_secrets = MagicMock()  # type: ignore[method-assign]
        client._preimage_hash = b"\x00" * 32
        client._claim_pubkey = b"\x02" + b"\x00" * 32

        client._create_reverse_swap = AsyncMock(return_value=swap_resp)  # type: ignore[method-assign]
        client._verify_swap_response = MagicMock()  # type: ignore[method-assign]
        client._pay_invoice = AsyncMock()  # type: ignore[method-assign]
        client._wait_for_lockup = AsyncMock(  # type: ignore[method-assign]
            return_value=MagicMock(txid="abc", vout=0, value=48000)
        )

        await client.acquire_swap_input(
            desired_amount_sats=48_000,
            current_block_height=800_000,
        )

        # Both invoices must be paid (concurrently): prepay and main hold invoice.
        assert client._pay_invoice.call_count == 2
        paid_invoices = {c.args[0] for c in client._pay_invoice.call_args_list}
        assert "lnbcrt_prepay_invoice" in paid_invoices
        assert "lnbcrt_main_invoice" in paid_invoices

    @pytest.mark.asyncio
    async def test_no_prepay_when_absent(self) -> None:
        """When minerFeeInvoice is None, only the main invoice should be paid."""
        from taker.swap.client import SwapClient
        from taker.swap.models import ReverseSwapResponse

        client = SwapClient(
            network="regtest",
            lnd_rest_url="https://lnd:8080",
            lnd_cert_path="/tmp/tls.cert",
            lnd_macaroon_path="/tmp/admin.macaroon",
        )

        client._provider = MagicMock(
            pubkey="test",
            percentage_fee=0.5,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            http_url="http://localhost:9999",
            pow_bits=0,
            calculate_invoice_amount=lambda x: x + 500,
        )

        swap_resp = ReverseSwapResponse(
            id="swap2",
            invoice="lnbcrt_main_only",
            miner_fee_invoice=None,  # No prepay
            lockup_address="bcrt1qmockaddr",
            redeem_script="ab" * 32,
            timeout_block_height=800080,
            onchain_amount=48000,
        )

        client._generate_swap_secrets = MagicMock()  # type: ignore[method-assign]
        client._preimage_hash = b"\x00" * 32
        client._claim_pubkey = b"\x02" + b"\x00" * 32

        client._create_reverse_swap = AsyncMock(return_value=swap_resp)  # type: ignore[method-assign]
        client._verify_swap_response = MagicMock()  # type: ignore[method-assign]
        client._pay_invoice = AsyncMock()  # type: ignore[method-assign]
        client._wait_for_lockup = AsyncMock(  # type: ignore[method-assign]
            return_value=MagicMock(txid="abc", vout=0, value=48000)
        )

        await client.acquire_swap_input(
            desired_amount_sats=48_000,
            current_block_height=800_000,
        )

        # Only main invoice paid
        assert client._pay_invoice.call_count == 1
        assert client._pay_invoice.call_args[0][0] == "lnbcrt_main_only"


# ---------------------------------------------------------------------------
# Tests: NostrSwapRPC used by SwapClient._create_reverse_swap
# ---------------------------------------------------------------------------


class TestSwapClientNostrRPC:
    """Tests that SwapClient routes to NostrSwapRPC for Nostr-only providers."""

    @pytest.mark.asyncio
    async def test_uses_nostr_rpc_for_nostr_provider(self) -> None:
        """Provider with relays but no http_url should use NostrSwapRPC."""
        from taker.swap.client import SwapClient
        from taker.swap.models import SwapProvider

        provider = SwapProvider(
            pubkey="aa" * 32,
            percentage_fee=0.5,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            relays=["wss://relay.example.com"],
            http_url=None,
        )

        client = SwapClient(network="regtest")
        client._preimage_hash = b"\x00" * 32
        client._claim_pubkey = b"\x02" + b"\x00" * 32

        mock_response = {
            "id": "swap_nostr",
            "invoice": "lnbcrt1nostr",
            "lockupAddress": "bcrt1qnostr",
            "redeemScript": "cd" * 32,
            "timeoutBlockHeight": 800080,
            "onchainAmount": 48000,
        }

        with patch("taker.swap.client.NostrSwapRPC") as mock_rpc_cls:
            mock_rpc = AsyncMock()
            mock_rpc.call = AsyncMock(return_value=mock_response)
            mock_rpc_cls.return_value = mock_rpc

            result = await client._create_reverse_swap(provider, 50000)

        assert result.id == "swap_nostr"
        assert result.invoice == "lnbcrt1nostr"
        mock_rpc_cls.assert_called_once_with(
            provider_pubkey="aa" * 32,
            relays=["wss://relay.example.com"],
            socks_host=None,
            socks_port=9050,
        )

    @pytest.mark.asyncio
    async def test_raises_when_no_transport(self) -> None:
        """Provider with neither http_url nor relays should raise ConnectionError."""
        from taker.swap.client import SwapClient
        from taker.swap.models import SwapProvider

        provider = SwapProvider(
            pubkey="cc" * 32,
            percentage_fee=0.5,
            mining_fee=150,
            min_amount=20_000,
            max_reverse_amount=5_000_000,
            relays=[],
            http_url=None,
        )

        client = SwapClient(network="regtest")
        client._preimage_hash = b"\x00" * 32
        client._claim_pubkey = b"\x02" + b"\x00" * 32

        with pytest.raises(ConnectionError, match="neither.*HTTP.*nor.*Nostr"):
            await client._create_reverse_swap(provider, 50000)
