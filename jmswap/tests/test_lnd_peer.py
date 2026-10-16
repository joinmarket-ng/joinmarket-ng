"""Transport-level tests for the LND peer messaging and invoice adapter.

Everything here pins the adapter contract only: request mapping, structural
validation of responses, how streams are bounded and cleaned up, and how
transport failures surface. Both gRPC stubs are replaced by recorders, so no
backend is involved and no test depends on buyout protocol state.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import grpc
import pytest
from bitcointx.core.key import CKey

from jmswap.buyout_messages import (
    BUYOUT_CUSTOM_MESSAGE_TYPE,
    MAX_PAYLOAD_BYTES,
    BuyoutClose,
    BuyoutMessageError,
    Outpoint,
    encode_buyout_payload,
)
from jmswap.lnd_peer import (
    LIGHTNING_SERVICE_NAME,
    MAX_ROUTE_HINT_HOPS,
    MAX_ROUTE_HINT_PATHS,
    ROUTER_SERVICE_NAME,
    ChannelInfo,
    InvoiceInfo,
    InvoiceState,
    InvoiceStatus,
    LndPeerClient,
    LndPeerError,
    LndPeerResponseError,
    LndPeerRpcError,
    NodeInfo,
    PaymentResult,
    PaymentRouteHop,
    PaymentStatus,
    PeerMessage,
    _NodeMacaroon,
)
from jmswap.lndrpc import lightning_pb2 as pb
from jmswap.lndrpc import router_pb2 as router_pb

TLS_CERT = b"-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"
MACAROON = bytes(range(64))
TIMEOUT = 30.0

# A display-order txid and the internal little-endian hash LND carries; the
# txid is deliberately not palindromic, so a missing reversal shows up.
TXID = "0f1e2d3c" * 8
TXID_INTERNAL = bytes.fromhex(TXID)[::-1]
POINT = Outpoint(txid=TXID, vout=3)

PREIMAGE = bytes([0x42]) * 32
PAYMENT_HASH = hashlib.sha256(PREIMAGE).digest()
BOLT11 = "lnbcrt10u1pjtestinvoice"


def _pubkey(secret: int) -> bytes:
    return bytes(CKey.from_secret_bytes(bytes([secret]) * 32).pub)


PEER_PUBKEY = _pubkey(1)
NODE_PUBKEY = _pubkey(2)
DESTINATION = _pubkey(3)

PAYLOAD = encode_buyout_payload(
    BuyoutClose(v=1, epoch_id="ab" * 32, attempt=0, type="buyout_close", reason_code="done")
)


class _Stream:
    """A server stream the stub hands back; records whether it was cancelled."""

    def __init__(self, updates: list[Any], error: BaseException | None = None) -> None:
        self.updates = list(updates)
        self.error = error
        self.cancelled = False

    def cancel(self) -> bool:
        self.cancelled = True
        return True

    async def read(self) -> Any:
        if self.error is not None:
            raise self.error
        if not self.updates:
            return grpc.aio.EOF
        return self.updates.pop(0)

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        if self.error is not None:
            raise self.error
        if not self.updates:
            raise StopAsyncIteration
        return self.updates.pop(0)


class _HangingStream(_Stream):
    """Delivers its updates and then blocks, like a live subscription."""

    async def __anext__(self) -> Any:
        if not self.updates:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        return self.updates.pop(0)


class RecordingStub:
    """Minimal stand-in for a generated stub that records every call."""

    def __init__(self, **results: Any) -> None:
        self.results = results
        self.calls: list[SimpleNamespace] = []

    def __getattr__(self, method: str) -> Any:
        if method.startswith("_"):
            raise AttributeError(method)
        result = self.results[method]

        def record(request: Any, timeout: float | None, credentials: Any) -> Any:
            self.calls.append(
                SimpleNamespace(
                    method=method, request=request, timeout=timeout, credentials=credentials
                )
            )
            if isinstance(result, BaseException):
                raise result
            return result

        if isinstance(result, _Stream):

            def stream_call(request: Any, *, credentials: Any, timeout: float | None = None) -> Any:
                return record(request, timeout, credentials)

            return stream_call

        async def unary_call(request: Any, *, timeout: float, credentials: Any) -> Any:
            return record(request, timeout, credentials)

        return unary_call

    @property
    def last(self) -> SimpleNamespace:
        return self.calls[-1]


@asynccontextmanager
async def peer_client(
    lightning: RecordingStub | None = None,
    router: RecordingStub | None = None,
    timeout: float = TIMEOUT,
    payment_route_hints: tuple[tuple[PaymentRouteHop, ...], ...] = (),
) -> AsyncIterator[Any]:
    """Enter a client whose transport is the recording stubs."""
    client = LndPeerClient(
        endpoint="127.0.0.1:10009",
        tls_certificate=TLS_CERT,
        macaroon=MACAROON,
        timeout=timeout,
        payment_route_hints=payment_route_hints,
    )
    async with client:
        client._lightning = lightning
        client._router = router
        yield client


def rpc_error(
    code: grpc.StatusCode, details: str = "secret backend detail"
) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(
        code, grpc.aio.Metadata(), grpc.aio.Metadata(), details=details, debug_error_string=details
    )


def custom_message(**overrides: Any) -> pb.CustomMessage:
    fields: dict[str, Any] = {
        "peer": PEER_PUBKEY,
        "type": BUYOUT_CUSTOM_MESSAGE_TYPE,
        "data": PAYLOAD,
    }
    fields.update(overrides)
    return pb.CustomMessage(**fields)


def info_response(**overrides: Any) -> pb.GetInfoResponse:
    fields: dict[str, Any] = {
        "identity_pubkey": NODE_PUBKEY.hex(),
        "block_height": 812_345,
        "synced_to_chain": True,
        "chains": [pb.Chain(chain="bitcoin", network="regtest")],
    }
    fields.update(overrides)
    return pb.GetInfoResponse(**fields)


def channel(**overrides: Any) -> pb.Channel:
    fields: dict[str, Any] = {
        "active": True,
        "private": True,
        "remote_pubkey": PEER_PUBKEY.hex(),
        "channel_point": f"{TXID}:3",
        "capacity": 1_000_000,
        "local_balance": 600_000,
        "remote_balance": 399_000,
        "commitment_type": pb.CommitmentType.SIMPLE_TAPROOT,
    }
    fields.update(overrides)
    return pb.Channel(**fields)


def pay_req(**overrides: Any) -> pb.PayReq:
    fields: dict[str, Any] = {
        "destination": DESTINATION.hex(),
        "payment_hash": PAYMENT_HASH.hex(),
        "num_satoshis": 1_000,
        "num_msat": 1_000_000,
        "timestamp": 1_700_000_000,
        "expiry": 600,
        "cltv_expiry": 80,
    }
    fields.update(overrides)
    return pb.PayReq(**fields)


def invoice(**overrides: Any) -> pb.Invoice:
    fields: dict[str, Any] = {
        "r_hash": PAYMENT_HASH,
        "r_preimage": PREIMAGE,
        "state": pb.Invoice.InvoiceState.SETTLED,
        "amt_paid_sat": 1_000,
    }
    fields.update(overrides)
    return pb.Invoice(**fields)


def payment(**overrides: Any) -> pb.Payment:
    fields: dict[str, Any] = {
        "payment_hash": PAYMENT_HASH.hex(),
        "payment_preimage": PREIMAGE.hex(),
        "status": pb.Payment.PaymentStatus.SUCCEEDED,
        "fee_sat": 7,
    }
    fields.update(overrides)
    return pb.Payment(**fields)


class TestConstruction:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"endpoint": ""},
            {"tls_certificate": b""},
            {"tls_certificate": "not bytes"},
            {"macaroon": b""},
            {"timeout": 0},
            {"timeout": -1.0},
            {"timeout": True},
        ],
    )
    def test_rejects_unusable_configuration(self, kwargs: dict[str, Any]) -> None:
        fields: dict[str, Any] = {
            "endpoint": "127.0.0.1:10009",
            "tls_certificate": TLS_CERT,
            "macaroon": MACAROON,
        }
        fields.update(kwargs)
        with pytest.raises(ValueError):
            LndPeerClient(**fields)

    def test_repr_hides_the_credentials(self) -> None:
        client = LndPeerClient("127.0.0.1:10009", TLS_CERT, MACAROON)
        text = repr(client)
        assert MACAROON.hex() not in text
        assert "certificate" not in text.lower()

    async def test_calls_require_the_context_manager(self) -> None:
        client = LndPeerClient("127.0.0.1:10009", TLS_CERT, MACAROON)
        with pytest.raises(LndPeerError):
            await client.node_info()


class RecordingCallback(grpc.AuthMetadataPluginCallback):
    """Captures what the macaroon plugin hands back to gRPC."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Exception | None]] = []

    def __call__(self, metadata: Any, error: Exception | None) -> None:
        self.calls.append((metadata, error))


class TestMacaroonScope:
    def context(self, service: str) -> Any:
        return SimpleNamespace(
            service_url=f"https://127.0.0.1:10009/{service}", method_name="GetInfo"
        )

    @pytest.mark.parametrize("service", [LIGHTNING_SERVICE_NAME, ROUTER_SERVICE_NAME])
    def test_attaches_the_macaroon_to_the_two_services(self, service: str) -> None:
        callback = RecordingCallback()
        _NodeMacaroon(MACAROON)(self.context(service), callback)
        assert callback.calls == [((("macaroon", MACAROON.hex()),), None)]

    @pytest.mark.parametrize(
        "service", ["invoicesrpc.Invoices", "channelescrowrpc.ChannelEscrow", "lnrpc.LightningX"]
    )
    def test_refuses_any_other_service(self, service: str) -> None:
        callback = RecordingCallback()
        _NodeMacaroon(MACAROON)(self.context(service), callback)
        (metadata, error) = callback.calls[0]
        assert metadata == ()
        assert isinstance(error, ValueError)

    def test_repr_hides_the_macaroon(self) -> None:
        assert MACAROON.hex() not in repr(_NodeMacaroon(MACAROON))


class TestSend:
    async def test_sends_the_payload_as_a_buyout_custom_message(self) -> None:
        stub = RecordingStub(SendCustomMessage=pb.SendCustomMessageResponse(status="ok"))
        async with peer_client(stub) as client:
            assert await client.send(PEER_PUBKEY, PAYLOAD) is None
        assert stub.last.request == pb.SendCustomMessageRequest(
            peer=PEER_PUBKEY, type=BUYOUT_CUSTOM_MESSAGE_TYPE, data=PAYLOAD
        )
        assert stub.last.timeout == TIMEOUT

    @pytest.mark.parametrize(
        "peer", [b"", PEER_PUBKEY[:-1], bytes(33), PEER_PUBKEY.hex(), bytes([0x04]) * 33]
    )
    async def test_rejects_an_unusable_peer_key(self, peer: Any) -> None:
        stub = RecordingStub(SendCustomMessage=pb.SendCustomMessageResponse())
        async with peer_client(stub) as client:
            with pytest.raises(ValueError):
                await client.send(peer, PAYLOAD)
        assert stub.calls == []

    @pytest.mark.parametrize(
        "payload",
        [b"", b"{}", b'{"v": 1}', b'{"b":1,"a":2}', b"x" * (MAX_PAYLOAD_BYTES + 1)],
    )
    async def test_refuses_to_send_a_payload_the_codec_rejects(self, payload: bytes) -> None:
        stub = RecordingStub(SendCustomMessage=pb.SendCustomMessageResponse())
        async with peer_client(stub) as client:
            with pytest.raises(BuyoutMessageError):
                await client.send(PEER_PUBKEY, payload)
        assert stub.calls == []

    async def test_reports_only_the_status_code_of_a_failure(self) -> None:
        stub = RecordingStub(
            SendCustomMessage=rpc_error(grpc.StatusCode.UNAVAILABLE, "peer 0123 is offline")
        )
        async with peer_client(stub) as client:
            with pytest.raises(LndPeerRpcError) as raised:
                await client.send(PEER_PUBKEY, PAYLOAD)
        assert raised.value.code is grpc.StatusCode.UNAVAILABLE
        assert "peer 0123 is offline" not in str(raised.value)
        assert raised.value.__cause__ is None


class TestReceive:
    async def _drain(self, stub: RecordingStub) -> list[PeerMessage]:
        async with peer_client(stub) as client:
            return [message async for message in client.receive()]

    async def test_yields_buyout_messages(self) -> None:
        stream = _Stream([custom_message(), custom_message(peer=NODE_PUBKEY)])
        assert await self._drain(RecordingStub(SubscribeCustomMessages=stream)) == [
            PeerMessage(peer_pubkey=PEER_PUBKEY, payload=PAYLOAD),
            PeerMessage(peer_pubkey=NODE_PUBKEY, payload=PAYLOAD),
        ]
        assert stream.cancelled

    async def test_subscription_carries_no_deadline(self) -> None:
        stub = RecordingStub(SubscribeCustomMessages=_Stream([]))
        await self._drain(stub)
        assert stub.last.request == pb.SubscribeCustomMessagesRequest()
        assert stub.last.timeout is None

    @pytest.mark.parametrize(
        "overrides",
        [
            {"type": BUYOUT_CUSTOM_MESSAGE_TYPE - 1},
            {"type": BUYOUT_CUSTOM_MESSAGE_TYPE + 1},
            {"peer": PEER_PUBKEY[:-1]},
            {"peer": bytes(33)},
            {"data": b""},
            {"data": b"x" * (MAX_PAYLOAD_BYTES + 1)},
        ],
    )
    async def test_drops_anything_that_is_not_a_buyout_message(
        self, overrides: dict[str, Any]
    ) -> None:
        stream = _Stream([custom_message(**overrides), custom_message()])
        assert await self._drain(RecordingStub(SubscribeCustomMessages=stream)) == [
            PeerMessage(peer_pubkey=PEER_PUBKEY, payload=PAYLOAD)
        ]

    async def test_does_not_validate_the_payload_against_the_codec(self) -> None:
        """Parsing a peer's payload is the runtime's job, not the transport's."""
        stream = _Stream([custom_message(data=b"not json")])
        assert await self._drain(RecordingStub(SubscribeCustomMessages=stream)) == [
            PeerMessage(peer_pubkey=PEER_PUBKEY, payload=b"not json")
        ]

    async def test_stream_failure_is_reported_as_a_status_code(self) -> None:
        stream = _Stream([], error=rpc_error(grpc.StatusCode.UNIMPLEMENTED, "no custom messages"))
        with pytest.raises(LndPeerRpcError) as raised:
            await self._drain(RecordingStub(SubscribeCustomMessages=stream))
        assert raised.value.code is grpc.StatusCode.UNIMPLEMENTED
        assert "no custom messages" not in str(raised.value)
        assert stream.cancelled

    async def test_leaving_the_iterator_early_cancels_the_stream(self) -> None:
        stream = _HangingStream([custom_message()])
        async with peer_client(RecordingStub(SubscribeCustomMessages=stream)) as client:
            messages = client.receive()
            assert await anext(messages) == PeerMessage(peer_pubkey=PEER_PUBKEY, payload=PAYLOAD)
            await messages.aclose()
        assert stream.cancelled

    async def test_cancellation_propagates_and_cancels_the_stream(self) -> None:
        stream = _HangingStream([custom_message()])
        received: list[PeerMessage] = []
        async with peer_client(RecordingStub(SubscribeCustomMessages=stream)) as client:

            async def consume() -> None:
                async for message in client.receive():
                    received.append(message)

            task = asyncio.create_task(consume())
            for _ in range(10):
                await asyncio.sleep(0)
                if received:
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert received == [PeerMessage(peer_pubkey=PEER_PUBKEY, payload=PAYLOAD)]
        assert stream.cancelled


class TestNodeInfo:
    async def test_maps_the_chain_view(self) -> None:
        stub = RecordingStub(GetInfo=info_response())
        async with peer_client(stub) as client:
            assert await client.node_info() == NodeInfo(
                identity_pubkey=NODE_PUBKEY.hex(),
                block_height=812_345,
                synced_to_chain=True,
                network="regtest",
            )

    async def test_reports_an_unsynced_node_without_judging_it(self) -> None:
        stub = RecordingStub(GetInfo=info_response(synced_to_chain=False))
        async with peer_client(stub) as client:
            assert (await client.node_info()).synced_to_chain is False

    @pytest.mark.parametrize(
        "overrides",
        [
            {"chains": []},
            {"chains": [pb.Chain(chain="bitcoin", network="regtest"), pb.Chain(network="signet")]},
            {"chains": [pb.Chain(chain="litecoin", network="mainnet")]},
            {"chains": [pb.Chain(chain="bitcoin", network="")]},
            {"identity_pubkey": ""},
            {"identity_pubkey": NODE_PUBKEY.hex().upper()},
            {"identity_pubkey": "zz" * 33},
        ],
    )
    async def test_rejects_a_malformed_info(self, overrides: dict[str, Any]) -> None:
        stub = RecordingStub(GetInfo=info_response(**overrides))
        async with peer_client(stub) as client:
            with pytest.raises(LndPeerResponseError):
                await client.node_info()


class TestChannels:
    async def test_maps_the_metadata_eligibility_is_judged_on(self) -> None:
        stub = RecordingStub(
            ListChannels=pb.ListChannelsResponse(
                channels=[channel(pending_htlcs=[pb.HTLC(amount=1), pb.HTLC(amount=2)])]
            )
        )
        async with peer_client(stub) as client:
            assert await client.channels() == [
                ChannelInfo(
                    point=POINT,
                    peer_pubkey=PEER_PUBKEY.hex(),
                    active=True,
                    private=True,
                    capacity_sat=1_000_000,
                    local_balance_sat=600_000,
                    remote_balance_sat=399_000,
                    pending_htlcs=2,
                    commitment_type="SIMPLE_TAPROOT",
                )
            ]

    async def test_lists_every_channel_the_node_reports(self) -> None:
        stub = RecordingStub(ListChannels=pb.ListChannelsResponse(channels=[]))
        async with peer_client(stub) as client:
            assert await client.channels() == []
        assert stub.last.request == pb.ListChannelsRequest()

    async def test_reports_the_commitment_type_by_name(self) -> None:
        stub = RecordingStub(
            ListChannels=pb.ListChannelsResponse(
                channels=[channel(commitment_type=pb.CommitmentType.ANCHORS)]
            )
        )
        async with peer_client(stub) as client:
            assert (await client.channels())[0].commitment_type == "ANCHORS"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"channel_point": TXID},
            {"channel_point": f"{TXID}:x"},
            {"channel_point": f"{TXID.upper()}:3"},
            {"channel_point": f"{TXID}:03"},
            {"remote_pubkey": ""},
            {"capacity": 0},
            {"capacity": -1},
            {"local_balance": -1},
            {"local_balance": 900_000, "remote_balance": 200_000},
        ],
    )
    async def test_rejects_a_malformed_channel(self, overrides: dict[str, Any]) -> None:
        stub = RecordingStub(ListChannels=pb.ListChannelsResponse(channels=[channel(**overrides)]))
        async with peer_client(stub) as client:
            with pytest.raises(LndPeerResponseError):
                await client.channels()


class TestNewAddress:
    async def test_requests_a_p2tr_address(self) -> None:
        stub = RecordingStub(NewAddress=pb.NewAddressResponse(address="bcrt1ptestaddress"))
        async with peer_client(stub) as client:
            assert await client.new_address() == "bcrt1ptestaddress"
        assert stub.last.request == pb.NewAddressRequest(type=pb.AddressType.TAPROOT_PUBKEY)

    async def test_rejects_an_empty_address(self) -> None:
        stub = RecordingStub(NewAddress=pb.NewAddressResponse())
        async with peer_client(stub) as client:
            with pytest.raises(LndPeerResponseError):
                await client.new_address()


class TestCreateInvoice:
    async def test_private_routes_are_explicitly_requested(self) -> None:
        stub = RecordingStub(
            AddInvoice=pb.AddInvoiceResponse(r_hash=PAYMENT_HASH, payment_request=BOLT11)
        )
        async with peer_client(stub) as client:
            assert (
                await client.create_invoice(PREIMAGE, 1_000, 600, include_private_routes=True)
                == BOLT11
            )
        assert stub.last.request.private is True

    async def test_invoice_request_recovery_checks_the_payment_hash(self) -> None:
        stub = RecordingStub(LookupInvoice=pb.Invoice(r_hash=PAYMENT_HASH, payment_request=BOLT11))
        async with peer_client(stub) as client:
            assert await client.invoice_request(PAYMENT_HASH) == BOLT11
        stub = RecordingStub(LookupInvoice=pb.Invoice(r_hash=bytes(32), payment_request=BOLT11))
        async with peer_client(stub) as client:
            with pytest.raises(LndPeerResponseError):
                await client.invoice_request(PAYMENT_HASH)

    async def test_adds_the_invoice_for_the_supplied_preimage(self) -> None:
        stub = RecordingStub(
            AddInvoice=pb.AddInvoiceResponse(r_hash=PAYMENT_HASH, payment_request=BOLT11)
        )
        async with peer_client(stub) as client:
            assert await client.create_invoice(PREIMAGE, 1_000, 600) == BOLT11
        assert stub.last.request == pb.Invoice(r_preimage=PREIMAGE, value=1_000, expiry=600)

    @pytest.mark.parametrize(
        "args",
        [
            (PREIMAGE[:-1], 1_000, 600),
            (PREIMAGE.hex(), 1_000, 600),
            (PREIMAGE, 0, 600),
            (PREIMAGE, -1, 600),
            (PREIMAGE, True, 600),
            (PREIMAGE, 1_000, 0),
            (PREIMAGE, 1_000, -600),
        ],
    )
    async def test_rejects_unusable_arguments(self, args: tuple[Any, ...]) -> None:
        stub = RecordingStub(AddInvoice=pb.AddInvoiceResponse())
        async with peer_client(stub) as client:
            with pytest.raises(ValueError):
                await client.create_invoice(*args)
        assert stub.calls == []

    @pytest.mark.parametrize(
        "response",
        [
            pb.AddInvoiceResponse(r_hash=bytes(32), payment_request=BOLT11),
            pb.AddInvoiceResponse(r_hash=PAYMENT_HASH[:-1], payment_request=BOLT11),
            pb.AddInvoiceResponse(payment_request=BOLT11),
            pb.AddInvoiceResponse(r_hash=PAYMENT_HASH),
        ],
    )
    async def test_rejects_an_invoice_that_is_not_the_requested_one(
        self, response: pb.AddInvoiceResponse
    ) -> None:
        stub = RecordingStub(AddInvoice=response)
        async with peer_client(stub) as client:
            with pytest.raises(LndPeerResponseError):
                await client.create_invoice(PREIMAGE, 1_000, 600)


class TestInspectInvoice:
    async def test_maps_the_decoded_invoice(self) -> None:
        stub = RecordingStub(DecodePayReq=pay_req())
        async with peer_client(stub) as client:
            assert await client.inspect_invoice(BOLT11) == InvoiceInfo(
                payment_hash=PAYMENT_HASH,
                destination=DESTINATION,
                amount_sat=1_000,
                created_at=1_700_000_000,
                expiry_seconds=600,
                min_final_cltv=80,
            )
        assert stub.last.request == pb.PayReqString(pay_req=BOLT11)

    @pytest.mark.parametrize("bolt11", ["", "xnbcrt1", BOLT11.upper(), "ln" + "a" * 4_000, 1234])
    async def test_rejects_an_unusable_payment_request(self, bolt11: Any) -> None:
        stub = RecordingStub(DecodePayReq=pay_req())
        async with peer_client(stub) as client:
            with pytest.raises(ValueError):
                await client.inspect_invoice(bolt11)
        assert stub.calls == []

    @pytest.mark.parametrize(
        "overrides",
        [
            {"num_satoshis": 0, "num_msat": 0},
            {"num_satoshis": -1, "num_msat": -1_000},
            {"num_msat": 1_000_500},
            {"num_msat": 0},
            {"timestamp": 0},
            {"expiry": 0},
            {"cltv_expiry": 0},
            {"payment_hash": PAYMENT_HASH.hex()[:-1]},
            {"destination": ""},
        ],
    )
    async def test_rejects_an_invoice_a_settlement_cannot_rely_on(
        self, overrides: dict[str, Any]
    ) -> None:
        stub = RecordingStub(DecodePayReq=pay_req(**overrides))
        async with peer_client(stub) as client:
            with pytest.raises(LndPeerResponseError):
                await client.inspect_invoice(BOLT11)


class TestInvoiceStatus:
    async def test_maps_a_settled_invoice(self) -> None:
        stub = RecordingStub(LookupInvoice=invoice())
        async with peer_client(stub) as client:
            assert await client.invoice_status(PAYMENT_HASH) == InvoiceStatus(
                state=InvoiceState.SETTLED, amount_paid_sat=1_000, preimage=PREIMAGE
            )
        assert stub.last.request == pb.PaymentHash(r_hash=PAYMENT_HASH)

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (pb.Invoice.InvoiceState.OPEN, InvoiceState.OPEN),
            (pb.Invoice.InvoiceState.ACCEPTED, InvoiceState.ACCEPTED),
            (pb.Invoice.InvoiceState.CANCELED, InvoiceState.CANCELED),
        ],
    )
    async def test_maps_every_unsettled_state(self, state: int, expected: InvoiceState) -> None:
        stub = RecordingStub(LookupInvoice=invoice(state=state, r_preimage=b"", amt_paid_sat=0))
        async with peer_client(stub) as client:
            status = await client.invoice_status(PAYMENT_HASH)
        assert status == InvoiceStatus(state=expected, amount_paid_sat=0, preimage=None)

    @pytest.mark.parametrize("payment_hash", [PAYMENT_HASH[:-1], PAYMENT_HASH.hex(), b""])
    async def test_rejects_an_unusable_payment_hash(self, payment_hash: Any) -> None:
        stub = RecordingStub(LookupInvoice=invoice())
        async with peer_client(stub) as client:
            with pytest.raises(ValueError):
                await client.invoice_status(payment_hash)
        assert stub.calls == []

    @pytest.mark.parametrize(
        "overrides",
        [
            {"r_hash": bytes(32)},
            {"r_preimage": bytes(32)},
            {"r_preimage": b""},
            {"amt_paid_sat": -1},
        ],
    )
    async def test_rejects_a_contradictory_invoice(self, overrides: dict[str, Any]) -> None:
        stub = RecordingStub(LookupInvoice=invoice(**overrides))
        async with peer_client(stub) as client:
            with pytest.raises(LndPeerResponseError):
                await client.invoice_status(PAYMENT_HASH)


class TestPay:
    async def test_pays_once_as_a_single_part_payment(self) -> None:
        stream = _Stream([payment()])
        stub = RecordingStub(SendPaymentV2=stream)
        async with peer_client(router=stub) as client:
            result = await client.pay(BOLT11, fee_limit_sat=10, cltv_limit=144, timeout_seconds=60)
        assert result == PaymentResult(
            status=PaymentStatus.SUCCEEDED,
            payment_hash=PAYMENT_HASH,
            preimage=PREIMAGE,
            fee_sat=7,
        )
        assert stub.last.request == router_pb.SendPaymentRequest(
            payment_request=BOLT11,
            fee_limit_sat=10,
            cltv_limit=144,
            timeout_seconds=60,
            max_parts=1,
            no_inflight_updates=True,
        )
        assert stub.last.timeout == 60 + TIMEOUT
        assert stream.cancelled

    async def test_sends_exactly_one_attempt(self) -> None:
        stub = RecordingStub(SendPaymentV2=_Stream([payment()]))
        async with peer_client(router=stub) as client:
            await client.pay(BOLT11, fee_limit_sat=0, cltv_limit=1, timeout_seconds=1)
        assert len(stub.calls) == 1

    async def test_does_not_retry_a_failed_attempt(self) -> None:
        stub = RecordingStub(
            SendPaymentV2=_Stream([], error=rpc_error(grpc.StatusCode.UNAVAILABLE))
        )
        async with peer_client(router=stub) as client:
            with pytest.raises(LndPeerRpcError):
                await client.pay(BOLT11, fee_limit_sat=10, cltv_limit=144, timeout_seconds=60)
        assert len(stub.calls) == 1

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"fee_limit_sat": -1},
            {"cltv_limit": 0},
            {"cltv_limit": -144},
            {"timeout_seconds": 0},
            {"timeout_seconds": -60},
            {"fee_limit_sat": 1.5},
            {"cltv_limit": True},
        ],
    )
    async def test_rejects_unbounded_or_negative_limits(self, kwargs: dict[str, Any]) -> None:
        stub = RecordingStub(SendPaymentV2=_Stream([payment()]))
        fields: dict[str, Any] = {"fee_limit_sat": 10, "cltv_limit": 144, "timeout_seconds": 60}
        fields.update(kwargs)
        async with peer_client(router=stub) as client:
            with pytest.raises(ValueError):
                await client.pay(BOLT11, **fields)
        assert stub.calls == []

    async def test_reports_a_failed_payment(self) -> None:
        stub = RecordingStub(
            SendPaymentV2=_Stream(
                [payment(status=pb.Payment.PaymentStatus.FAILED, payment_preimage="", fee_sat=0)]
            )
        )
        async with peer_client(router=stub) as client:
            result = await client.pay(BOLT11, fee_limit_sat=10, cltv_limit=144, timeout_seconds=60)
        assert result == PaymentResult(
            status=PaymentStatus.FAILED, payment_hash=PAYMENT_HASH, preimage=None, fee_sat=0
        )

    async def test_reads_a_zero_preimage_as_no_preimage(self) -> None:
        stub = RecordingStub(
            SendPaymentV2=_Stream(
                [
                    payment(
                        status=pb.Payment.PaymentStatus.FAILED,
                        payment_preimage=bytes(32).hex(),
                        fee_sat=0,
                    )
                ]
            )
        )
        async with peer_client(router=stub) as client:
            result = await client.pay(BOLT11, fee_limit_sat=10, cltv_limit=144, timeout_seconds=60)
        assert result.preimage is None

    @pytest.mark.parametrize(
        "update",
        [
            payment(payment_preimage=bytes([0x43] * 32).hex()),
            payment(payment_preimage=""),
            payment(payment_hash=PAYMENT_HASH.hex()[:-1]),
            payment(status=pb.Payment.PaymentStatus.FAILED),
            payment(status=pb.Payment.PaymentStatus.UNKNOWN),
            payment(fee_sat=-1),
        ],
    )
    async def test_rejects_a_result_that_does_not_hold_together(self, update: pb.Payment) -> None:
        stub = RecordingStub(SendPaymentV2=_Stream([update]))
        async with peer_client(router=stub) as client:
            with pytest.raises(LndPeerResponseError):
                await client.pay(BOLT11, fee_limit_sat=10, cltv_limit=144, timeout_seconds=60)

    async def test_rejects_a_stream_that_ends_without_a_result(self) -> None:
        stream = _Stream([])
        async with peer_client(router=RecordingStub(SendPaymentV2=stream)) as client:
            with pytest.raises(LndPeerResponseError):
                await client.pay(BOLT11, fee_limit_sat=10, cltv_limit=144, timeout_seconds=60)
        assert stream.cancelled


def hop(**overrides: Any) -> PaymentRouteHop:
    fields: dict[str, Any] = {
        "node_id": DESTINATION.hex(),
        "chan_id": 123_456_789,
        "fee_base_msat": 1_000,
        "fee_proportional_millionths": 1,
        "cltv_expiry_delta": 80,
    }
    fields.update(overrides)
    return PaymentRouteHop(**fields)


HINTS = ((hop(),),)


class TestPaymentRouteHints:
    """Operator route hints are payer-only input to SendPaymentV2."""

    async def test_no_hints_is_the_default_and_sends_none(self) -> None:
        stub = RecordingStub(SendPaymentV2=_Stream([payment()]))
        async with peer_client(router=stub) as client:
            await client.pay(BOLT11, fee_limit_sat=10, cltv_limit=144, timeout_seconds=60)
        assert list(stub.last.request.route_hints) == []

    async def test_configured_hints_reach_the_payment_request_unchanged(self) -> None:
        stub = RecordingStub(SendPaymentV2=_Stream([payment()]))
        async with peer_client(router=stub, payment_route_hints=HINTS) as client:
            await client.pay(BOLT11, fee_limit_sat=10, cltv_limit=144, timeout_seconds=60)
        assert stub.last.request == router_pb.SendPaymentRequest(
            payment_request=BOLT11,
            fee_limit_sat=10,
            cltv_limit=144,
            timeout_seconds=60,
            max_parts=1,
            no_inflight_updates=True,
            route_hints=[
                pb.RouteHint(
                    hop_hints=[
                        pb.HopHint(
                            node_id=DESTINATION.hex(),
                            chan_id=123_456_789,
                            fee_base_msat=1_000,
                            fee_proportional_millionths=1,
                            cltv_expiry_delta=80,
                        )
                    ]
                )
            ],
        )
        assert stub.last.timeout == 60 + TIMEOUT

    async def test_every_hop_of_every_hint_is_sent_in_order(self) -> None:
        hints = ((hop(chan_id=1), hop(chan_id=2)), (hop(chan_id=3),))
        stub = RecordingStub(SendPaymentV2=_Stream([payment()]))
        async with peer_client(router=stub, payment_route_hints=hints) as client:
            await client.pay(BOLT11, fee_limit_sat=10, cltv_limit=144, timeout_seconds=60)
        sent = [[int(h.chan_id) for h in hint.hop_hints] for hint in stub.last.request.route_hints]
        assert sent == [[1, 2], [3]]

    async def test_hints_never_reach_an_invoice(self) -> None:
        stub = RecordingStub(
            AddInvoice=pb.AddInvoiceResponse(r_hash=PAYMENT_HASH, payment_request=BOLT11)
        )
        async with peer_client(stub, payment_route_hints=HINTS) as client:
            assert await client.create_invoice(PREIMAGE, 1_000, 600) == BOLT11
        assert stub.last.request == pb.Invoice(r_preimage=PREIMAGE, value=1_000, expiry=600)
        assert list(stub.last.request.route_hints) == []

    @pytest.mark.parametrize(
        "hints",
        [
            [(hop(),)],
            ((hop(),),) + ((),),
            ([hop()],),
            ((hop().model_dump(),),),
            ((hop(),) * (MAX_ROUTE_HINT_HOPS + 1),),
            ((hop(),),) * (MAX_ROUTE_HINT_PATHS + 1),
        ],
    )
    def test_rejects_hints_that_are_not_bounded_tuples_of_hops(self, hints: Any) -> None:
        with pytest.raises(ValueError) as error:
            LndPeerClient("127.0.0.1:10009", TLS_CERT, MACAROON, payment_route_hints=hints)
        assert DESTINATION.hex() not in str(error.value)
        assert "123456789" not in str(error.value)

    def test_the_bounds_are_inclusive(self) -> None:
        LndPeerClient(
            "127.0.0.1:10009",
            TLS_CERT,
            MACAROON,
            payment_route_hints=((hop(),) * MAX_ROUTE_HINT_HOPS,) * MAX_ROUTE_HINT_PATHS,
        )

    def test_hints_are_not_in_the_client_repr(self) -> None:
        client = LndPeerClient("127.0.0.1:10009", TLS_CERT, MACAROON, payment_route_hints=HINTS)

        assert "route" not in repr(client)
        assert DESTINATION.hex() not in repr(client)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"node_id": DESTINATION.hex()[:-1]},
            {"node_id": DESTINATION},
            {"chan_id": 0},
            {"chan_id": -1},
            {"chan_id": 2**64},
            {"chan_id": True},
            {"fee_base_msat": -1},
            {"fee_base_msat": 2**32},
            {"fee_proportional_millionths": -1},
            {"fee_proportional_millionths": "1"},
            {"cltv_expiry_delta": 0},
            {"cltv_expiry_delta": 65_536},
        ],
    )
    def test_a_hop_outside_lnds_ranges_is_refused(self, overrides: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            hop(**overrides)

    def test_a_hop_is_frozen_and_closed(self) -> None:
        with pytest.raises(ValueError):
            hop(extra=1)
        with pytest.raises(ValueError):
            hop().chan_id = 2


class TestTrackPayment:
    async def test_returns_the_current_result(self) -> None:
        stream = _Stream(
            [payment(status=pb.Payment.PaymentStatus.IN_FLIGHT, payment_preimage="", fee_sat=0)]
        )
        stub = RecordingStub(TrackPaymentV2=stream)
        async with peer_client(router=stub) as client:
            assert await client.track_payment(PAYMENT_HASH) == PaymentResult(
                status=PaymentStatus.IN_FLIGHT,
                payment_hash=PAYMENT_HASH,
                preimage=None,
                fee_sat=0,
            )
        assert stub.last.request == router_pb.TrackPaymentRequest(
            payment_hash=PAYMENT_HASH, no_inflight_updates=False
        )
        assert stub.last.timeout == TIMEOUT
        assert stream.cancelled

    async def test_reads_an_initiated_payment_as_in_flight(self) -> None:
        stub = RecordingStub(
            TrackPaymentV2=_Stream(
                [payment(status=pb.Payment.PaymentStatus.INITIATED, payment_preimage="", fee_sat=0)]
            )
        )
        async with peer_client(router=stub) as client:
            result = await client.track_payment(PAYMENT_HASH)
        assert result is not None and result.status is PaymentStatus.IN_FLIGHT

    async def test_an_unknown_payment_is_not_an_error(self) -> None:
        stub = RecordingStub(
            TrackPaymentV2=_Stream([], error=rpc_error(grpc.StatusCode.NOT_FOUND, "no payment"))
        )
        async with peer_client(router=stub) as client:
            assert await client.track_payment(PAYMENT_HASH) is None

    async def test_an_empty_stream_is_not_an_error(self) -> None:
        stub = RecordingStub(TrackPaymentV2=_Stream([]))
        async with peer_client(router=stub) as client:
            assert await client.track_payment(PAYMENT_HASH) is None

    async def test_other_failures_propagate_sanitized(self) -> None:
        stub = RecordingStub(
            TrackPaymentV2=_Stream(
                [], error=rpc_error(grpc.StatusCode.PERMISSION_DENIED, "macaroon detail")
            )
        )
        async with peer_client(router=stub) as client:
            with pytest.raises(LndPeerRpcError) as raised:
                await client.track_payment(PAYMENT_HASH)
        assert raised.value.code is grpc.StatusCode.PERMISSION_DENIED
        assert "macaroon detail" not in str(raised.value)

    async def test_rejects_a_result_for_another_payment(self) -> None:
        other = hashlib.sha256(b"other").digest()
        stub = RecordingStub(
            TrackPaymentV2=_Stream(
                [
                    payment(
                        payment_hash=other.hex(),
                        status=pb.Payment.PaymentStatus.IN_FLIGHT,
                        payment_preimage="",
                    )
                ]
            )
        )
        async with peer_client(router=stub) as client:
            with pytest.raises(LndPeerResponseError):
                await client.track_payment(PAYMENT_HASH)

    @pytest.mark.parametrize("payment_hash", [PAYMENT_HASH[:-1], PAYMENT_HASH.hex()])
    async def test_rejects_an_unusable_payment_hash(self, payment_hash: Any) -> None:
        stub = RecordingStub(TrackPaymentV2=_Stream([payment()]))
        async with peer_client(router=stub) as client:
            with pytest.raises(ValueError):
                await client.track_payment(payment_hash)
        assert stub.calls == []


class TestCloseChannel:
    async def test_returns_on_the_first_update(self) -> None:
        stream = _Stream(
            [
                pb.CloseStatusUpdate(close_pending=pb.PendingUpdate(txid=TXID_INTERNAL)),
                pb.CloseStatusUpdate(chan_close=pb.ChannelCloseUpdate()),
            ]
        )
        stub = RecordingStub(CloseChannel=stream)
        async with peer_client(stub) as client:
            assert await client.close_channel(POINT, force=False) is None
        assert stub.last.request == pb.CloseChannelRequest(
            channel_point=pb.ChannelPoint(funding_txid_bytes=TXID_INTERNAL, output_index=3),
            force=False,
        )
        assert stub.last.timeout == TIMEOUT
        # The second update (the confirmed close) is never waited for.
        assert stream.cancelled
        assert len(stream.updates) == 1

    async def test_forwards_a_force_close(self) -> None:
        stub = RecordingStub(
            CloseChannel=_Stream([pb.CloseStatusUpdate(close_pending=pb.PendingUpdate())])
        )
        async with peer_client(stub) as client:
            await client.close_channel(POINT, force=True)
        assert stub.last.request.force is True

    async def test_rejects_a_point_that_is_not_an_outpoint(self) -> None:
        stub = RecordingStub(CloseChannel=_Stream([]))
        async with peer_client(stub) as client:
            with pytest.raises(ValueError):
                await client.close_channel(f"{TXID}:3", force=False)
        assert stub.calls == []

    @pytest.mark.parametrize("updates", [[], [pb.CloseStatusUpdate()]])
    async def test_rejects_a_close_the_node_did_not_accept(
        self, updates: list[pb.CloseStatusUpdate]
    ) -> None:
        async with peer_client(RecordingStub(CloseChannel=_Stream(updates))) as client:
            with pytest.raises(LndPeerResponseError):
                await client.close_channel(POINT, force=False)

    async def test_a_rejected_close_propagates_sanitized(self) -> None:
        stub = RecordingStub(
            CloseChannel=_Stream(
                [], error=rpc_error(grpc.StatusCode.FAILED_PRECONDITION, "channel escrow locked")
            )
        )
        async with peer_client(stub) as client:
            with pytest.raises(LndPeerRpcError) as raised:
                await client.close_channel(POINT, force=False)
        assert raised.value.code is grpc.StatusCode.FAILED_PRECONDITION
        assert "escrow locked" not in str(raised.value)
