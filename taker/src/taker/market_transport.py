"""Bounded private transport for the credential market.

The module deliberately owns only discovery, authenticated message envelopes,
sealed-box request/reply exchange, and direct-peer routing.  Listing, request,
response, and fault-proof semantics remain with the caller.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from jmcore.credential_market import MAX_MARKET_BYTES, MarketError, canonical, decode_document
from jmcore.crypto import NickIdentity, verify_signed_privmsg
from jmcore.directory_client import DirectoryClient, DirectoryClientError
from jmcore.network import ONION_HOSTID, HiddenServiceListener, OnionPeer, TCPConnection
from jmcore.network import ConnectionError as NetworkConnectionError
from jmcore.nick_auth import NickAuthMode
from jmcore.protocol import (
    JM_VERSION,
    NOT_SERVING_ONION_HOSTNAME,
    MessageType,
    create_handshake_request,
    is_onion_peer_location,
    is_valid_nick,
    parse_peer_location,
)
from jmcore.tasks import parse_directory_address
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox

from taker.multi_directory import MultiDirectoryClient

MAX_LISTING_BYTES = 2048
MAX_LISTINGS = 256
MAX_IN_FLIGHT = 64
MAX_WIRE_BYTES = 32768
_SEALED_BOX_OVERHEAD = 48
_MAX_CIPHERTEXT_BYTES = MAX_MARKET_BYTES + _SEALED_BOX_OVERHEAD
_MAX_RESPONSE_CACHE = 256


class MarketTransportError(Exception):
    """Raised when the market transport cannot complete an operation."""


class MarketRequestTimeoutError(MarketTransportError, TimeoutError):
    """Raised when neither the direct nor directory path returns a response."""


ListingCallback = Callable[[], bytes | Awaitable[bytes]]
ResponderCallback = Callable[[str, dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]
FaultCallback = Callable[[bytes], None | Awaitable[None]]
CallbackT = TypeVar("CallbackT")


@dataclass(slots=True)
class _Inbound:
    """A small, pre-bounded message awaiting transport dispatch."""

    message_type: int
    line: str
    directory: DirectoryClient | None = None
    connection: TCPConnection | None = None


@dataclass(slots=True)
class _PendingRequest:
    """One caller-owned request correlated by its random request identifier."""

    seller_nick: str
    reply_key: PrivateKey
    future: asyncio.Future[dict[str, Any]]


async def _resolve(value: CallbackT | Awaitable[CallbackT]) -> CallbackT:
    """Await callback output only when the caller supplied an async callback."""
    if inspect.isawaitable(value):
        return cast(CallbackT, await value)
    return value


class MarketTransport(MultiDirectoryClient):
    """Market-only discovery and private request transport.

    ``nick_identity`` is a dedicated market identity.  It must not be reused by
    CoinJoin sessions.  A provider supplies ``encryption_private_key`` to decrypt
    sealed requests, plus optional ``listing_callback`` and ``responder``
    callbacks.  The callback payloads are opaque canonical JSON documents to this
    transport.

    ``direct_location`` is the public onion ``host:port`` advertised during the
    directory handshake.  When ``listen_host`` is configured, an external Tor
    hidden service must map that advertised port to the listener; this module
    intentionally does not create or manage the Tor service.
    """

    def __init__(
        self,
        *,
        directory_servers: list[str],
        network: str,
        nick_identity: NickIdentity,
        socks_host: str = "127.0.0.1",
        socks_port: int = 9050,
        connection_timeout: float = 30.0,
        stream_isolation: bool = True,
        nick_auth_mode: NickAuthMode = NickAuthMode.PREFER_VERIFIED,
        nick_auth_directory_ids: dict[str, str] | None = None,
        encryption_private_key: PrivateKey | None = None,
        listing_callback: ListingCallback | None = None,
        responder: ResponderCallback | None = None,
        on_fault: FaultCallback | None = None,
        direct_location: str = NOT_SERVING_ONION_HOSTNAME,
        listen_host: str | None = None,
        listen_port: int = 0,
        allow_clearnet_connections: bool = False,
    ) -> None:
        if not isinstance(encryption_private_key, (PrivateKey, type(None))):
            raise TypeError("encryption_private_key must be a nacl.public.PrivateKey")
        if listen_host is None and listen_port:
            raise ValueError("listen_port requires listen_host")
        self._validate_direct_location(network, direct_location, allow_clearnet_connections)
        super().__init__(
            directory_servers=directory_servers,
            network=network,
            nick_identity=nick_identity,
            socks_host=socks_host,
            socks_port=socks_port,
            connection_timeout=connection_timeout,
            neutrino_compat=False,
            prefer_direct_connections=True,
            our_location=direct_location,
            stream_isolation=stream_isolation,
            nick_auth_mode=nick_auth_mode,
            nick_auth_directory_ids=nick_auth_directory_ids,
            # Clearnet direct peers are never permitted outside regtest.
            allow_clearnet_connections=allow_clearnet_connections,
        )
        self.encryption_private_key = encryption_private_key
        self.listing_callback = listing_callback
        self.responder = responder
        self.on_fault = on_fault
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.hidden_service_listener: HiddenServiceListener | None = None

        self.running = False
        self._direct_message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=MAX_IN_FLIGHT
        )
        self._inbound: asyncio.Queue[_Inbound] = asyncio.Queue(maxsize=MAX_IN_FLIGHT)
        self._listings: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue(maxsize=MAX_LISTINGS)
        self._work_slots = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._request_slots = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._discover_lock = asyncio.Lock()
        self._pending: dict[str, _PendingRequest] = {}
        self._response_cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._responding: dict[tuple[str, str], asyncio.Event] = {}
        self._incoming_connections: set[TCPConnection] = set()
        self._workers: set[asyncio.Task[None]] = set()
        self._directory_task: asyncio.Task[None] | None = None
        self._dispatch_task: asyncio.Task[None] | None = None

    @staticmethod
    def _validate_direct_location(
        network: str, location: str, allow_clearnet_connections: bool = False
    ) -> None:
        if location == NOT_SERVING_ONION_HOSTNAME:
            return
        try:
            parse_peer_location(location)
        except ValueError as exc:
            raise ValueError("direct_location must be a valid host:port") from exc
        if (
            network != "regtest"
            and not allow_clearnet_connections
            and not is_onion_peer_location(location)
        ):
            raise ValueError("direct_location must be an onion service outside regtest")

    def _build_client_kwargs(self, host: str, port: int) -> dict[str, Any]:
        """Advertise only this market identity and its optional direct location."""
        kwargs = super()._build_client_kwargs(host, port)
        kwargs["location"] = self.our_location
        kwargs["max_message_size"] = MAX_WIRE_BYTES
        return kwargs

    async def start(self) -> None:
        """Start the optional direct listener, directory clients, and bounded dispatchers."""
        if self.running:
            return
        self.running = True
        try:
            if self.listen_host is not None:
                self.hidden_service_listener = HiddenServiceListener(
                    host=self.listen_host,
                    port=self.listen_port,
                    max_message_size=MAX_WIRE_BYTES,
                    on_connection=self._on_direct_connection,
                )
                await self.hidden_service_listener.start()
            await self.connect_all()
            if self.directory_servers and not self.clients:
                raise MarketTransportError("No market directory connection available")
            self._directory_task = asyncio.create_task(
                self._directory_loop(), name="market-directory-listener"
            )
            self._dispatch_task = asyncio.create_task(
                self._dispatch_loop(), name="market-message-dispatcher"
            )
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Cancel bounded work and close direct and directory resources."""
        self.running = False
        for task in (self._directory_task, self._dispatch_task):
            if task is not None:
                task.cancel()
        tasks = [task for task in (self._directory_task, self._dispatch_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._directory_task = None
        self._dispatch_task = None

        for task in tuple(self._workers):
            task.cancel()
        if self._workers:
            await asyncio.gather(*tuple(self._workers), return_exceptions=True)
        self._workers.clear()

        for connection in tuple(self._incoming_connections):
            with contextlib.suppress(Exception):
                await connection.close()
        self._incoming_connections.clear()
        if self.hidden_service_listener is not None:
            await self._stop_direct_listener(self.hidden_service_listener)
            self.hidden_service_listener = None

        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(MarketTransportError("Market transport closed"))
        self._pending.clear()
        await super().close_all()

    async def discover(self, timeout: float = 30.0) -> list[tuple[str, bytes]]:
        """Broadcast ``mbook`` and collect at most 256 signed canonical listings."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not self.running:
            raise MarketTransportError("Market transport is not started")
        async with self._discover_lock:
            self._drain_listings()
            for client in tuple(self.clients.values()):
                try:
                    await client.send_public_message("mbook")
                except Exception:
                    continue

            deadline = asyncio.get_running_loop().time() + timeout
            found: list[tuple[str, bytes]] = []
            seen: set[tuple[str, bytes]] = set()
            while len(found) < MAX_LISTINGS:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._listings.get(), timeout=remaining)
                except TimeoutError:
                    break
                if item not in seen:
                    seen.add(item)
                    found.append(item)
            return found

    async def request(
        self,
        seller_nick: str,
        seller_encryption_pubkey: bytes,
        payload: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send one sealed request, preferring direct onion transport then one relay.

        The same request id, reply key, and ciphertext are reused for the relay
        fallback.  Providers can consequently return their cached response rather
        than running a non-idempotent responder callback twice.
        """
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not self.running:
            raise MarketTransportError("Market transport is not started")
        if not is_valid_nick(seller_nick):
            raise ValueError("seller_nick is not a valid JoinMarket nick")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a JSON object")
        try:
            seller_key = PublicKey(seller_encryption_pubkey)
        except (TypeError, ValueError) as exc:
            raise ValueError("seller_encryption_pubkey must be 32 bytes") from exc

        async with self._request_slots:
            async with self._work_slots:
                reply_key = PrivateKey.generate()
                request_id = secrets.token_hex(16)
                request_body = self._canonical_document(
                    {
                        "request_id": request_id,
                        "reply_pubkey": self._encode_base64(bytes(reply_key.public_key)),
                        "body": payload,
                    }
                )
                request_data = self._encode_base64(
                    bytes(SealedBox(seller_key).encrypt(request_body))
                )

            loop = asyncio.get_running_loop()
            pending = _PendingRequest(
                seller_nick=seller_nick,
                reply_key=reply_key,
                future=loop.create_future(),
            )
            self._pending[request_id] = pending
            deadline = loop.time() + timeout
            try:
                direct_wait = max(0.0, timeout / 3)
                peer = await self._establish_direct(
                    seller_nick, min(direct_wait, self._remaining(deadline))
                )
                if peer is not None:
                    sent = await peer.send_privmsg(self.nick, "mrequest", request_data)
                    if sent:
                        response = await self._wait_pending(
                            pending, min(direct_wait, self._remaining(deadline))
                        )
                        if response is not None:
                            return response

                await self._send_relay_once(seller_nick, "mrequest", request_data)
                response = await self._wait_pending(pending, self._remaining(deadline))
                if response is not None:
                    return response
                raise MarketRequestTimeoutError("Timed out waiting for market response")
            finally:
                self._pending.pop(request_id, None)

    async def broadcast_fault(self, proof: bytes) -> int:
        """Broadcast a bounded canonical proof through fresh one-shot identities.

        Fault evidence is intentionally not verified here.  A fresh directory
        identity is used for every attempted directory so publishing evidence does
        not reuse the identity that negotiated a market request.
        """
        raw = self._canonical_document(proof)
        data = self._encode_base64(raw)
        sent = 0
        for server in self.directory_servers:
            try:
                host, port = parse_directory_address(server)
                identity = NickIdentity(JM_VERSION)
                client = DirectoryClient(
                    host=host,
                    port=port,
                    network=self.network,
                    nick_identity=identity,
                    location=NOT_SERVING_ONION_HOSTNAME,
                    socks_host=self.socks_host,
                    socks_port=self.socks_port,
                    timeout=self.connection_timeout,
                    max_message_size=MAX_WIRE_BYTES,
                    socks_username=secrets.token_hex(16),
                    socks_password=secrets.token_hex(16),
                    allow_clearnet_connections=self.allow_clearnet_connections,
                    nick_auth_mode=self.nick_auth_mode,
                    nick_auth_directory_id=self.nick_auth_directory_ids.get(f"{host}:{port}"),
                )
                try:
                    await client.connect()
                    await client.send_public_message(f"mproof {data}")
                    sent += 1
                finally:
                    await client.close()
            except Exception:
                continue
        return sent

    async def _directory_loop(self) -> None:
        """Read each market-only directory stream into the bounded work queue."""
        reconnect_at = asyncio.get_running_loop().time() + 10
        while self.running:
            if self.directory_servers and asyncio.get_running_loop().time() >= reconnect_at:
                await self.reconnect_disconnected()
                reconnect_at = asyncio.get_running_loop().time() + 10
            clients = tuple(self.clients.values())
            if not clients:
                await asyncio.sleep(0.05)
                continue
            for client in clients:
                if not self.running:
                    return
                try:
                    messages = await client.listen_for_messages(duration=0.1)
                except (DirectoryClientError, asyncio.CancelledError):
                    if not self.running:
                        raise
                    continue
                except Exception:
                    continue
                for message in messages:
                    self._enqueue_directory_message(message, client)

    async def _dispatch_loop(self) -> None:
        """Create at most ``MAX_IN_FLIGHT`` tasks after acquiring the work budget."""
        while self.running:
            inbound = await self._inbound.get()
            await self._work_slots.acquire()
            worker = asyncio.create_task(self._run_inbound(inbound), name="market-inbound-message")
            self._workers.add(worker)
            worker.add_done_callback(self._worker_done)

    def _worker_done(self, task: asyncio.Task[None]) -> None:
        self._workers.discard(task)
        self._work_slots.release()

    async def _run_inbound(self, inbound: _Inbound) -> None:
        try:
            async with asyncio.timeout(self.connection_timeout):
                await self._handle_inbound(inbound)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Invalid or unavailable peer input is intentionally not logged with
            # its body, which can contain encrypted market material.
            return

    def _enqueue_directory_message(
        self, message: dict[str, Any], directory: DirectoryClient
    ) -> None:
        message_type = message.get("type")
        line = message.get("line")
        if not isinstance(message_type, int) or not isinstance(line, str):
            return
        self._enqueue(_Inbound(message_type=message_type, line=line, directory=directory))

    async def _on_peer_message(self, _nick: str, data: bytes) -> None:
        """Receive a direct OnionPeer envelope without unbounded queue growth."""
        if len(data) > MAX_WIRE_BYTES:
            return
        try:
            message = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict):
            return
        message_type = message.get("type")
        line = message.get("line")
        if not isinstance(message_type, int) or not isinstance(line, str):
            return
        self._enqueue(_Inbound(message_type=message_type, line=line))

    def _enqueue(self, inbound: _Inbound) -> None:
        if len(inbound.line.encode("utf-8")) > MAX_WIRE_BYTES:
            return
        try:
            self._inbound.put_nowait(inbound)
        except asyncio.QueueFull:
            return

    async def _handle_inbound(self, inbound: _Inbound) -> None:
        if inbound.message_type not in (MessageType.PUBMSG.value, MessageType.PRIVMSG.value):
            return
        parts = inbound.line.split("!", 2)
        if len(parts) != 3:
            return
        sender, recipient, rest = parts
        if not is_valid_nick(sender):
            return
        if inbound.message_type == MessageType.PUBMSG.value and recipient == "PUBLIC":
            await self._handle_public(sender, rest, inbound.directory)
        elif inbound.message_type == MessageType.PRIVMSG.value and recipient == self.nick:
            await self._handle_private(sender, rest, inbound)

    async def _handle_public(
        self, sender: str, rest: str, directory: DirectoryClient | None
    ) -> None:
        if rest == "mbook":
            if self.listing_callback is None or directory is None:
                return
            listing = await _resolve(self.listing_callback())
            listing_raw = self._canonical_document(listing, limit=MAX_LISTING_BYTES)
            encoded = self._encode_base64(listing_raw)
            signed = self.nick_identity.sign_message(encoded, ONION_HOSTID)
            await directory.send_public_message(f"moffer {signed}")
            return

        command, separator, data = rest.partition(" ")
        if command == "mproof" and separator and " " not in data:
            proof = self._canonical_document(self._decode_base64(data))
            if self.on_fault is not None:
                await _resolve(self.on_fault(proof))
            return

        authenticated, command, data = verify_signed_privmsg(sender, rest, ONION_HOSTID)
        if not authenticated:
            return
        if command == "moffer":
            listing = self._decode_base64(data, limit=MAX_LISTING_BYTES)
            listing = self._canonical_document(listing, limit=MAX_LISTING_BYTES)
            try:
                self._listings.put_nowait((sender, listing))
            except asyncio.QueueFull:
                return

    async def _handle_private(self, sender: str, rest: str, inbound: _Inbound) -> None:
        authenticated, command, data = verify_signed_privmsg(sender, rest, ONION_HOSTID)
        if not authenticated:
            return
        if command == "mrequest":
            await self._handle_request(sender, data, inbound)
        elif command == "mresponse":
            self._handle_response(sender, data)

    async def _handle_request(self, sender: str, data: str, inbound: _Inbound) -> None:
        if self.encryption_private_key is None or self.responder is None:
            return
        try:
            decrypted = SealedBox(self.encryption_private_key).decrypt(self._decode_base64(data))
            document = self._canonical_document(decrypted)
            request_id, reply_pubkey, body = self._request_envelope(document)
        except (CryptoError, MarketError, TypeError, ValueError):
            return

        cache_key = (sender, request_id)
        cached = self._response_cache.get(cache_key)
        if cached is not None:
            self._response_cache.move_to_end(cache_key)
            await self._reply(sender, cached, inbound)
            return
        existing = self._responding.get(cache_key)
        if existing is not None:
            await existing.wait()
            cached = self._response_cache.get(cache_key)
            if cached is not None:
                await self._reply(sender, cached, inbound)
            return

        completed = asyncio.Event()
        self._responding[cache_key] = completed
        try:
            response_body = await _resolve(self.responder(sender, body))
            if not isinstance(response_body, dict):
                return
            response_plaintext = self._canonical_document(
                {"request_id": request_id, "body": response_body}
            )
            response_data = self._encode_base64(
                bytes(SealedBox(reply_pubkey).encrypt(response_plaintext))
            )
            self._response_cache[cache_key] = response_data
            self._response_cache.move_to_end(cache_key)
            while len(self._response_cache) > _MAX_RESPONSE_CACHE:
                self._response_cache.popitem(last=False)
            await self._reply(sender, response_data, inbound)
        finally:
            self._responding.pop(cache_key, None)
            completed.set()

    def _handle_response(self, sender: str, data: str) -> None:
        try:
            encrypted = self._decode_base64(data)
        except ValueError:
            return
        for request_id, pending in tuple(self._pending.items()):
            if pending.seller_nick != sender or pending.future.done():
                continue
            try:
                plaintext = SealedBox(pending.reply_key).decrypt(encrypted)
                document = self._canonical_document(plaintext)
                response_id, body = self._response_envelope(document)
            except (CryptoError, MarketError, TypeError, ValueError):
                continue
            if response_id == request_id:
                pending.future.set_result(body)
                return

    async def _reply(self, recipient: str, data: str, inbound: _Inbound) -> None:
        if inbound.connection is not None and inbound.connection.is_connected():
            await self._send_private_connection(inbound.connection, recipient, "mresponse", data)
        elif inbound.directory is not None:
            await inbound.directory.send_private_message(recipient, "mresponse", data)

    async def _send_private_connection(
        self, connection: TCPConnection, recipient: str, command: str, data: str
    ) -> None:
        signed = self.nick_identity.sign_message(data, ONION_HOSTID)
        message = {
            "type": MessageType.PRIVMSG.value,
            "line": f"{self.nick}!{recipient}!{command} {signed}",
        }
        await connection.send(json.dumps(message, separators=(",", ":")).encode("utf-8"))

    async def _establish_direct(self, seller_nick: str, timeout: float) -> OnionPeer | None:
        if timeout <= 0:
            return None
        peer = self.get_connected_peer(seller_nick)
        if peer is not None:
            return peer
        location = self.get_peer_location(seller_nick)
        if location is None:
            return None
        peer = OnionPeer(
            nick=seller_nick,
            location=location,
            socks_host=self.socks_host,
            socks_port=self.socks_port,
            timeout=min(timeout, self.connection_timeout),
            max_message_size=MAX_WIRE_BYTES,
            on_message=self._on_peer_message,
            nick_identity=self.nick_identity,
            socks_username=self._peer_creds[0],
            socks_password=self._peer_creds[1],
            allow_clearnet_connections=self.allow_clearnet_connections,
        )
        self._peer_connections[seller_nick] = peer
        try:
            connected = await asyncio.wait_for(
                peer.connect(self.nick, self.our_location, self.network), timeout=timeout
            )
        except TimeoutError:
            connected = False
        if connected:
            return peer
        return None

    async def _send_relay_once(self, recipient: str, command: str, data: str) -> None:
        candidates = [
            client for client in self.clients.values() if recipient in client._active_peers
        ] or list(self.clients.values())
        if not candidates:
            raise MarketTransportError("No connected directory relay")
        try:
            await candidates[0].send_private_message(recipient, command, data)
        except Exception as exc:
            raise MarketTransportError("Encrypted directory relay failed") from exc

    async def _wait_pending(
        self, pending: _PendingRequest, timeout: float
    ) -> dict[str, Any] | None:
        if timeout <= 0:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), timeout=timeout)
        except TimeoutError:
            return None

    async def _on_direct_connection(self, connection: TCPConnection, _peer: str) -> None:
        """Accept only a bounded number of direct sockets before any crypto work."""
        if len(self._incoming_connections) >= MAX_IN_FLIGHT:
            await connection.close()
            return
        self._incoming_connections.add(connection)
        try:
            first = await asyncio.wait_for(connection.receive(), timeout=self.connection_timeout)
            if not await self._accept_direct_handshake(connection, first):
                return
            while self.running and connection.is_connected():
                try:
                    data = await asyncio.wait_for(
                        connection.receive(), timeout=self.connection_timeout
                    )
                except TimeoutError:
                    return
                if len(data) > MAX_WIRE_BYTES:
                    return
                try:
                    message = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                message_type = message.get("type")
                line = message.get("line")
                if not isinstance(message_type, int) or not isinstance(line, str):
                    continue
                self._enqueue(_Inbound(message_type=message_type, line=line, connection=connection))
        except (TimeoutError, ValueError, NetworkConnectionError):
            return
        finally:
            self._incoming_connections.discard(connection)
            with contextlib.suppress(Exception):
                await connection.close()

    async def _accept_direct_handshake(self, connection: TCPConnection, data: bytes) -> bool:
        if len(data) > MAX_WIRE_BYTES:
            return False
        try:
            message = json.loads(data.decode("utf-8"))
            if not isinstance(message, dict) or message.get("type") != MessageType.HANDSHAKE.value:
                return False
            line = message.get("line")
            handshake = json.loads(line) if isinstance(line, str) else None
            if not isinstance(handshake, dict):
                return False
            if (
                handshake.get("app-name") != "joinmarket"
                or handshake.get("directory") is not False
                or handshake.get("proto-ver") != JM_VERSION
                or handshake.get("network") != self.network
            ):
                return False
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        response = create_handshake_request(
            nick=self.nick,
            location=self.our_location,
            network=self.network,
            directory=False,
        )
        await connection.send(
            json.dumps(
                {"type": MessageType.HANDSHAKE.value, "line": json.dumps(response)},
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return True

    @staticmethod
    async def _stop_direct_listener(listener: HiddenServiceListener) -> None:
        """Close the listener without waiting forever on a closed peer callback.

        Python's ``Server.wait_closed`` can remain pending after a peer has
        disconnected while its stream callback unwinds.  Closing the server first
        still releases its listening socket; the short wait is only best effort.
        """
        listener.running = False
        server = listener.server
        if server is None:
            return
        server.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(server.wait_closed(), timeout=0.2)
        listener.server = None

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - asyncio.get_running_loop().time())

    @staticmethod
    def _encode_base64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    @staticmethod
    def _decode_base64(value: str, *, limit: int = _MAX_CIPHERTEXT_BYTES) -> bytes:
        if not isinstance(value, str) or not value.isascii():
            raise ValueError("Market base64 is invalid")
        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("Market base64 is invalid") from exc
        if len(decoded) > limit or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("Market base64 is invalid")
        return decoded

    @staticmethod
    def _canonical_document(raw: bytes | dict[str, Any], *, limit: int = MAX_MARKET_BYTES) -> bytes:
        if isinstance(raw, dict):
            encoded = canonical(raw)
        elif isinstance(raw, bytes):
            if len(raw) > limit:
                raise MarketError("Market document size limit exceeded")
            encoded = raw
        else:
            raise TypeError("Market document must be bytes or a JSON object")
        if len(encoded) > limit:
            raise MarketError("Market document size limit exceeded")
        decoded = decode_document(encoded)
        if canonical(decoded) != encoded:
            raise MarketError("Market document is not canonical")
        return encoded

    @staticmethod
    def _request_envelope(document: bytes) -> tuple[str, PublicKey, dict[str, Any]]:
        data = decode_document(document)
        if set(data) != {"request_id", "reply_pubkey", "body"}:
            raise ValueError("Invalid request envelope")
        request_id = data["request_id"]
        reply_pubkey = data["reply_pubkey"]
        body = data["body"]
        if not isinstance(request_id, str) or len(request_id) != 32:
            raise ValueError("Invalid request id")
        try:
            int(request_id, 16)
        except ValueError as exc:
            raise ValueError("Invalid request id") from exc
        if request_id.lower() != request_id or not isinstance(body, dict):
            raise ValueError("Invalid request envelope")
        return request_id, PublicKey(MarketTransport._decode_base64(reply_pubkey, limit=32)), body

    @staticmethod
    def _response_envelope(document: bytes) -> tuple[str, dict[str, Any]]:
        data = decode_document(document)
        if set(data) != {"request_id", "body"}:
            raise ValueError("Invalid response envelope")
        request_id = data["request_id"]
        body = data["body"]
        if not isinstance(request_id, str) or len(request_id) != 32 or not isinstance(body, dict):
            raise ValueError("Invalid response envelope")
        try:
            int(request_id, 16)
        except ValueError as exc:
            raise ValueError("Invalid request id") from exc
        if request_id.lower() != request_id:
            raise ValueError("Invalid request id")
        return request_id, body

    def _drain_listings(self) -> None:
        while True:
            try:
                self._listings.get_nowait()
            except asyncio.QueueEmpty:
                return
