"""Regtest proof that a real private channel pays a real buyout escrow.

The unit tests prove the escrow artifacts and the ChannelEscrow adapter are
internally consistent. They cannot prove that two real LND nodes will cosign a
transaction that spends a live SIMPLE_TAPROOT channel, nor that Bitcoin Core
accepts the result. This module proves exactly that, on a disposable regtest
stack (with a third node for public routing):

* two nodes open a private SIMPLE_TAPROOT channel and confirm it,
* both endpoints freeze it under one session and report mirrored entitlements,
  mirrored funding keys and the identical funding script,
* the parent (channel input plus an ordinary P2TR wallet input, escrow change
  plus an equal ordinary output) is prepared on both endpoints,
* the timeout split of that escrow output is signed with a real two-party
  MuSig2 session and independently verified **while both endpoints are still
  only at ``PARENT_PREPARED``**, which is the protocol's point of no return: the
  buyer holds a refund before the counterparty ever contributes a partial,
* only then do the endpoints sign and finalize; the channel input carries one
  64-byte key-path signature, Bitcoin Core adds the ordinary input's signature
  and mines the exact prepared txid,
* the presigned split then spends that exact escrow output once its BIP68
  relative lock matures.

A second scenario proves the escrow's durable side: cancelling before any
partial exists releases the channel, cancelling once a partial exists is
refused, and restarting the LND node preserves the prepared parent and the
durable signing state.

The stack is a uniquely named Compose project with its own volumes. It never
touches an existing node: bitcoind makes no outbound connections, no container
publishes a port (gRPC is reached over a private loopback tunnel through
``docker exec``), credentials are copied into a private ``0700`` directory under
this repository's ``tmp`` and deleted afterwards, and the project is torn down
with ``down --volumes`` in a ``finally`` block.

These tests are marked ``docker`` and are opt-in: they never skip. If Docker or
an image is missing they fail with an explicit error.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    address_to_scriptpubkey,
    get_txid,
    parse_transaction_bytes,
    scriptpubkey_to_address,
    serialize_transaction,
    taproot_tweak_pubkey,
)
from jmcore.musig2 import nonce_agg, sign_partial

from jmswap.bitcoin_escrow import (
    MIN_CSV_DELAY,
    BuyoutEscrow,
    EscrowOutpoint,
    UnsignedKeyPathSpend,
    build_split,
    escrow_nonce,
    escrow_session,
    finalize_key_path_spend,
    verify_signed_split,
)
from jmswap.buyout_chain import BuyoutChain, CoreRpcError
from jmswap.buyout_config import BuyoutSettings
from jmswap.buyout_messages import Outpoint, Prevout
from jmswap.buyout_runtime import BuyoutRuntime
from jmswap.buyout_settlement import BuyoutSettlement
from jmswap.buyout_signing import BuyoutBuyer, BuyoutPolicy, CounterpartySigner
from jmswap.buyout_store import BuyoutStore
from jmswap.buyout_terms import BuyoutTerms, ProtocolError
from jmswap.buyout_transport import BuyoutTransport
from jmswap.lnd_escrow import (
    EscrowStage,
    EscrowStatus,
    FrozenChannel,
    LndEscrowClient,
    LndEscrowRpcError,
)
from jmswap.lnd_peer import InvoiceState, LndPeerClient

pytestmark = [pytest.mark.docker, pytest.mark.timeout(600)]

# --------------------------------------------------------------------------- #
# Disposable stack
# --------------------------------------------------------------------------- #

LND_IMAGE = os.environ.get("JM_BUYOUT_LND_IMAGE", "jm-buyout-lnd:v0.21.3-beta")
BITCOIN_IMAGE = os.environ.get("JM_BUYOUT_BITCOIN_IMAGE", "bitcoin/bitcoin:30.0")
COMPOSE_FILE = Path(__file__).resolve().parent / "regtest" / "compose.yml"
REPO_TMP = Path(__file__).resolve().parents[2] / "tmp"

NODES = ("alice", "bob")
ROUTING_NODES = (*NODES, "carol")
RPC_USER = "buyout"
RPC_PASSWORD = "buyout"
WALLET = "buyout"
MACAROON_PATH = "/lnd/data/chain/bitcoin/regtest/channelescrow.macaroon"

COMPOSE_TIMEOUT = 300
READY_TIMEOUT = 180.0
CHANNEL_TIMEOUT = 120.0
ESCROW_RPC_TIMEOUT = 30.0

DOCKER_REQUIRED = (
    "this suite is marked 'docker' and is opt-in, so it must never skip: it needs a working "
    f"Docker daemon, the {LND_IMAGE} image and the {BITCOIN_IMAGE} image"
)

SATS_PER_BTC = Decimal(100_000_000)


class RegtestError(RuntimeError):
    """A command against the disposable stack failed."""


def _decode(output: str) -> Any:
    text = output.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@dataclass(frozen=True)
class BuyoutRegtest:
    """The running stack: bitcoind plus two escrow-capable LND nodes and a router.

    ``secrets_dir`` is a private directory holding each node's TLS certificate
    and dedicated ``channelescrow`` macaroon. Nothing in this class ever prints
    those bytes.
    """

    project: str
    secrets_dir: Path
    node_ids: dict[str, str]
    compose_override_file: str | None = None

    # -- Compose ------------------------------------------------------------ #

    def compose(self, *args: str, timeout: int = COMPOSE_TIMEOUT) -> str:
        files = ["-f", str(COMPOSE_FILE)]
        if self.compose_override_file:
            files.extend(("-f", self.compose_override_file))
        done = subprocess.run(
            ["docker", "compose", "-p", self.project, *files, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if done.returncode:
            raise RegtestError(f"compose {args[0]} failed: {done.stderr.strip()}")
        return done.stdout.strip()

    def logs(self) -> str:
        """Container logs for a failure report; they carry no credentials."""
        try:
            return self.compose("logs", "--no-color", "--tail", "40", *ROUTING_NODES)
        except (RegtestError, subprocess.SubprocessError) as exc:
            return f"logs unavailable: {exc}"

    def started_at(self, node: str) -> str:
        """The container's exact start time, which proves a restart happened."""
        done = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.StartedAt}}", self.compose("ps", "-q", node)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if done.returncode:
            raise RegtestError(f"inspect {node} failed: {done.stderr.strip()}")
        return done.stdout.strip()

    # -- Bitcoin Core ------------------------------------------------------- #

    def bitcoin(self, *args: str, wallet: bool = False) -> Any:
        command = [
            "exec",
            "-T",
            "bitcoin",
            "bitcoin-cli",
            "-regtest",
            f"-rpcuser={RPC_USER}",
            f"-rpcpassword={RPC_PASSWORD}",
        ]
        if wallet:
            command.append(f"-rpcwallet={WALLET}")
        return _decode(self.compose(*command, *args))

    def mine(self, count: int) -> None:
        address = self.bitcoin("getnewaddress", "mining", "bech32m", wallet=True)
        self.bitcoin("generatetoaddress", str(count), str(address), wallet=True)

    def confirmations(self, txid: str) -> int:
        return int(self.bitcoin("getrawtransaction", txid, "true")["confirmations"])

    def mine_and_confirm(self, txid: str) -> None:
        self.mine(1)
        assert self.confirmations(txid) >= 1

    # -- LND ---------------------------------------------------------------- #

    def lncli(self, node: str, *args: str) -> Any:
        return _decode(
            self.compose("exec", "-T", node, "lncli", "--lnddir=/lnd", "--network=regtest", *args)
        )

    def container(self, node: str) -> str:
        return self.compose("ps", "-q", node)

    def credentials(self, node: str) -> tuple[bytes, bytes]:
        """The node's TLS certificate and its dedicated escrow macaroon."""
        node_dir = self.secrets_dir / node
        return (
            (node_dir / "tls.cert").read_bytes(),
            (node_dir / "channelescrow.macaroon").read_bytes(),
        )


def test_compose_override_is_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok")

    monkeypatch.setattr(subprocess, "run", fake_run)
    stack = BuyoutRegtest(project="isolated", secrets_dir=tmp_path, node_ids={})
    assert stack.compose("config") == "ok"
    assert commands[-1] == [
        "docker",
        "compose",
        "-p",
        "isolated",
        "-f",
        str(COMPOSE_FILE),
        "config",
    ]
    isolated = BuyoutRegtest(
        project="isolated",
        secrets_dir=tmp_path,
        node_ids={},
        compose_override_file=str(tmp_path / "ipam.yml"),
    )
    assert isolated.compose("config") == "ok"
    assert commands[-1] == [
        "docker",
        "compose",
        "-p",
        "isolated",
        "-f",
        str(COMPOSE_FILE),
        "-f",
        str(tmp_path / "ipam.yml"),
        "config",
    ]


@asynccontextmanager
async def grpc_endpoint(stack: BuyoutRegtest, node: str) -> AsyncIterator[str]:
    """Open a ChannelEscrow client for one node over a private loopback tunnel.

    Nothing in the stack publishes a port, and a rootless Docker daemon does not
    forward published ports to the host anyway, so each connection is relayed
    into the container by ``docker exec``. The TLS certificate still
    authenticates the node, because it carries the ``127.0.0.1`` address this
    tunnel listens on.
    """
    container = stack.container(node)
    relays: set[asyncio.Task[None]] = set()

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        process = await asyncio.create_subprocess_exec(
            *("docker", "exec", "-i", container, "nc", "127.0.0.1", "10009"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdin is not None and process.stdout is not None
        pipes = [
            asyncio.create_task(pump(reader, process.stdin)),
            asyncio.create_task(pump(process.stdout, writer)),
        ]
        try:
            await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pipe in pipes:
                pipe.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
            writer.close()
            process.stdin.close()
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            # Draining after the relay stopped: waiting on process exit alone
            # can deadlock on a paused subprocess pipe.
            await asyncio.wait_for(process.communicate(), timeout=10)

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        relay = asyncio.create_task(connect(reader, writer))
        relays.add(relay)
        relay.add_done_callback(relays.discard)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.close()
        for relay in relays:
            relay.cancel()
        await asyncio.gather(*relays, return_exceptions=True)
        await server.wait_closed()


@asynccontextmanager
async def escrow_client(stack: BuyoutRegtest, node: str) -> AsyncIterator[LndEscrowClient]:
    certificate, macaroon = stack.credentials(node)
    async with (
        grpc_endpoint(stack, node) as endpoint,
        LndEscrowClient(endpoint, certificate, macaroon, timeout=ESCROW_RPC_TIMEOUT) as client,
    ):
        yield client


@asynccontextmanager
async def peer_client(stack: BuyoutRegtest, node: str) -> AsyncIterator[LndPeerClient]:
    certificate, _ = stack.credentials(node)
    macaroon = (stack.secrets_dir / node / "admin.macaroon").read_bytes()
    async with (
        grpc_endpoint(stack, node) as endpoint,
        LndPeerClient(endpoint, certificate, macaroon) as client,
    ):
        yield client


def _require_docker() -> None:
    try:
        done = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RegtestError(f"{DOCKER_REQUIRED}: {exc}") from exc
    if done.returncode:
        raise RegtestError(f"{DOCKER_REQUIRED}: {done.stderr.strip()}")
    for image in (LND_IMAGE, BITCOIN_IMAGE):
        found = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if found.returncode:
            raise RegtestError(f"{DOCKER_REQUIRED}: {image} is not available locally")


def _collect_credentials(stack: BuyoutRegtest) -> None:
    """Copy each node's TLS certificate and escrow macaroon out, privately."""
    deadline = time.monotonic() + READY_TIMEOUT
    for node in NODES:
        node_dir = stack.secrets_dir / node
        node_dir.mkdir(mode=0o700)
        wanted = (
            ("/lnd/tls.cert", "tls.cert"),
            (MACAROON_PATH, "channelescrow.macaroon"),
            ("/lnd/data/chain/bitcoin/regtest/admin.macaroon", "admin.macaroon"),
        )
        for source, name in wanted:
            while True:
                try:
                    stack.compose("cp", f"{node}:{source}", str(node_dir / name))
                    break
                except RegtestError:
                    if time.monotonic() >= deadline:
                        raise RegtestError(f"{node} never produced {name}") from None
                    time.sleep(0.5)
            (node_dir / name).chmod(0o600)


def _wait_for_sync(stack: BuyoutRegtest, node: str) -> str:
    """Block until the node is synced to the chain and return its node id."""
    deadline = time.monotonic() + READY_TIMEOUT
    last = "no attempt completed"
    while time.monotonic() < deadline:
        try:
            info = stack.lncli(node, "getinfo")
        except (RegtestError, subprocess.SubprocessError) as exc:
            last = str(exc)
        else:
            if isinstance(info, dict) and info.get("synced_to_chain"):
                return str(info["identity_pubkey"])
            last = "not synced to chain"
        time.sleep(0.5)
    raise RegtestError(f"{node} was not ready within {READY_TIMEOUT}s: {last}")


def _isolated_stack(*, maker_stack: bool = False) -> Iterator[BuyoutRegtest]:
    """Start one throwaway stack, fund both nodes, and always tear it down."""
    _require_docker()
    REPO_TMP.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="buyout-regtest-", dir=REPO_TMP))
    directory.chmod(0o700)
    override_env = (
        "JM_BUYOUT_MAKER_COMPOSE_OVERRIDE_FILE"
        if maker_stack
        else "JM_BUYOUT_COMPOSE_OVERRIDE_FILE"
    )
    running = BuyoutRegtest(
        f"jm-buyout-{secrets.token_hex(6)}",
        directory,
        {},
        compose_override_file=os.environ.get(override_env),
    )
    try:
        running.compose("up", "-d", "--wait", "--wait-timeout", "180")
        running.bitcoin("createwallet", WALLET)
        running.mine(101)
        _collect_credentials(running)
        for node in ROUTING_NODES:
            _wait_for_sync(running, node)
            address = running.lncli(node, "newaddress", "p2tr")["address"]
            # Fund the entire module, including separate settlement routes.
            running.bitcoin("sendtoaddress", str(address), "0.20", wallet=True)
        running.mine(6)
        for node in ROUTING_NODES:
            running.node_ids[node] = _wait_for_sync(running, node)
        running.lncli("alice", "connect", f"{running.node_ids['bob']}@bob:9735")
        yield running
    except BaseException:
        print(running.logs())
        raise
    finally:
        try:
            running.compose("down", "--volumes", "--remove-orphans")
        finally:
            shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="module")
def stack() -> Iterator[BuyoutRegtest]:
    yield from _isolated_stack()


@pytest.fixture
def maker_stack() -> Iterator[BuyoutRegtest]:
    # Earlier crash-recovery tests leave quiescent channels whose timers can
    # disconnect the shared peer during this scenario. Each maker/reorg case
    # needs its own nodes to assert the cooperative path deterministically.
    yield from _isolated_stack(maker_stack=True)


# --------------------------------------------------------------------------- #
# Channel, parent and escrow helpers
# --------------------------------------------------------------------------- #

CHANNEL_CAPACITY = 1_000_000
CHANNEL_PUSH = 400_000
WALLET_INPUT_BTC = "0.03"
PARENT_FEE = 2_000
SPLIT_FEE = 1_000
BUYOUT_FEE = 500


def _open_channel_rpc(stack: BuyoutRegtest, opener: str, peer: str, *args: str) -> Any:
    """Open a channel, reconnecting a peer that an idle channel timer disconnected.

    Earlier tests on the shared stack leave quiescent channels whose timers can
    drop the peer link; a funding negotiation that fails on a disconnected peer
    broadcasts nothing, so reconnecting and retrying is safe.
    """
    node_id = stack.node_ids[peer]
    for attempt in range(3):
        peers = (stack.lncli(opener, "listpeers") or {}).get("peers", [])
        if not any(entry.get("pub_key") == node_id for entry in peers):
            try:
                stack.lncli(opener, "connect", f"{node_id}@{peer}:9735")
            except RegtestError as exc:
                if "already connected" not in str(exc):
                    raise
        try:
            return stack.lncli(opener, "openchannel", "--node_key", node_id, *args)
        except RegtestError as exc:
            transient = "disconnected" in str(exc) or "not connected" in str(exc)
            if not transient or attempt == 2:
                raise
            time.sleep(1.0)
    raise AssertionError("unreachable")


def _open_private_taproot_channel(
    stack: BuyoutRegtest, opener: str = "alice", peer: str = "bob", *, push_sat: int = CHANNEL_PUSH
) -> Outpoint:
    """Open a private channel, including a route-less first hop when settlement must fail without hints."""
    opened = _open_channel_rpc(
        stack,
        opener,
        peer,
        "--local_amt",
        str(CHANNEL_CAPACITY),
        "--push_amt",
        str(push_sat),
        "--private",
        "--channel_type",
        "taproot",
        "--sat_per_vbyte",
        "2",
    )
    funding_txid = str(opened["funding_txid"])
    stack.mine(6)
    deadline = time.monotonic() + CHANNEL_TIMEOUT
    while True:
        active = {
            node: [
                channel
                for channel in (stack.lncli(node, "listchannels", "--active_only") or {}).get(
                    "channels", []
                )
                if str(channel["channel_point"]).startswith(f"{funding_txid}:")
            ]
            for node in (opener, peer)
        }
        if all(active[node] for node in active):
            channel = active[opener][0]
            assert channel["private"] is True, "the buyout channel must be unannounced"
            # lncli renders the SIMPLE_TAPROOT commitment type as "TAPROOT".
            assert str(channel["commitment_type"]).endswith("TAPROOT")
            point = str(channel["channel_point"]).split(":")
            return Outpoint(txid=point[0], vout=int(point[1]))
        if time.monotonic() >= deadline:
            raise RegtestError(f"channel {funding_txid} did not become active on both endpoints")
        stack.mine(1)
        time.sleep(1.0)


def _open_announced_channel(
    stack: BuyoutRegtest, opener: str, peer: str, *, push_sat: int
) -> Outpoint:
    """Open a public route channel so settlement can traverse the graph after a buyout spends its channel."""
    opened = _open_channel_rpc(
        stack,
        opener,
        peer,
        "--local_amt",
        str(CHANNEL_CAPACITY),
        "--push_amt",
        str(push_sat),
        "--sat_per_vbyte",
        "2",
    )
    funding_txid = str(opened["funding_txid"])
    stack.mine(6)
    deadline = time.monotonic() + CHANNEL_TIMEOUT
    while True:
        active = {
            node: [
                channel
                for channel in (stack.lncli(node, "listchannels", "--active_only") or {}).get(
                    "channels", []
                )
                if str(channel["channel_point"]).startswith(f"{funding_txid}:")
            ]
            for node in (opener, peer)
        }
        if all(active[node] for node in active):
            channel = active[opener][0]
            assert channel["private"] is False, "the routing channel must be announced"
            point = str(channel["channel_point"]).split(":")
            return Outpoint(txid=point[0], vout=int(point[1]))
        if time.monotonic() >= deadline:
            raise RegtestError(
                f"public channel {funding_txid} did not become active on both endpoints"
            )
        stack.mine(1)
        time.sleep(1.0)


def _wait_for_announced_channels(stack: BuyoutRegtest, links: set[frozenset[str]]) -> None:
    """Wait until every node can route over the announced channels without an invoice or payer hint."""
    deadline = time.monotonic() + CHANNEL_TIMEOUT
    while True:
        known = {
            node: {
                frozenset((str(edge["node1_pub"]), str(edge["node2_pub"])))
                for edge in (
                    stack.lncli(node, "describegraph", "--include_unannounced=false") or {}
                ).get("edges", [])
            }
            for node in ROUTING_NODES
        }
        if all(links <= ids for ids in known.values()):
            return
        if time.monotonic() >= deadline:
            raise RegtestError(f"public links {links} did not propagate into every routing graph")
        time.sleep(1.0)


def _p2tr_wallet_input(
    stack: BuyoutRegtest, amount_btc: str = WALLET_INPUT_BTC
) -> tuple[TxInput, Prevout]:
    """Confirm one ordinary P2TR wallet output and return it as a parent input."""
    address = str(stack.bitcoin("getnewaddress", "parent-input", "bech32m", wallet=True))
    txid = str(stack.bitcoin("sendtoaddress", address, amount_btc, wallet=True))
    stack.mine(1)
    funding = stack.bitcoin("getrawtransaction", txid, "true")
    script = address_to_scriptpubkey(address).hex()
    for output in funding["vout"]:
        if output["scriptPubKey"]["hex"] == script:
            value = int(Decimal(str(output["value"])) * SATS_PER_BTC)
            # Later sendtoaddress calls must not consume a prepared test input.
            assert stack.bitcoin(
                "lockunspent",
                "false",
                json.dumps([{"txid": txid, "vout": int(output["n"])}]),
                wallet=True,
            )
            return (
                TxInput.from_hex(txid, int(output["n"])),
                Prevout(value=value, script_pubkey=script),
            )
    raise RegtestError(f"wallet funding transaction {txid} has no output paying {address}")


def _p2tr_script(secret: bytes) -> bytes:
    _, output_key = taproot_tweak_pubkey(bytes(CKey(secret).xonly_pub))
    return bytes([0x51, 0x20]) + output_key


@dataclass(frozen=True)
class EscrowKeys:
    """Fresh per-attempt escrow material for the buyer ``B`` and ``C``."""

    buyer_secret: bytes
    counterparty_secret: bytes
    escrow: BuyoutEscrow
    buyer_split_script: bytes
    counterparty_split_script: bytes

    @classmethod
    def fresh(cls) -> EscrowKeys:
        buyer, counterparty, claim = (secrets.token_bytes(32) for _ in range(3))
        return cls(
            buyer_secret=buyer,
            counterparty_secret=counterparty,
            escrow=BuyoutEscrow(
                buyer_escrow_pubkey=bytes(CKey(buyer).pub),
                counterparty_escrow_pubkey=bytes(CKey(counterparty).pub),
                buyer_claim_pubkey=bytes(CKey(claim).pub),
                payment_hash=secrets.token_bytes(32),
            ),
            buyer_split_script=_p2tr_script(secrets.token_bytes(32)),
            counterparty_split_script=_p2tr_script(secrets.token_bytes(32)),
        )

    def sign_key_path(self, spend: UnsignedKeyPathSpend) -> bytes:
        """Run a real two-party MuSig2 session over the escrow key-path sighash."""
        buyer_secnonce, buyer_pubnonce = escrow_nonce(
            self.escrow.buyer_escrow_pubkey,
            os.urandom(32),
            privkey=self.buyer_secret,
            sighash=spend.sighash,
        )
        cp_secnonce, cp_pubnonce = escrow_nonce(
            self.escrow.counterparty_escrow_pubkey,
            os.urandom(32),
            privkey=self.counterparty_secret,
            sighash=spend.sighash,
        )
        session = escrow_session(
            self.escrow, nonce_agg([buyer_pubnonce, cp_pubnonce]), spend.sighash
        )
        return finalize_key_path_spend(
            self.escrow,
            spend,
            buyer_pubnonce,
            cp_pubnonce,
            sign_partial(buyer_secnonce, self.buyer_secret, session),
            sign_partial(cp_secnonce, self.counterparty_secret, session),
        )


@dataclass(frozen=True)
class Parent:
    """The unsigned parent, with everything the escrow calls need about it."""

    raw: bytes
    txid: str
    prevouts: tuple[Prevout, ...]
    channel_input_index: int
    escrow_outpoint: EscrowOutpoint


def _build_parent(
    stack: BuyoutRegtest,
    point: Outpoint,
    channel_prevout: Prevout,
    keys: EscrowKeys,
) -> Parent:
    """Build the parent: channel input plus one ordinary P2TR wallet input.

    Its outputs are the buyer's escrow change and one ordinary output of the
    same size, which is the shape a CoinJoin equal-output round produces.
    """
    wallet_input, wallet_prevout = _p2tr_wallet_input(stack)
    total_in = channel_prevout.value + wallet_prevout.value
    equal_value = (total_in - PARENT_FEE) // 2
    ordinary_script = address_to_scriptpubkey(
        str(stack.bitcoin("getnewaddress", "equal-output", "bech32m", wallet=True))
    )
    inputs = [TxInput.from_hex(point.txid, point.vout), wallet_input]
    outputs = [
        TxOutput(value=equal_value, script=keys.escrow.output_script()),
        TxOutput(value=equal_value, script=ordinary_script),
    ]
    raw = serialize_transaction(2, inputs, outputs, 0)
    txid = get_txid(raw.hex())
    return Parent(
        raw=raw,
        txid=txid,
        prevouts=(channel_prevout, wallet_prevout),
        channel_input_index=0,
        escrow_outpoint=EscrowOutpoint(
            txid=txid, vout=0, value=equal_value, scriptpubkey=keys.escrow.output_script()
        ),
    )


def _assert_prepared_only(status: EscrowStatus, parent_txid: str) -> None:
    """The endpoint has accepted the parent and contributed nothing yet."""
    assert status.stage is EscrowStage.PARENT_PREPARED
    assert status.parent_txid == parent_txid
    assert status.finalized is False
    assert status.durable_local_nonce is None
    assert status.durable_remote_nonce is None
    assert status.durable_local_partial is None
    assert status.finalized_parent is None


def _channel_witness(signed_tx: bytes) -> list[bytes]:
    return list(parse_transaction_bytes(signed_tx).witnesses[0])


async def _freeze_both(
    clients: Sequence[LndEscrowClient], point: Outpoint, session: bytes
) -> tuple[FrozenChannel, FrozenChannel]:
    """Freeze the same channel from both endpoints under one session."""
    alice, bob = await asyncio.gather(*(client.freeze(point, session) for client in clients))
    return alice, bob


# --------------------------------------------------------------------------- #
# The buyout parent, end to end
# --------------------------------------------------------------------------- #


async def test_channel_input_pays_the_escrow_with_the_split_held_first(
    stack: BuyoutRegtest,
) -> None:
    """A live private channel funds an escrow the buyer can already refund."""
    try:
        point = _open_private_taproot_channel(stack)
        keys = EscrowKeys.fresh()
        session = secrets.token_bytes(32)
        async with AsyncExitStack() as connections:
            alice_client = await connections.enter_async_context(escrow_client(stack, "alice"))
            bob_client = await connections.enter_async_context(escrow_client(stack, "bob"))
            clients = (alice_client, bob_client)
            alice, bob = await _freeze_both(clients, point, session)

            # The two endpoints must describe one channel, mirrored.
            assert alice.point == bob.point == point
            assert alice.capacity_sat == bob.capacity_sat == CHANNEL_CAPACITY
            assert alice.local_claim_sat == bob.remote_claim_sat
            assert alice.remote_claim_sat == bob.local_claim_sat
            assert alice.local_claim_sat + alice.remote_claim_sat == alice.capacity_sat
            assert alice.funding_script == bob.funding_script
            assert alice.local_funding_pubkey == bob.remote_funding_pubkey
            assert alice.remote_funding_pubkey == bob.local_funding_pubkey
            assert alice.peer_pubkey.hex() == stack.node_ids["bob"]
            assert bob.peer_pubkey.hex() == stack.node_ids["alice"]
            assert bob.local_claim_sat >= CHANNEL_PUSH

            channel_prevout = Prevout(
                value=alice.capacity_sat, script_pubkey=alice.funding_script.hex()
            )
            parent = _build_parent(stack, point, channel_prevout, keys)
            prepared = await asyncio.gather(
                *(
                    client.prepare(
                        point,
                        session,
                        parent.raw,
                        list(parent.prevouts),
                        parent.channel_input_index,
                    )
                    for client in clients
                )
            )
            assert prepared == [parent.txid, parent.txid]

            # The buyer's refund is built and verified first. Both endpoints are
            # still only at PARENT_PREPARED here, so the counterparty has not yet
            # contributed anything that could spend the channel.
            counterparty_value = bob.local_claim_sat + BUYOUT_FEE
            split = build_split(
                keys.escrow,
                parent.escrow_outpoint,
                keys.buyer_split_script,
                keys.counterparty_split_script,
                counterparty_value,
                SPLIT_FEE,
                MIN_CSV_DELAY,
            )
            signed_split = keys.sign_key_path(split)
            assert verify_signed_split(
                keys.escrow,
                parent.escrow_outpoint,
                keys.buyer_split_script,
                keys.counterparty_split_script,
                counterparty_value,
                SPLIT_FEE,
                MIN_CSV_DELAY,
                signed_split,
            )
            for status in await asyncio.gather(
                *(client.status(point, session) for client in clients)
            ):
                _assert_prepared_only(status, parent.txid)

            # Only now does the counterparty sign the channel input.
            attempts = await asyncio.gather(*(client.begin(point, session) for client in clients))
            partials = await asyncio.gather(
                alice_client.sign(point, session, attempts[0], attempts[1].public_nonce),
                bob_client.sign(point, session, attempts[1], attempts[0].public_nonce),
            )
            finalized = await asyncio.gather(
                alice_client.finalize(point, session, attempts[0], partials[1]),
                bob_client.finalize(point, session, attempts[1], partials[0]),
            )
            assert finalized[0] == finalized[1]
            signed_parent = finalized[0]
            assert get_txid(signed_parent.hex()) == parent.txid

            # A key-path spend of the funding output: one signature, no script.
            witness = _channel_witness(signed_parent)
            assert len(witness) == 1
            assert len(witness[0]) == 64

            # Bitcoin Core adds the ordinary input's signature and nothing else.
            completed = stack.bitcoin("signrawtransactionwithwallet", signed_parent.hex())
            assert completed["complete"], completed.get("errors")
            final = bytes.fromhex(str(completed["hex"]))
            assert _channel_witness(final) == witness
            acceptance = stack.bitcoin("testmempoolaccept", f'["{completed["hex"]}"]')[0]
            assert acceptance["allowed"], acceptance
            assert stack.bitcoin("sendrawtransaction", str(completed["hex"])) == parent.txid
            stack.mine_and_confirm(parent.txid)

            # The channel funding output is gone and the escrow output exists.
            assert stack.bitcoin("gettxout", point.txid, str(point.vout)) == ""
            escrow_output = stack.bitcoin("gettxout", parent.txid, "0")
            assert escrow_output["scriptPubKey"]["hex"] == keys.escrow.output_script().hex()
            assert int(Decimal(str(escrow_output["value"])) * SATS_PER_BTC) == (
                parent.escrow_outpoint.value
            )

            for status in await asyncio.gather(
                *(client.status(point, session) for client in clients)
            ):
                assert status.stage is EscrowStage.FINALIZED
                assert status.finalized is True
                assert status.parent_txid == parent.txid
                assert status.finalized_parent == signed_parent

        # The refund held before the counterparty signed spends that exact
        # escrow output, once its relative lock matures.
        split_input = parse_transaction_bytes(signed_split).inputs[0]
        assert (split_input.txid, split_input.vout) == (parent.txid, 0)
        stack.mine(MIN_CSV_DELAY - stack.confirmations(parent.txid))
        split_txid = str(stack.bitcoin("sendrawtransaction", signed_split.hex()))
        stack.mine_and_confirm(split_txid)
        assert stack.bitcoin("gettxout", parent.txid, "0") == ""
    except BaseException:
        print(stack.logs())
        raise


# --------------------------------------------------------------------------- #
# Cancellation and restart
# --------------------------------------------------------------------------- #


async def test_cancel_is_refused_once_signed_and_survives_a_restart(
    stack: BuyoutRegtest,
) -> None:
    """An abandoned session releases the channel; a signed one is durable."""
    try:
        point = _open_private_taproot_channel(stack)
        async with AsyncExitStack() as connections:
            alice_client = await connections.enter_async_context(escrow_client(stack, "alice"))
            bob_client = await connections.enter_async_context(escrow_client(stack, "bob"))
            clients = (alice_client, bob_client)

            # Abandoned before any partial exists: both links resume and the
            # session is gone, so the channel can be frozen again.
            abandoned = secrets.token_bytes(32)
            alice, _ = await _freeze_both(clients, point, abandoned)
            abandoned_parent = _build_parent(
                stack,
                point,
                Prevout(value=alice.capacity_sat, script_pubkey=alice.funding_script.hex()),
                EscrowKeys.fresh(),
            )
            await asyncio.gather(
                *(
                    client.prepare(
                        point,
                        abandoned,
                        abandoned_parent.raw,
                        list(abandoned_parent.prevouts),
                        abandoned_parent.channel_input_index,
                    )
                    for client in clients
                )
            )
            resumed = await asyncio.gather(*(client.cancel(point, abandoned) for client in clients))
            assert resumed == [True, True]
            for client in clients:
                with pytest.raises(LndEscrowRpcError):
                    await client.status(point, abandoned)

            # A second session reaches a partial signature on both endpoints.
            session = secrets.token_bytes(32)
            alice, _ = await _freeze_both(clients, point, session)
            parent = _build_parent(
                stack,
                point,
                Prevout(value=alice.capacity_sat, script_pubkey=alice.funding_script.hex()),
                EscrowKeys.fresh(),
            )
            await asyncio.gather(
                *(
                    client.prepare(
                        point,
                        session,
                        parent.raw,
                        list(parent.prevouts),
                        parent.channel_input_index,
                    )
                    for client in clients
                )
            )
            attempts = await asyncio.gather(*(client.begin(point, session) for client in clients))
            await asyncio.gather(
                alice_client.sign(point, session, attempts[0], attempts[1].public_nonce),
                bob_client.sign(point, session, attempts[1], attempts[0].public_nonce),
            )

            # Cancelling is refused from here on, and the record is untouched.
            for client in clients:
                with pytest.raises(LndEscrowRpcError):
                    await client.cancel(point, session)
            before = await alice_client.status(point, session)
            assert before.stage is EscrowStage.PARENT_SIGNED
            assert before.parent_txid == parent.txid

        started_at = stack.started_at("alice")
        stack.compose("restart", "alice")
        assert stack.started_at("alice") != started_at
        _wait_for_sync(stack, "alice")

        async with escrow_client(stack, "alice") as restarted:
            after = await restarted.status(point, session)
        assert after.stage is EscrowStage.PARENT_SIGNED
        assert after.parent_txid == parent.txid
        assert after.finalized is False
        assert after.durable_local_nonce == before.durable_local_nonce
        assert after.durable_remote_nonce == before.durable_remote_nonce
        assert after.durable_local_partial == before.durable_local_partial
        assert after.durable_local_partial is not None
        # Nothing was broadcast, so the channel is still funded.
        assert stack.bitcoin("gettxout", point.txid, str(point.vout)) != ""

        # Signing forbids cancellation, but must not remove unilateral recovery.
        async with peer_client(stack, "alice") as peer:
            await peer.close_channel(point, force=True)
        spenders = stack.bitcoin(
            "gettxspendingprevout", json.dumps([{"txid": point.txid, "vout": point.vout}])
        )
        closing_txid = spenders[0]["spendingtxid"]
        assert closing_txid != parent.txid
        stack.mine_and_confirm(closing_txid)
        assert stack.bitcoin("gettxout", point.txid, str(point.vout)) == ""
    except BaseException:
        print(stack.logs())
        raise


async def test_private_signing_runtime_and_durable_replay(stack: BuyoutRegtest) -> None:
    point = _open_private_taproot_channel(stack)
    ordinary, ordinary_prevout = _p2tr_wallet_input(stack)
    buyer_dir = stack.secrets_dir / "buyer-journal"
    counterparty_dir = stack.secrets_dir / "counterparty-journal"

    async def height() -> int:
        return int(stack.bitcoin("getblockcount"))

    async with AsyncExitStack() as resources:
        alice = await resources.enter_async_context(escrow_client(stack, "alice"))
        bob = await resources.enter_async_context(escrow_client(stack, "bob"))
        alice_peer = await resources.enter_async_context(peer_client(stack, "alice"))
        bob_peer = await resources.enter_async_context(peer_client(stack, "bob"))
        buyer_store = resources.enter_context(BuyoutStore(buyer_dir / "sessions.sqlite"))
        cp_store = resources.enter_context(BuyoutStore(counterparty_dir / "sessions.sqlite"))
        payout = _p2tr_script(secrets.token_bytes(32)).hex()
        payouts = (payout, _p2tr_script(secrets.token_bytes(32)).hex())
        policy = BuyoutPolicy()
        counterparty = CounterpartySigner(cp_store, bob, bob_peer, height, policy, payouts)
        parent_received, release_parent = asyncio.Event(), asyncio.Event()

        async def handle(peer: str, message: Any) -> Any:
            if message.type == "buyout_parent":
                parent_received.set()
                await release_parent.wait()
            return await counterparty.handle(peer, message)

        await resources.enter_async_context(
            BuyoutTransport(bob_peer, frozenset({stack.node_ids["alice"]}), handle, timeout=2)
        )
        transport = await resources.enter_async_context(
            BuyoutTransport(alice_peer, frozenset({stack.node_ids["bob"]}), timeout=2)
        )
        request = transport.request

        buyer = BuyoutBuyer(buyer_store, alice, alice_peer, request, height, policy)
        session_id = await buyer.prepare(stack.node_ids["bob"], [point], payouts)
        terms = buyer.terms(session_id)
        assert terms.claim_sat == CHANNEL_PUSH
        change = CHANNEL_CAPACITY + ordinary_prevout.value - 500_000 - PARENT_FEE
        inputs = [TxInput.from_hex(point.txid, point.vout), ordinary]
        outputs = [
            TxOutput(value=500_000, script=bytes.fromhex(payout)),
            TxOutput(value=change, script=terms.escrow.output_script()),
        ]
        raw = serialize_transaction(2, inputs, outputs, 0)
        prevouts = [
            Prevout(value=CHANNEL_CAPACITY, script_pubkey=terms.channels[0].funding_script.hex()),
            ordinary_prevout,
        ]
        signing = asyncio.create_task(buyer.sign_parent(session_id, raw, prevouts, [0], 1))
        await parent_received.wait()
        replacement = serialize_transaction(
            2,
            inputs,
            [outputs[0], TxOutput(value=change - 1, script=terms.escrow.output_script())],
            0,
        )
        competing = asyncio.create_task(
            buyer.sign_parent(session_id, replacement, prevouts, [0], 1)
        )
        await asyncio.sleep(0)
        assert not competing.done()
        assert buyer_store.get(session_id).data["raw_parent"] == raw.hex()
        release_parent.set()
        signatures = await signing
        with pytest.raises(ProtocolError, match="cannot resume with this parent"):
            await competing
        assert len(signatures[(point.txid, point.vout)]) == 64
        assert buyer_store.get(session_id).parent_signing_started
        assert (
            cp_store.get(session_id).data["split_tx"]
            == buyer_store.get(session_id).data["split_tx"]
        )
        # Drop process-local escrow nonces and replay only durable signatures.
        counterparty = CounterpartySigner(cp_store, bob, bob_peer, height, policy, payouts)
        buyer = BuyoutBuyer(buyer_store, alice, alice_peer, request, height, policy)

        async def disconnected(peer: str, message: Any) -> Any:
            raise AssertionError("durable replay must not depend on the counterparty")

        buyer.request = disconnected
        assert await buyer.sign_parent(session_id, raw, prevouts, [0], 1) == signatures
        partial_parent = serialize_transaction(
            2, inputs, outputs, 0, witnesses=[[signatures[(point.txid, point.vout)]], []]
        )
        signed = stack.bitcoin("signrawtransactionwithwallet", partial_parent.hex(), wallet=True)
        assert signed["complete"]
        txid = stack.bitcoin("sendrawtransaction", signed["hex"])
        stack.mine_and_confirm(txid)
        assert stack.bitcoin("gettxout", point.txid, str(point.vout)) == ""


async def test_runtime_recovers_nonce_loss_and_interrupted_cancellation(
    stack: BuyoutRegtest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = _open_private_taproot_channel(stack)
    ordinary, ordinary_prevout = _p2tr_wallet_input(stack)

    async def height() -> int:
        return int(stack.bitcoin("getblockcount"))

    async with AsyncExitStack() as resources:
        alice = await resources.enter_async_context(escrow_client(stack, "alice"))
        bob = await resources.enter_async_context(escrow_client(stack, "bob"))
        alice_peer = await resources.enter_async_context(peer_client(stack, "alice"))
        bob_peer = await resources.enter_async_context(peer_client(stack, "bob"))
        buyer_store = resources.enter_context(
            BuyoutStore(stack.secrets_dir / "recovery-b" / "sessions.sqlite")
        )
        cp_store = resources.enter_context(
            BuyoutStore(stack.secrets_dir / "recovery-c" / "sessions.sqlite")
        )
        payout = _p2tr_script(secrets.token_bytes(32)).hex()
        payouts = (payout, _p2tr_script(secrets.token_bytes(32)).hex())
        policy = BuyoutPolicy()
        counterparty = CounterpartySigner(cp_store, bob, bob_peer, height, policy, payouts)

        async def request(peer: str, message: Any) -> Any:
            response = await counterparty.handle(stack.node_ids["alice"], message)
            if message.type == "buyout_parent":
                raise TimeoutError("lost nonce reply")
            return response

        buyer = BuyoutBuyer(buyer_store, alice, alice_peer, request, height, policy)
        sid = await buyer.prepare(stack.node_ids["bob"], [point], payouts)
        terms = buyer.terms(sid)
        inputs = [TxInput.from_hex(point.txid, point.vout), ordinary]
        outputs = [
            TxOutput(value=500_000, script=bytes.fromhex(payout)),
            TxOutput(
                value=CHANNEL_CAPACITY + ordinary_prevout.value - 500_000 - PARENT_FEE,
                script=terms.escrow.output_script(),
            ),
        ]
        parent = serialize_transaction(2, inputs, outputs, 0)
        prevouts = [
            Prevout(value=CHANNEL_CAPACITY, script_pubkey=terms.channels[0].funding_script.hex()),
            ordinary_prevout,
        ]
        with pytest.raises(TimeoutError, match="nonce reply"):
            await buyer.sign_parent(sid, parent, prevouts, [0], 1)
        assert buyer_store.get(sid).state == "NONCES"
        assert cp_store.get(sid).state == "NONCES"
        assert not buyer_store.get(sid).parent_signing_started
        assert not cp_store.get(sid).parent_signing_started
        counterparty = CounterpartySigner(cp_store, bob, bob_peer, height, policy, payouts)
        buyer = BuyoutBuyer(buyer_store, alice, alice_peer, request, height, policy)
        cancel = bob.cancel

        async def interrupted_cancel(point: Outpoint, session_id: bytes) -> bool:
            await cancel(point, session_id)
            raise TimeoutError("cancellation completed but response was lost")

        monkeypatch.setattr(bob, "cancel", interrupted_cancel)
        with pytest.raises(TimeoutError, match="response was lost"):
            await buyer.cancel(sid)
        assert buyer_store.get(sid).state == "CANCELING"
        assert cp_store.get(sid).state == "CANCELING"
        monkeypatch.setattr(bob, "cancel", cancel)
        counterparty = CounterpartySigner(cp_store, bob, bob_peer, height, policy, payouts)
        buyer = BuyoutBuyer(buyer_store, alice, alice_peer, request, height, policy)
        await buyer.cancel(sid)
        assert buyer_store.get(sid).state == "CANCELED"
        assert cp_store.get(sid).state == "CANCELED"
        assert await cancel(point, bytes.fromhex(sid))
        assert stack.bitcoin("gettxout", point.txid, str(point.vout)) != ""


@pytest.mark.parametrize("settlement_path", ["cooperative", "claim", "split"])
async def test_taker_builds_and_signs_channel_funded_coinjoin(
    stack: BuyoutRegtest,
    settlement_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from jmcore.bitcoin import scriptpubkey_to_address
    from jmcore.models import NetworkType, Offer, OfferType
    from jmwallet.wallet.models import UTXOInfo
    from taker.buyout import ChannelBuyout
    from taker.coinjoin_session import CoinJoinSession
    from taker.config import TakerConfig
    from taker.models import MakerSession
    from taker.tx_builder import CoinJoinTxBuilder

    from jmswap.buyout_chain import BuyoutChain, CoreRpcError
    from jmswap.buyout_settlement import BuyoutSettlement

    point = _open_private_taproot_channel(stack)
    if settlement_path != "split":
        # The purchased channel will be gone. Settlement needs another route.
        _open_private_taproot_channel(stack)
    ordinary, ordinary_prevout = _p2tr_wallet_input(stack)
    maker_input, maker_prevout = _p2tr_wallet_input(stack, "0.04")

    async def height() -> int:
        return int(stack.bitcoin("getblockcount"))

    def address() -> str:
        return scriptpubkey_to_address(_p2tr_script(secrets.token_bytes(32)), "regtest")

    async with AsyncExitStack() as resources:
        alice = await resources.enter_async_context(escrow_client(stack, "alice"))
        bob = await resources.enter_async_context(escrow_client(stack, "bob"))
        alice_peer = await resources.enter_async_context(peer_client(stack, "alice"))
        bob_peer = await resources.enter_async_context(peer_client(stack, "bob"))
        buyer_store = resources.enter_context(
            BuyoutStore(stack.secrets_dir / "taker-b" / "sessions.sqlite")
        )
        cp_store = resources.enter_context(
            BuyoutStore(stack.secrets_dir / "taker-c" / "sessions.sqlite")
        )
        policy = BuyoutPolicy(settlement_enabled=True)
        payout = _p2tr_script(secrets.token_bytes(32)).hex()
        payouts = (payout, _p2tr_script(secrets.token_bytes(32)).hex())
        counterparty = CounterpartySigner(cp_store, bob, bob_peer, height, policy, payouts)

        async def request(peer: str, message: Any) -> Any:
            return await counterparty.handle(stack.node_ids["alice"], message)

        buyer = BuyoutBuyer(buyer_store, alice, alice_peer, request, height, policy)
        sid = await buyer.prepare(stack.node_ids["bob"], [point], payouts)
        buyout = ChannelBuyout(buyer, sid)
        buyout.begin_round()
        with pytest.raises(ProtocolError, match="already reserved"):
            buyout.begin_round()
        session = CoinJoinSession()
        owner = MagicMock()
        owner.config = TakerConfig(
            network=NetworkType.REGTEST,
            preferred_offer_type=OfferType.TR0_ABSOLUTE,
            address_type="p2tr",
            mnemonic="abandon " * 11 + "about",
            data_dir=stack.secrets_dir / "taker-data",
        )
        owner.backend.get_block_height = AsyncMock(side_effect=height)
        owner.wallet.renew_coinjoin_inputs.return_value = True
        session.attach(owner)
        session.buyout = buyout
        session.cj_amount = 3_200_000
        session._fee_rate = 2.0
        session._minimum_fee_rate_sat_vb = 1.0
        wallet_utxo = UTXOInfo(
            txid=ordinary.txid,
            vout=ordinary.vout,
            value=ordinary_prevout.value,
            address=scriptpubkey_to_address(
                bytes.fromhex(ordinary_prevout.script_pubkey), "regtest"
            ),
            scriptpubkey=ordinary_prevout.script_pubkey,
            confirmations=10,
            path="m/86'/1'/0'/0/0",
            mixdepth=0,
        )
        session.preselected_utxos = [wallet_utxo]
        session.reserved_inputs = {(wallet_utxo.txid, wallet_utxo.vout)}
        maker = MakerSession(
            nick="maker",
            offer=Offer(
                counterparty="maker",
                oid=0,
                ordertype=OfferType.TR0_ABSOLUTE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=0,
                cjfee=500,
            ),
        )
        maker.utxos = [
            {
                "txid": maker_input.txid,
                "vout": maker_input.vout,
                "value": maker_prevout.value,
                "scriptpubkey": maker_prevout.script_pubkey,
            }
        ]
        maker.cj_address, maker.change_address = address(), address()
        session.maker_sessions = {"maker": maker}
        assert await session._phase_build_tx(address(), 0)
        assert session.selected_utxos == [wallet_utxo]
        assert session.wallet_change_address == ""
        owner.wallet.get_new_internal_address.assert_not_called()
        owner.wallet.select_utxos.assert_not_called()
        core_signed = stack.bitcoin(
            "signrawtransactionwithwallet", session.unsigned_tx.hex(), wallet=True
        )
        signed = parse_transaction_bytes(bytes.fromhex(core_signed["hex"]))
        assert len(signed.witnesses) == len(signed.inputs), core_signed.get("errors")

        def sign_wallet(tx: Any, index: int, utxo: UTXOInfo, **kwargs: Any) -> Any:
            assert utxo == wallet_utxo
            assert len(kwargs["prevout_values"]) == 3
            assert (signed.inputs[index].txid, signed.inputs[index].vout) == (utxo.txid, utxo.vout)
            witness = signed.witnesses[index]
            return SimpleNamespace(signature=witness[0], pubkey=b"", witness=witness)

        owner.wallet.sign_input.side_effect = sign_wallet
        taker_signatures = await session._sign_our_inputs()
        assert len(taker_signatures) == 2
        owner.wallet.sign_input.assert_called_once()
        owner.wallet.get_private_key.assert_not_called()
        maker_index = next(
            n
            for n, item in enumerate(signed.inputs)
            if (item.txid, item.vout) == (maker_input.txid, maker_input.vout)
        )
        final = CoinJoinTxBuilder("regtest").add_signatures(
            session.unsigned_tx,
            {
                "taker": taker_signatures,
                "maker": [
                    {
                        "txid": maker_input.txid,
                        "vout": maker_input.vout,
                        "witness": [w.hex() for w in signed.witnesses[maker_index]],
                    }
                ],
            },
            session.tx_metadata,
        )
        parsed = parse_transaction_bytes(final)
        assert sum(output.value == session.cj_amount for output in parsed.outputs) == 2
        assert all(len(witness) == 1 and len(witness[0]) == 64 for witness in parsed.witnesses)
        txid = stack.bitcoin("sendrawtransaction", final.hex())
        stack.mine_and_confirm(txid)
        assert buyer_store.get(sid).parent_signing_started

        async def core_rpc(method: str, *args: Any) -> Any:
            try:
                result = stack.bitcoin(
                    method, *(arg if isinstance(arg, str) else json.dumps(arg) for arg in args)
                )
            except RegtestError as exc:
                code = re.search(r"error code:\s*(-?\d+)", str(exc))
                if code:
                    raise CoreRpcError(int(code[1])) from None
                raise
            return None if result == "" else result

        chain = BuyoutChain("http://unused.invalid", "unused", "unused")
        monkeypatch.setattr(chain, "_rpc", core_rpc)
        await resources.enter_async_context(chain)
        cp_settlement = BuyoutSettlement(cp_store, bob_peer, chain)

        async def settlement_request(peer: str, message: Any) -> Any:
            if settlement_path == "claim" and message.type == "buyout_sweep":
                raise TimeoutError("counterparty disconnected after payment")
            return await cp_settlement.handle(stack.node_ids["alice"], message)

        b_settlement = BuyoutSettlement(buyer_store, alice_peer, chain, request=settlement_request)
        if settlement_path == "split":
            stack.mine(policy.csv_delay - 1)
            assert await cp_settlement.poll(sid) == "SPEND_BROADCAST"
            spend = cp_store.get(sid).data["spends"][-1]
        else:
            stack.mine(policy.buyer_settlement_depth)
            _wait_for_sync(stack, "alice")
            _wait_for_sync(stack, "bob")
            assert await b_settlement.poll(sid) == "SETTLED"
            assert await b_settlement.poll(sid) == "SPEND_BROADCAST"
            spend = buyer_store.get(sid).data["spends"][-1]
        assert spend["kind"] == settlement_path
        spend_txid = get_txid(spend["raw"])
        stack.mine_and_confirm(spend_txid)
        stack.mine(policy.buyer_settlement_depth)
        if settlement_path == "split":
            assert await cp_settlement.poll(sid) == "COMPLETED"
        else:
            assert await b_settlement.poll(sid) == "COMPLETED"
        escrow_index = next(
            n
            for n, output in enumerate(parsed.outputs)
            if output.script == buyout.terms.escrow.output_script()
        )
        assert stack.bitcoin("gettxout", txid, str(escrow_index)) == ""


# --------------------------------------------------------------------------- #
# Operator runtime deadline recovery
# --------------------------------------------------------------------------- #


def _core_rpc_adapter(stack: BuyoutRegtest) -> Any:
    """A real Bitcoin Core boundary for a node that publishes no port.

    Every call is a real ``bitcoin-cli`` call against the disposable node, so no
    chain observation is faked; only the HTTP transport is replaced, exactly as
    the taker scenario above does.
    """
    import re

    async def _rpc(self: BuyoutChain, method: str, *params: object) -> Any:
        try:
            result = stack.bitcoin(
                method, *(item if isinstance(item, str) else json.dumps(item) for item in params)
            )
        except RegtestError as exc:
            code = re.search(r"error code:\s*(-?\d+)", str(exc))
            if code:
                raise CoreRpcError(int(code[1])) from None
            raise
        return None if result == "" else result

    return _rpc


@dataclass
class ConfirmedSettlement:
    """A real purchased channel already spent into escrow, ready for a routing-specific settlement."""

    session_id: str
    terms: BuyoutTerms
    parent_txid: str
    escrow_value: int
    buyer_store: BuyoutStore
    counterparty_store: BuyoutStore
    buyer_peer: LndPeerClient
    counterparty_peer: LndPeerClient
    buyer_settlement: BuyoutSettlement
    counterparty_settlement: BuyoutSettlement


@asynccontextmanager
async def _confirmed_settlement(
    stack: BuyoutRegtest, point: Outpoint, policy: BuyoutPolicy
) -> AsyncIterator[ConfirmedSettlement]:
    """Confirm one purchase before testing how its post-spend Lightning payment can, or cannot, route."""

    async def height() -> int:
        return int(stack.bitcoin("getblockcount"))

    async with AsyncExitStack() as resources:
        alice = await resources.enter_async_context(escrow_client(stack, "alice"))
        bob = await resources.enter_async_context(escrow_client(stack, "bob"))
        alice_peer = await resources.enter_async_context(peer_client(stack, "alice"))
        bob_peer = await resources.enter_async_context(peer_client(stack, "bob"))
        buyer_store = resources.enter_context(
            BuyoutStore(
                stack.secrets_dir / f"settlement-b-{secrets.token_hex(4)}" / "sessions.sqlite"
            )
        )
        counterparty_store = resources.enter_context(
            BuyoutStore(
                stack.secrets_dir / f"settlement-c-{secrets.token_hex(4)}" / "sessions.sqlite"
            )
        )
        payout = _p2tr_script(secrets.token_bytes(32)).hex()
        payouts = (payout, _p2tr_script(secrets.token_bytes(32)).hex())
        counterparty = CounterpartySigner(
            counterparty_store, bob, bob_peer, height, policy, payouts
        )

        async def request(peer: str, message: Any) -> Any:
            assert peer == stack.node_ids["bob"]
            return await counterparty.handle(stack.node_ids["alice"], message)

        buyer = BuyoutBuyer(
            buyer_store, alice, alice_peer, request, height, policy, recovery_authorized=True
        )
        session_id = await buyer.prepare(stack.node_ids["bob"], [point], payouts)
        terms = buyer.terms(session_id)
        ordinary, ordinary_prevout = _p2tr_wallet_input(stack)
        escrow_value = CHANNEL_CAPACITY + ordinary_prevout.value - 500_000 - PARENT_FEE
        unsigned = serialize_transaction(
            2,
            [TxInput.from_hex(point.txid, point.vout), ordinary],
            [
                TxOutput(value=500_000, script=bytes.fromhex(payout)),
                TxOutput(value=escrow_value, script=terms.escrow.output_script()),
            ],
            0,
        )
        prevouts = [
            Prevout(value=CHANNEL_CAPACITY, script_pubkey=terms.channels[0].funding_script.hex()),
            ordinary_prevout,
        ]
        signatures = await buyer.sign_parent(session_id, unsigned, prevouts, [0], 1)
        signed_parent = serialize_transaction(
            2,
            parse_transaction_bytes(unsigned).inputs,
            parse_transaction_bytes(unsigned).outputs,
            0,
            witnesses=[[signatures[(point.txid, point.vout)]], []],
        )
        completed = stack.bitcoin("signrawtransactionwithwallet", signed_parent.hex(), wallet=True)
        assert completed["complete"], completed.get("errors")
        parent_txid = str(stack.bitcoin("sendrawtransaction", str(completed["hex"])))
        assert parent_txid == get_txid(unsigned.hex())
        stack.mine_and_confirm(parent_txid)

        chain = BuyoutChain("http://unused.invalid", "unused", "unused")
        await resources.enter_async_context(chain)
        counterparty_settlement = BuyoutSettlement(counterparty_store, bob_peer, chain)

        async def settlement_request(peer: str, message: Any) -> Any:
            assert peer == stack.node_ids["bob"]
            return await counterparty_settlement.handle(stack.node_ids["alice"], message)

        yield ConfirmedSettlement(
            session_id,
            terms,
            parent_txid,
            escrow_value,
            buyer_store,
            counterparty_store,
            alice_peer,
            bob_peer,
            BuyoutSettlement(buyer_store, alice_peer, chain, request=settlement_request),
            counterparty_settlement,
        )


def _deadline_settings(
    stack: BuyoutRegtest,
    node: str,
    endpoint: str,
    peer_identity: str,
    journal_dir: Path,
    *,
    automatic_force_close: bool,
    wallet_fingerprint: str | None = None,
) -> BuyoutSettings:
    """A validated regtest configuration for one operator runtime on ``node``."""
    node_dir = stack.secrets_dir / node
    values: dict[str, Any] = {
        "enabled": True,
        "automatic_force_close": automatic_force_close,
        "network": "regtest",
        "journal": journal_dir / "sessions.sqlite",
        "lnd_endpoint": endpoint,
        "lnd_identity": stack.node_ids[node],
        "lnd_tls_cert": node_dir / "tls.cert",
        "lnd_peer_macaroon": node_dir / "admin.macaroon",
        "lnd_escrow_macaroon": node_dir / "channelescrow.macaroon",
        "bitcoin_rpc_url": "http://127.0.0.1:18443/",
        "bitcoin_rpc_user": RPC_USER,
        "bitcoin_rpc_password": RPC_PASSWORD,
        "allowed_peers": (peer_identity,),
        "payout_addresses": [
            scriptpubkey_to_address(_p2tr_script(secrets.token_bytes(32)), "regtest")
            for _ in range(8)
        ],
        "mixdepth": 0,
        "poll_interval_seconds": 1.0,
    }
    if wallet_fingerprint is not None:
        values["wallet_fingerprint"] = wallet_fingerprint
    return BuyoutSettings.model_validate(values)


def _mine_to(stack: BuyoutRegtest, height: int) -> None:
    current = int(stack.bitcoin("getblockcount"))
    if height > current:
        stack.mine(height - current)
    for node in NODES:
        _wait_for_sync(stack, node)


def _wait_for_active_link(stack: BuyoutRegtest, point: Outpoint) -> None:
    """Block until both endpoints report the released channel as active again."""
    target = f"{point.txid}:{point.vout}"
    deadline = time.monotonic() + CHANNEL_TIMEOUT
    while True:
        listed = {
            node: [
                channel
                for channel in (stack.lncli(node, "listchannels", "--active_only") or {}).get(
                    "channels", []
                )
                if str(channel["channel_point"]) == target
            ]
            for node in NODES
        }
        if all(listed[node] for node in NODES):
            return
        if time.monotonic() >= deadline:
            raise RegtestError(f"channel {target} did not become active again after release")
        time.sleep(1.0)


def _wait_for_spent_channel_removal(stack: BuyoutRegtest, point: Outpoint) -> None:
    """Wait until LND no longer offers the purchased channel as a routing candidate after its spend."""
    target = f"{point.txid}:{point.vout}"
    deadline = time.monotonic() + CHANNEL_TIMEOUT
    while True:
        listed = {
            node: (stack.lncli(node, "listchannels") or {}).get("channels", []) for node in NODES
        }
        if all(
            all(str(channel["channel_point"]) != target for channel in channels)
            for channels in listed.values()
        ):
            return
        if time.monotonic() >= deadline:
            raise RegtestError(f"spent channel {target} remained a routing candidate")
        time.sleep(1.0)


def _payment_record(stack: BuyoutRegtest, payment_hash: str) -> dict[str, Any]:
    """Return the single real LND payment that proves which routing outcome settlement reached."""
    payments = (stack.lncli("alice", "listpayments", "--include_incomplete") or {}).get(
        "payments", []
    )
    matched = [payment for payment in payments if payment.get("payment_hash") == payment_hash]
    assert len(matched) == 1, matched
    return matched[0]


async def test_settlement_pays_over_second_private_channel(
    maker_stack: BuyoutRegtest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remaining private peer channel settles the purchase after its original channel is gone."""
    stack = maker_stack
    monkeypatch.setattr(BuyoutChain, "_rpc", _core_rpc_adapter(stack))
    purchased = _open_private_taproot_channel(stack)
    _open_private_taproot_channel(stack)
    policy = BuyoutPolicy(settlement_enabled=True)
    try:
        async with _confirmed_settlement(stack, purchased, policy) as scenario:
            stack.mine(policy.buyer_settlement_depth)
            _wait_for_sync(stack, "alice")
            _wait_for_sync(stack, "bob")
            assert await scenario.buyer_settlement.poll(scenario.session_id) == "SETTLED"
            invoice = await scenario.counterparty_peer.invoice_status(
                bytes.fromhex(scenario.terms.acceptance.payment_hash)
            )
            assert invoice.state is InvoiceState.SETTLED
            assert invoice.amount_paid_sat == scenario.terms.claim_sat
            payment = _payment_record(stack, scenario.terms.acceptance.payment_hash)
            assert payment["status"] == "SUCCEEDED"

            assert await scenario.buyer_settlement.poll(scenario.session_id) == "SPEND_BROADCAST"
            spends = cast(
                list[dict[str, Any]], scenario.buyer_store.get(scenario.session_id).data["spends"]
            )
            spend = spends[-1]
            assert spend["kind"] == "cooperative"
            assert all(item["kind"] != "split" for item in spends)
            stack.mine_and_confirm(get_txid(spend["raw"]))
            stack.mine(policy.buyer_settlement_depth)
            assert await scenario.buyer_settlement.poll(scenario.session_id) == "COMPLETED"
            assert scenario.buyer_store.get(scenario.session_id).state == "COMPLETED"
    except BaseException:
        print(stack.logs())
        raise


async def test_settlement_pays_over_announced_public_channels(
    maker_stack: BuyoutRegtest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public graph channels route settlement through Carol without payer-supplied route hints."""
    stack = maker_stack
    monkeypatch.setattr(BuyoutChain, "_rpc", _core_rpc_adapter(stack))
    purchased = _open_private_taproot_channel(stack)
    stack.lncli("alice", "connect", f"{stack.node_ids['carol']}@carol:9735")
    stack.lncli("carol", "connect", f"{stack.node_ids['bob']}@bob:9735")
    _open_announced_channel(stack, "alice", "carol", push_sat=0)
    _open_announced_channel(stack, "carol", "bob", push_sat=CHANNEL_PUSH)
    _wait_for_announced_channels(
        stack,
        {
            frozenset((stack.node_ids["alice"], stack.node_ids["carol"])),
            frozenset((stack.node_ids["carol"], stack.node_ids["bob"])),
        },
    )
    policy = BuyoutPolicy(settlement_enabled=True)
    try:
        async with _confirmed_settlement(stack, purchased, policy) as scenario:
            assert scenario.buyer_peer._payment_route_hints == ()
            stack.mine(policy.buyer_settlement_depth)
            _wait_for_sync(stack, "alice")
            _wait_for_sync(stack, "bob")
            assert await scenario.buyer_settlement.poll(scenario.session_id) == "SETTLED"
            invoice = await scenario.counterparty_peer.invoice_status(
                bytes.fromhex(scenario.terms.acceptance.payment_hash)
            )
            assert invoice.state is InvoiceState.SETTLED
            assert invoice.amount_paid_sat == scenario.terms.claim_sat
            payment = _payment_record(stack, scenario.terms.acceptance.payment_hash)
            assert payment["status"] == "SUCCEEDED"
            hops = payment["htlcs"][-1]["route"]["hops"]
            assert [hop["pub_key"] for hop in hops] == [
                stack.node_ids["carol"],
                stack.node_ids["bob"],
            ]

            assert await scenario.buyer_settlement.poll(scenario.session_id) == "SPEND_BROADCAST"
            spends = cast(
                list[dict[str, Any]], scenario.buyer_store.get(scenario.session_id).data["spends"]
            )
            spend = spends[-1]
            assert spend["kind"] == "cooperative"
            stack.mine_and_confirm(get_txid(spend["raw"]))
            stack.mine(policy.buyer_settlement_depth)
            assert await scenario.buyer_settlement.poll(scenario.session_id) == "COMPLETED"
    except BaseException:
        print(stack.logs())
        raise


async def test_failed_settlement_broadcasts_the_presigned_split(
    maker_stack: BuyoutRegtest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A route-less private invoice records one failed payment before the buyer recovers with its split."""
    stack = maker_stack
    monkeypatch.setattr(BuyoutChain, "_rpc", _core_rpc_adapter(stack))
    policy = BuyoutPolicy(
        settlement_enabled=True,
        csv_delay=144,
        buyer_settlement_depth=3,
        # LND rejects 80 with "cltv limit 80 should be greater than 83": its
        # route selection adds three blocks to the invoice's 80-block final delta.
        cltv_limit=84,
        settlement_depth=3,
    )
    purchased = _open_private_taproot_channel(stack)
    stack.lncli("alice", "connect", f"{stack.node_ids['carol']}@carol:9735")
    _open_private_taproot_channel(stack, "alice", "carol", push_sat=0)
    try:
        async with _confirmed_settlement(stack, purchased, policy) as scenario:
            pay = scenario.buyer_peer.pay
            payment_attempts = 0

            async def record_payment(*args: Any, **kwargs: Any) -> Any:
                nonlocal payment_attempts
                payment_attempts += 1
                return await pay(*args, **kwargs)

            monkeypatch.setattr(scenario.buyer_peer, "pay", record_payment)
            parent_height = int(stack.bitcoin("getblockcount"))
            stack.mine(policy.buyer_settlement_depth)
            _wait_for_sync(stack, "alice")
            _wait_for_sync(stack, "bob")
            _wait_for_spent_channel_removal(stack, purchased)
            assert await scenario.buyer_settlement.poll(scenario.session_id) == "PAYMENT_FAILED"
            assert payment_attempts == 1
            payment = _payment_record(stack, scenario.terms.acceptance.payment_hash)
            assert payment["status"] == "FAILED"
            assert payment["failure_reason"] == "FAILURE_REASON_NO_ROUTE"
            assert scenario.buyer_store.get(scenario.session_id).data["payment_failed"] is True

            _mine_to(stack, parent_height + policy.csv_delay - 1)
            assert await scenario.buyer_settlement.poll(scenario.session_id) == "SPEND_BROADCAST"
            assert payment_attempts == 1
            spends = cast(
                list[dict[str, Any]], scenario.buyer_store.get(scenario.session_id).data["spends"]
            )
            spend = spends[-1]
            assert spend["kind"] == "split"
            split_txid = get_txid(spend["raw"])
            stack.mine_and_confirm(split_txid)
            split = parse_transaction_bytes(bytes.fromhex(spend["raw"]))
            expected_buyer = (
                scenario.escrow_value
                - scenario.terms.counterparty_split_sat
                - scenario.terms.proposal.split_fee
            )
            assert {(output.script, output.value) for output in split.outputs} == {
                (bytes.fromhex(scenario.terms.proposal.split_script_B), expected_buyer),
                (
                    bytes.fromhex(scenario.terms.acceptance.split_script_C),
                    scenario.terms.counterparty_split_sat,
                ),
            }
            assert stack.bitcoin("gettxout", scenario.parent_txid, "1") == ""
    except BaseException:
        print(stack.logs())
        raise


async def test_runtime_deadline_recovery_cancels_unsigned_then_force_closes_signed(
    stack: BuyoutRegtest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two real operator runtimes recover both freeze deadlines on a live channel.

    The endpoints are whole :class:`BuyoutRuntime` instances: each one verifies
    its own node, opens its own journal, and negotiates over the real private
    custom-message transport. The unsigned session is released at the agreed TTL
    and the channel is reusable afterwards; the signed, unpaid session reaches
    the freeze limit and the buyer, and only the buyer, closes the channel on
    chain without ever attempting a Lightning payment.
    """
    monkeypatch.setattr(BuyoutChain, "_rpc", _core_rpc_adapter(stack))
    fingerprint = secrets.token_hex(4)
    point = _open_private_taproot_channel(stack)
    ordinary, ordinary_prevout = _p2tr_wallet_input(stack)
    try:
        async with AsyncExitStack() as resources:
            buyer_endpoint = await resources.enter_async_context(grpc_endpoint(stack, "alice"))
            cp_endpoint = await resources.enter_async_context(grpc_endpoint(stack, "bob"))
            buyer = await resources.enter_async_context(
                BuyoutRuntime(
                    _deadline_settings(
                        stack,
                        "alice",
                        buyer_endpoint,
                        stack.node_ids["bob"],
                        stack.secrets_dir / "deadline-buyer",
                        automatic_force_close=True,
                        wallet_fingerprint=fingerprint,
                    )
                )
            )
            cp = await resources.enter_async_context(
                BuyoutRuntime(
                    _deadline_settings(
                        stack,
                        "bob",
                        cp_endpoint,
                        stack.node_ids["alice"],
                        stack.secrets_dir / "deadline-counterparty",
                        automatic_force_close=False,
                    ),
                    counterparty=True,
                )
            )
            assert buyer.runtime_binding == {
                "network": "regtest",
                "lnd_identity": stack.node_ids["alice"],
                "mixdepth": 0,
                "wallet_fingerprint": fingerprint,
            }
            assert cp.runtime_binding == {
                "network": "regtest",
                "lnd_identity": stack.node_ids["bob"],
                "mixdepth": 0,
            }

            # -- An unsigned session is released at the agreed TTL ----------- #
            unsigned = await buyer.prepare(stack.node_ids["bob"], [point])
            records = {"buyer": buyer.store.get(unsigned), "cp": cp.store.get(unsigned)}
            assert records["buyer"].peer_pubkey == stack.node_ids["bob"]
            assert records["cp"].peer_pubkey == stack.node_ids["alice"]
            assert records["buyer"].data["runtime_binding"] == buyer.runtime_binding
            assert records["cp"].data["runtime_binding"] == cp.runtime_binding
            for record in records.values():
                assert record.data["recovery_authorized"] is True
                assert record.data["settlement_authorized"] is True
                assert not record.parent_signing_started
            assert records["buyer"].data["force_close_authorized"] is True
            assert records["cp"].data["force_close_authorized"] is False

            terms = buyer.buyer.terms(unsigned)
            freeze_height = int(str(records["buyer"].data["freeze_height"]))
            _mine_to(stack, freeze_height + terms.proposal.freeze_ttl_blocks)
            assert await buyer.poll_once() == {unsigned: "CANCELED"}
            assert await cp.poll_once() == {unsigned: "CANCELED"}
            # Releasing a lock must not touch the channel it protected.
            assert stack.bitcoin("gettxout", point.txid, str(point.vout)) != ""
            _wait_for_active_link(stack, point)

            # -- A signed but unpaid session reaches the freeze limit -------- #
            signed_session = await buyer.prepare(stack.node_ids["bob"], [point])
            assert signed_session != unsigned
            terms = buyer.buyer.terms(signed_session)
            inputs = [TxInput.from_hex(point.txid, point.vout), ordinary]
            outputs = [
                TxOutput(value=500_000, script=_p2tr_script(secrets.token_bytes(32))),
                TxOutput(
                    value=CHANNEL_CAPACITY + ordinary_prevout.value - 500_000 - PARENT_FEE,
                    script=terms.escrow.output_script(),
                ),
            ]
            parent = serialize_transaction(2, inputs, outputs, 0)
            prevouts = [
                Prevout(
                    value=CHANNEL_CAPACITY, script_pubkey=terms.channels[0].funding_script.hex()
                ),
                ordinary_prevout,
            ]
            signatures = await buyer.buyer.sign_parent(signed_session, parent, prevouts, [0], 1)
            assert len(signatures[(point.txid, point.vout)]) == 64
            assert buyer.store.get(signed_session).parent_signing_started
            assert cp.store.get(signed_session).parent_signing_started
            parent_txid = get_txid(parent.hex())
            # The parent is deliberately never broadcast, which is the deadline
            # the force close exists for: the funding output is still unspent.
            assert (
                stack.bitcoin(
                    "gettxspendingprevout",
                    json.dumps([{"txid": point.txid, "vout": point.vout}]),
                )[0].get("spendingtxid")
                is None
            )

            freeze_height = int(str(buyer.store.get(signed_session).data["freeze_height"]))
            _mine_to(stack, freeze_height + terms.acceptance.max_freeze_blocks)
            assert await buyer.poll_once() == {signed_session: "FORCE_CLOSE_PENDING"}
            spenders = stack.bitcoin(
                "gettxspendingprevout", json.dumps([{"txid": point.txid, "vout": point.vout}])
            )
            closing_txid = str(spenders[0]["spendingtxid"])
            assert closing_txid != parent_txid
            stack.mine_and_confirm(closing_txid)

            # The funding output is spent outside the immutable parent.
            assert await buyer.poll_once() == {signed_session: "CONFLICTED"}
            assert stack.bitcoin("gettxout", point.txid, str(point.vout)) == ""
            for store in (buyer.store, cp.store):
                data = store.get(signed_session).data
                assert data.get("payment_started") is None
                assert data.get("invoice_started") is None
                assert data.get("settlement_preimage") is None
    except BaseException:
        print(stack.logs())
        raise


# --------------------------------------------------------------------------- #
# A maker funds its own CoinJoin from a live channel
# --------------------------------------------------------------------------- #

MAKER_WALLET_INPUT_BTC = "0.01"
MAKER_CJ_AMOUNT = 800_000
MAKER_CJFEE = 500
MAKER_COINJOIN_FEE = 2_000
MAKER_MIXDEPTH = 0
MAKER_UNBOUND_MIXDEPTH = 3


def _core_backend(stack: BuyoutRegtest) -> Any:
    """A maker blockchain backend answering from the disposable Core node.

    Only the transport is a test double: every value, script, confirmation
    count and height the maker validates comes from a real ``gettxout``.
    """
    from unittest.mock import AsyncMock, MagicMock

    from jmwallet.backends.base import UTXO

    async def block_height() -> int:
        return int(stack.bitcoin("getblockcount"))

    async def get_utxo(txid: str, vout: int) -> UTXO | None:
        entry = stack.bitcoin("gettxout", txid, str(vout))
        if entry == "":
            return None
        confirmations = int(entry["confirmations"])
        script = str(entry["scriptPubKey"]["hex"])
        return UTXO(
            txid=txid,
            vout=vout,
            value=int(Decimal(str(entry["value"])) * SATS_PER_BTC),
            address=scriptpubkey_to_address(bytes.fromhex(script), "regtest"),
            confirmations=confirmations,
            scriptpubkey=script,
            height=None if confirmations < 1 else await block_height() - confirmations + 1,
        )

    backend = MagicMock()
    backend.requires_neutrino_metadata.return_value = False
    backend.can_lookup_arbitrary_utxos.return_value = True
    backend.get_block_height = AsyncMock(side_effect=block_height)
    backend.get_utxo = AsyncMock(side_effect=get_utxo)
    return backend


def _bound_wallet(utxo: Any, fingerprint: str) -> Any:
    """A Taproot wallet holding exactly ``utxo`` in the bound mixdepth.

    The wallet is a stub because the maker's own coins are not what this test
    proves, but everything it hands the session is real: a chain-derived UTXO,
    a real secp256k1 authentication key and, for the signature itself, whatever
    the caller installs on ``sign_input``.
    """
    from unittest.mock import AsyncMock, MagicMock

    wallet = MagicMock()
    wallet.network = "regtest"
    wallet.address_type = "p2tr"
    wallet.wallet_fingerprint = fingerprint
    wallet.mixdepth_count = 5
    wallet.get_locked_input_outpoints.return_value = set()
    wallet.get_maker_rotation_lineage_outpoints = AsyncMock(return_value=set())
    balances = {MAKER_MIXDEPTH: 100_000_000, MAKER_UNBOUND_MIXDEPTH: 100_000_000}

    async def balance(mixdepth: int, **_: Any) -> int:
        return balances.get(mixdepth, 0)

    wallet.get_balance_for_offers = AsyncMock(side_effect=balance)
    wallet.select_utxos_with_merge.return_value = [utxo]
    wallet.reserve_coinjoin_inputs.return_value = True
    wallet.renew_coinjoin_inputs.return_value = True
    wallet.get_new_internal_address.return_value = scriptpubkey_to_address(
        _p2tr_script(secrets.token_bytes(32)), "regtest"
    )
    secret = secrets.token_bytes(32)
    key = MagicMock()
    key.get_public_key_bytes.return_value = bytes(CKey(secret).pub)
    key.get_private_key_bytes.return_value = secret
    wallet.get_key_for_address.return_value = key
    return wallet


@pytest.mark.parametrize(
    ("reorg_after_payment", "high_fee_split", "rejected_sweep"),
    [
        (False, False, None),
        (True, False, None),
        (False, True, None),
        (False, False, "cooperative"),
        (False, False, "claim"),
    ],
)
async def test_maker_funds_its_own_coinjoin_from_a_live_channel(
    maker_stack: BuyoutRegtest,
    monkeypatch: pytest.MonkeyPatch,
    reorg_after_payment: bool,
    high_fee_split: bool,
    rejected_sweep: str | None,
) -> None:
    """A maker funds a live CoinJoin and exercises settlement or timeout recovery.

    The maker side is the real :class:`maker.coinjoin.CoinJoinSession` driven
    through ``!fill``, ``!auth`` and ``!tx``, holding a real
    :class:`~jmswap.coinjoin_funding.ChannelBuyout` over two whole operator
    runtimes that negotiate across the private custom-message transport. The
    channel signature is produced by the buyout's own MuSig2 session and
    arrives on the wire as an ordinary Taproot key-spend signature, so this
    proves the claims the unit tests cannot:

    * the channel funding output is never wallet state (never selected, never
      signed by the wallet, never an address in the wallet's history) while it
      still pays for the round,
    * the escrow is the maker's change destination and keeps the whole channel
      value plus the maker's ordinary input minus its CoinJoin output,
    * the resulting transaction is accepted, relayed and mined by Bitcoin Core
      with the channel's 64-byte key-path signature in it, and
    * paid purchases settle cooperatively, including after a parent reorg;
      unpaid timeout splits survive fee-policy rejection and replay unchanged
      once the node accepts their fee.
    """
    import base64
    from types import SimpleNamespace
    from unittest.mock import patch

    from jmcore.encryption import CryptoSession
    from jmcore.models import Offer, OfferType
    from jmwallet.wallet.models import UTXOInfo
    from maker.coinjoin import CoinJoinSession, CoinJoinState

    from jmswap.coinjoin_funding import ChannelBuyout

    stack = maker_stack
    monkeypatch.setattr(BuyoutChain, "_rpc", _core_rpc_adapter(stack))
    fingerprint = secrets.token_hex(4)
    point = _open_private_taproot_channel(stack)
    # The purchased channel is spent by this CoinJoin, so the cooperative
    # settlement that follows needs an independent route to the counterparty.
    _open_private_taproot_channel(stack)
    maker_input, maker_prevout = _p2tr_wallet_input(stack, MAKER_WALLET_INPUT_BTC)
    taker_input, taker_prevout = _p2tr_wallet_input(stack)
    # The taker's authorization UTXO must age past the maker's policy.
    stack.mine(6)

    def address() -> str:
        return scriptpubkey_to_address(_p2tr_script(secrets.token_bytes(32)), "regtest")

    try:
        async with AsyncExitStack() as resources:
            buyer_endpoint = await resources.enter_async_context(grpc_endpoint(stack, "alice"))
            cp_endpoint = await resources.enter_async_context(grpc_endpoint(stack, "bob"))
            buyer = await resources.enter_async_context(
                BuyoutRuntime(
                    _deadline_settings(
                        stack,
                        "alice",
                        buyer_endpoint,
                        stack.node_ids["bob"],
                        stack.secrets_dir / "maker-buyer",
                        automatic_force_close=False,
                        wallet_fingerprint=fingerprint,
                    )
                )
            )
            cp = await resources.enter_async_context(
                BuyoutRuntime(
                    _deadline_settings(
                        stack,
                        "bob",
                        cp_endpoint,
                        stack.node_ids["alice"],
                        stack.secrets_dir / "maker-counterparty",
                        automatic_force_close=False,
                    ),
                    counterparty=True,
                )
            )
            session_id = await buyer.prepare(stack.node_ids["bob"], [point])
            buyout = ChannelBuyout(buyer.buyer, session_id)
            escrow_script = buyout.terms.escrow.output_script()

            # -- The maker session, bound to this runtime's wallet ----------- #
            wallet_utxo = UTXOInfo(
                txid=maker_input.txid,
                vout=maker_input.vout,
                value=maker_prevout.value,
                address=scriptpubkey_to_address(
                    bytes.fromhex(maker_prevout.script_pubkey), "regtest"
                ),
                confirmations=7,
                scriptpubkey=maker_prevout.script_pubkey,
                path="m/86'/1'/0'/0/0",
                mixdepth=MAKER_MIXDEPTH,
            )
            wallet = _bound_wallet(wallet_utxo, fingerprint)
            session = CoinJoinSession(
                taker_nick="J5taker",
                offer=Offer(
                    counterparty="J5maker",
                    oid=0,
                    ordertype=OfferType.TR0_ABSOLUTE,
                    minsize=10_000,
                    maxsize=5_000_000,
                    txfee=0,
                    cjfee=MAKER_CJFEE,
                ),
                wallet=wallet,
                backend=_core_backend(stack),
                buyout=buyout,
                restrict_md0=False,
            )
            assert session.buyout_mixdepth == MAKER_MIXDEPTH

            commitment = "ab" * 32
            filled, _ = await session.handle_fill(
                MAKER_CJ_AMOUNT, commitment, CryptoSession().get_pubkey_hex()
            )
            assert filled
            revelation: dict[str, Any] = {
                "P": b"\x02" + b"\x01" * 32,
                "P2": b"\x02" + b"\x02" * 32,
                "sig": b"\x03" * 32,
                "e": b"\x04" * 32,
                "txid": taker_input.txid,
                "vout": taker_input.vout,
            }
            with (
                patch("maker.coinjoin.parse_podle_revelation", return_value=revelation),
                patch("maker.coinjoin.verify_podle", return_value=(True, "")),
                patch("maker.coinjoin.verify_podle_binding", return_value=(True, "")),
            ):
                authorized, ioauth = await session.handle_auth(commitment, dict(revelation), "")
            assert authorized, ioauth

            # The channel pays for the round without ever being wallet state.
            channel_outpoint = (point.txid, point.vout)
            assert set(session.our_utxos) == {(maker_input.txid, maker_input.vout)}
            assert channel_outpoint not in session.our_utxos
            assert session.channel_prevouts == {
                channel_outpoint: (
                    CHANNEL_CAPACITY,
                    buyout.terms.channels[0].funding_script,
                )
            }
            assert session.channel_heights[channel_outpoint] is not None
            assert wallet.select_utxos_with_merge.call_args.args[0] == MAKER_MIXDEPTH
            assert channel_outpoint in wallet.select_utxos_with_merge.call_args.kwargs["exclude"]
            assert ioauth["utxo_list"] == (
                f"{maker_input.txid}:{maker_input.vout},{point.txid}:{point.vout}"
            )
            # Escrow is the change destination, and never wallet address history.
            assert session.change_address == buyout.change_address
            assert ioauth["change_addr"] == buyout.change_address
            assert session.wallet_change_address == ""
            wallet.get_new_internal_address.assert_called_once_with(MAKER_MIXDEPTH + 1)

            # -- The taker assembles the CoinJoin ---------------------------- #
            escrow_change = maker_prevout.value + CHANNEL_CAPACITY - MAKER_CJ_AMOUNT + MAKER_CJFEE
            total_in = maker_prevout.value + CHANNEL_CAPACITY + taker_prevout.value
            taker_change = total_in - 2 * MAKER_CJ_AMOUNT - escrow_change - MAKER_COINJOIN_FEE
            assert taker_change > 0
            inputs = [
                TxInput.from_hex(point.txid, point.vout),
                taker_input,
                maker_input,
            ]
            outputs = [
                TxOutput(value=MAKER_CJ_AMOUNT, script=address_to_scriptpubkey(address())),
                TxOutput(value=escrow_change, script=escrow_script),
                TxOutput(
                    value=MAKER_CJ_AMOUNT,
                    script=address_to_scriptpubkey(session.cj_address),
                ),
                TxOutput(
                    value=taker_change,
                    script=address_to_scriptpubkey(
                        str(stack.bitcoin("getnewaddress", "taker-change", "bech32m", wallet=True))
                    ),
                ),
            ]
            unsigned = serialize_transaction(2, inputs, outputs, 0)

            # Bitcoin Core owns the two ordinary inputs, so it produces their
            # real signatures; it cannot sign the channel input.
            core_signed = stack.bitcoin("signrawtransactionwithwallet", unsigned.hex(), wallet=True)
            parsed_signed = parse_transaction_bytes(bytes.fromhex(str(core_signed["hex"])))
            core_witnesses = {
                (item.txid, item.vout): list(witness)
                for item, witness in zip(parsed_signed.inputs, parsed_signed.witnesses, strict=True)
            }
            assert not core_witnesses[channel_outpoint]

            def sign_wallet(tx: Any, index: int, utxo: UTXOInfo, **kwargs: Any) -> Any:
                assert utxo == wallet_utxo
                assert len(kwargs["prevout_values"]) == len(inputs)
                witness = core_witnesses[(utxo.txid, utxo.vout)]
                assert len(witness) == 1 and len(witness[0]) == 64
                return SimpleNamespace(
                    signature=witness[0],
                    pubkey=bytes.fromhex(utxo.scriptpubkey)[2:],
                )

            wallet.sign_input.side_effect = sign_wallet

            # -- The maker validates and signs the real parent --------------- #
            session.state = CoinJoinState.IOAUTH_SENT
            signed_ok, response = await session.handle_tx(unsigned.hex())
            assert signed_ok, response
            assert session.state is CoinJoinState.SIG_SENT
            # The wallet signed its own input and nothing else.
            wallet.sign_input.assert_called_once()
            wallet.get_private_key.assert_not_called()

            wallet_sigmsg, channel_sigmsg = (
                base64.b64decode(item) for item in response["signatures"]
            )
            funding_script = buyout.terms.channels[0].funding_script
            assert channel_sigmsg == (
                bytes([64]) + channel_sigmsg[1:65] + bytes([32]) + funding_script[2:]
            )
            assert wallet_sigmsg[1:65] == core_witnesses[(maker_input.txid, maker_input.vout)][0]
            assert buyer.store.get(session_id).parent_signing_started
            assert cp.store.get(session_id).parent_signing_started

            witnesses = {
                channel_outpoint: [channel_sigmsg[1:65]],
                (maker_input.txid, maker_input.vout): [wallet_sigmsg[1:65]],
                (taker_input.txid, taker_input.vout): core_witnesses[
                    (taker_input.txid, taker_input.vout)
                ],
            }
            final = serialize_transaction(
                2,
                inputs,
                outputs,
                0,
                witnesses=[
                    witnesses[(item.txid, item.vout)]
                    for item in parse_transaction_bytes(unsigned).inputs
                ],
            )
            assert all(
                len(witness) == 1 and len(witness[0]) == 64
                for witness in parse_transaction_bytes(final).witnesses
            )
            acceptance = stack.bitcoin("testmempoolaccept", f'["{final.hex()}"]')[0]
            assert acceptance["allowed"], acceptance
            txid = str(stack.bitcoin("sendrawtransaction", final.hex()))
            assert txid == response["txid"] == get_txid(unsigned.hex())
            stack.mine_and_confirm(txid)

            # The channel is gone and its value sits in the maker's escrow.
            assert stack.bitcoin("gettxout", point.txid, str(point.vout)) == ""
            escrow_output = stack.bitcoin("gettxout", txid, "1")
            assert escrow_output["scriptPubKey"]["hex"] == escrow_script.hex()
            assert int(Decimal(str(escrow_output["value"])) * SATS_PER_BTC) == escrow_change

            if high_fee_split:
                # Apply a node-local fee penalty to model rejection under fee
                # pressure without filling unrelated mempools or changing fees
                # on the immutable, jointly presigned timeout transaction.
                parent_observed = await buyer.chain.transaction(txid)
                assert parent_observed is not None and parent_observed.height is not None
                _mine_to(stack, parent_observed.height + buyout.terms.proposal.csv_delay)
                split_raw = buyer.store.get(session_id).data["split_tx"]
                split_txid = get_txid(split_raw)
                assert stack.bitcoin("testmempoolaccept", json.dumps([split_raw]))[0]["allowed"]
                stack.bitcoin("prioritisetransaction", split_txid, "0", "-1000000")
                rejected = stack.bitcoin("testmempoolaccept", json.dumps([split_raw]))[0]
                assert not rejected["allowed"]
                assert "fee" in rejected["reject-reason"]
                assert (await buyer.poll_once())[session_id] == "ERROR:CoreRpcError"
                failed = buyer.store.get(session_id)
                assert failed.state == "SPEND_BROADCASTING"
                assert failed.data.get("payment_started") is None
                assert failed.data["spends"][0]["raw"] == split_raw
                assert split_txid not in stack.bitcoin("getrawmempool")

                # The current recovery contract retries the exact split once
                # fee policy permits it; it cannot increase its signed fee.
                stack.bitcoin("prioritisetransaction", split_txid, "0", "1000000")
                assert (await buyer.poll_once())[session_id] == "SPEND_BROADCAST"
                assert stack.bitcoin("getrawtransaction", split_txid) == split_raw
                assert len(buyer.store.get(session_id).data["spends"]) == 1
                stack.mine_and_confirm(split_txid)
                _mine_to(stack, int(stack.bitcoin("getblockcount")) + 6)
                assert (await buyer.poll_once())[session_id] == "COMPLETED"
                return

            # -- The purchase settles cooperatively -------------------------- #
            depth = buyout.terms.proposal.buyer_settlement_depth
            _mine_to(stack, int(stack.bitcoin("getblockcount")) + depth)
            assert (await buyer.poll_once())[session_id] == "SETTLED"
            if rejected_sweep is not None:
                if rejected_sweep == "claim":
                    buyer.settlement.request = None
                broadcast = buyer.chain.broadcast
                rejected_txid: str | None = None

                async def penalize_first_sweep(raw: bytes) -> str:
                    nonlocal rejected_txid
                    if rejected_txid is None:
                        rejected_txid = get_txid(raw.hex())
                        stack.bitcoin("prioritisetransaction", rejected_txid, "0", "-1000000")
                    return await broadcast(raw)

                monkeypatch.setattr(buyer.chain, "broadcast", penalize_first_sweep)
                assert (await buyer.poll_once())[session_id] == "ERROR:CoreRpcError"
                saved = buyer.store.get(session_id).data["spends"][-1]
                assert saved["kind"] == rejected_sweep
                assert rejected_txid not in stack.bitcoin("getrawmempool")
                assert (await buyer.poll_once())[session_id] == "ERROR:CoreRpcError"
                stack.mine(buyer.settlement.policy.bump_after_blocks)
                assert (await buyer.poll_once())[session_id] == "SPEND_BROADCAST"
                replacement = buyer.store.get(session_id).data["spends"][-1]
                assert replacement["kind"] == "claim"
                assert replacement["fee"] > saved["fee"]
                assert get_txid(replacement["raw"]) != rejected_txid
                stack.mine_and_confirm(get_txid(replacement["raw"]))
                _mine_to(stack, int(stack.bitcoin("getblockcount")) + depth)
                assert (await buyer.poll_once())[session_id] == "COMPLETED"
                return
            if reorg_after_payment:
                record = buyer.store.get(session_id)
                assert record.data["finalized_parent"] == final.hex()
                paid_preimage = record.data["settlement_preimage"]

                async def refuse_second_payment(*args: Any, **kwargs: Any) -> Any:
                    raise AssertionError("a reorganization must never trigger another payment")

                monkeypatch.setattr(buyer.peer, "pay", refuse_second_payment)
                # Keep wallet reacceptance from rescuing the parent for us.
                stack.bitcoin("unloadwallet", WALLET, "false")
                stack.bitcoin("invalidateblock", record.data["payment_parent_block"])
                # Core normally returns disconnected transactions to its mempool.
                # This disposable node disables mempool persistence so a restart
                # exercises loss of the parent as well as loss of confirmations.
                stack.compose("restart", "bitcoin")
                deadline = time.monotonic() + READY_TIMEOUT
                while True:
                    try:
                        stack.bitcoin("getblockcount")
                        break
                    except RegtestError:
                        if time.monotonic() >= deadline:
                            raise
                        await asyncio.sleep(0.5)
                assert txid not in stack.bitcoin("getrawmempool")
                assert await buyer.chain.transaction(txid) is None
                assert (await buyer.poll_once())[session_id] == "PARENT_REBROADCAST"
                assert stack.bitcoin("getrawtransaction", txid) == final.hex()
                assert buyer.store.get(session_id).data["settlement_preimage"] == paid_preimage
                stack.bitcoin("loadwallet", WALLET)
                stack.mine_and_confirm(txid)
                _mine_to(stack, int(stack.bitcoin("getblockcount")) + depth)
            assert (await buyer.poll_once())[session_id] == "SPEND_BROADCAST"
            spend = buyer.store.get(session_id).data["spends"][-1]
            assert spend["kind"] == "cooperative"
            stack.mine_and_confirm(get_txid(spend["raw"]))
            _mine_to(stack, int(stack.bitcoin("getblockcount")) + depth)
            assert (await buyer.poll_once())[session_id] == "COMPLETED"
            assert stack.bitcoin("gettxout", txid, "1") == ""
    except BaseException:
        print(stack.logs())
        raise
