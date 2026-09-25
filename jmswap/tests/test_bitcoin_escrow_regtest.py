"""Bitcoin Core regtest acceptance for the channel-buyout escrow primitives.

The unit tests prove the escrow artifacts are internally consistent and
cryptographically valid. They cannot prove that a real node would accept them,
so this module replays the same artifacts against ``bitcoind`` and asserts what
consensus and policy actually do with them:

* the presigned split is rejected exactly one block before its BIP68 relative
  lock matures, and accepted and mined on the very next block,
* the cooperative key-path sweep confirms on its own escrow output,
* the preimage claim confirms on a third escrow output, while a claim carrying a
  wrong preimage or a corrupted signature is rejected by script verification,
* an unconfirmed claim can be replaced by a higher-fee claim (BIP125).

The node is a disposable, uniquely named container started for this module only.
It publishes no ports, mounts nothing, makes no outbound connections, and every
RPC goes through ``docker exec ... bitcoin-cli``. All coins are regtest coins
mined by the test itself. The container is force-removed in the fixture's
``finally`` block, including when a test fails.

These tests are marked ``docker`` and are therefore opt-in: they never skip. If
Docker or the image is unavailable they fail with an explicit error.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import parse_transaction_bytes, taproot_tweak_pubkey
from jmcore.musig2 import nonce_agg, sign_partial
from jmcore.taproot import TaprootTx, TxIn, TxOut

from jmswap.bitcoin_escrow import (
    MIN_CSV_DELAY,
    BuyoutEscrow,
    EscrowOutpoint,
    UnsignedKeyPathSpend,
    build_claim,
    build_cooperative_sweep,
    build_split,
    escrow_nonce,
    escrow_session,
    finalize_key_path_spend,
    verify_signed_claim,
    verify_signed_cooperative_sweep,
    verify_signed_split,
)

pytestmark = [pytest.mark.docker, pytest.mark.timeout(600)]

# --------------------------------------------------------------------------- #
# Disposable regtest node
# --------------------------------------------------------------------------- #

IMAGE = "bitcoin/bitcoin:30.0"
RPC_USER = "jmswap"
RPC_PASSWORD = "jmswap-regtest"
WALLET = "jmswap"
RPC_TIMEOUT = 120
DOCKER_TIMEOUT = 300
STARTUP_TIMEOUT = 120.0

DOCKER_REQUIRED = (
    "this suite is marked 'docker' and is opt-in, so it must never skip: it needs a "
    f"working Docker daemon and the {IMAGE} image"
)

SATS_PER_BTC = Decimal(100_000_000)


class RegtestRpcError(RuntimeError):
    """A ``bitcoin-cli`` call against the disposable node failed."""


def _decode(stdout: str) -> Any:
    text = stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@dataclass(frozen=True)
class RegtestNode:
    """A running regtest ``bitcoind``, reachable only through ``docker exec``."""

    container: str

    def _run(self, args: Sequence[str], *, wallet: bool) -> subprocess.CompletedProcess[str]:
        command = [
            "docker",
            "exec",
            self.container,
            "bitcoin-cli",
            "-regtest",
            f"-rpcuser={RPC_USER}",
            f"-rpcpassword={RPC_PASSWORD}",
        ]
        if wallet:
            command.append(f"-rpcwallet={WALLET}")
        command.extend(args)
        return subprocess.run(command, capture_output=True, text=True, timeout=RPC_TIMEOUT)

    def rpc(self, *args: str, wallet: bool = False) -> Any:
        """Run an RPC that is expected to succeed and return its decoded result."""
        done = self._run(args, wallet=wallet)
        if done.returncode != 0:
            raise RegtestRpcError(
                f"'{args[0]}' failed: {done.stderr.strip() or done.stdout.strip()}"
            )
        return _decode(done.stdout)

    def rpc_error(self, *args: str, wallet: bool = False) -> str:
        """Run an RPC that must fail and return the node's error text."""
        done = self._run(args, wallet=wallet)
        if done.returncode == 0:
            raise AssertionError(f"'{args[0]}' unexpectedly succeeded: {done.stdout.strip()}")
        return (done.stderr.strip() or done.stdout.strip()).lower()

    def height(self) -> int:
        return int(self.rpc("getblockcount"))

    def mine(self, count: int) -> list[str]:
        address = self.rpc("getnewaddress", wallet=True)
        return list(self.rpc("generatetoaddress", str(count), address, wallet=True))

    def mine_to_height(self, target: int) -> None:
        current = self.height()
        if current > target:
            raise AssertionError(f"chain is already at height {current}, past the target {target}")
        if current < target:
            self.mine(target - current)

    def broadcast(self, signed_tx: bytes) -> str:
        return str(self.rpc("sendrawtransaction", signed_tx.hex()))

    def mempool(self) -> list[str]:
        return list(self.rpc("getrawmempool"))

    def mine_and_confirm(self, txid: str) -> None:
        """Mine one block and assert it contains ``txid``."""
        block_hash = self.mine(1)[0]
        block = self.rpc("getblock", block_hash)
        if txid not in block["tx"]:
            raise AssertionError(f"{txid} was not mined into block {block_hash}")
        assert int(self.rpc("getrawtransaction", txid, "true")["confirmations"]) >= 1


def _require_docker() -> None:
    try:
        done = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"{DOCKER_REQUIRED}: {exc}") from exc
    if done.returncode != 0:
        raise RuntimeError(f"{DOCKER_REQUIRED}: {done.stderr.strip()}")


def _wait_for_rpc(node: RegtestNode) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    last = "no attempt completed"
    while time.monotonic() < deadline:
        try:
            node.rpc("getblockchaininfo")
        except (RegtestRpcError, subprocess.SubprocessError) as exc:
            last = str(exc)
            time.sleep(0.5)
        else:
            return
    raise RuntimeError(f"bitcoind did not answer RPC within {STARTUP_TIMEOUT}s: {last}")


@pytest.fixture(scope="module")
def node() -> Iterator[RegtestNode]:
    """Start one throwaway regtest node and always tear it down."""
    _require_docker()
    container = f"jmswap-escrow-regtest-{uuid.uuid4().hex[:12]}"
    started = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container,
            IMAGE,
            "-regtest",
            "-listen=0",
            "-dnsseed=0",
            "-connect=0",
            "-fallbackfee=0.0002",
            # Escrow outputs are not wallet outputs, so confirmed escrow and
            # child transactions are only queryable through the tx index.
            "-txindex=1",
            f"-rpcuser={RPC_USER}",
            f"-rpcpassword={RPC_PASSWORD}",
        ],
        capture_output=True,
        text=True,
        timeout=DOCKER_TIMEOUT,
    )
    if started.returncode != 0:
        raise RuntimeError(f"{DOCKER_REQUIRED}: could not start {IMAGE}: {started.stderr.strip()}")
    try:
        running = RegtestNode(container=container)
        _wait_for_rpc(running)
        running.rpc("createwallet", WALLET)
        # 101 blocks so the first coinbase is mature and can fund the escrow.
        running.mine(101)
        yield running
    finally:
        subprocess.run(
            ["docker", "rm", "--force", "--volumes", container],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT,
        )


# --------------------------------------------------------------------------- #
# Escrow under test
# --------------------------------------------------------------------------- #

BUYER_ESCROW_SECRET = bytes.fromhex(
    "1111111111111111111111111111111111111111111111111111111111111111"
)
COUNTERPARTY_ESCROW_SECRET = bytes.fromhex(
    "2222222222222222222222222222222222222222222222222222222222222222"
)
BUYER_CLAIM_SECRET = bytes.fromhex(
    "3333333333333333333333333333333333333333333333333333333333333333"
)
PREIMAGE = bytes.fromhex("4444444444444444444444444444444444444444444444444444444444444444")
PAYMENT_HASH = hashlib.sha256(PREIMAGE).digest()
WRONG_PREIMAGE = bytes.fromhex("5555555555555555555555555555555555555555555555555555555555555555")

ESCROW_VALUE = 1_000_000
COUNTERPARTY_VALUE = 400_000
SPLIT_FEE = 1_000
SWEEP_FEE = 1_000
CLAIM_FEE = 1_000
REPLACEMENT_CLAIM_FEE = 8_000


def _pubkey(secret: bytes) -> bytes:
    return bytes(CKey(secret).pub)


def _p2tr(secret: bytes) -> bytes:
    """A destination P2TR script derived from a throwaway key-path-only key."""
    _, output_key = taproot_tweak_pubkey(bytes(CKey(secret).xonly_pub))
    return bytes([0x51, 0x20]) + output_key


BUYER_SPLIT_SCRIPT = _p2tr(b"\x51" * 32)
COUNTERPARTY_SPLIT_SCRIPT = _p2tr(b"\x52" * 32)
BUYER_SWEEP_SCRIPT = _p2tr(b"\x53" * 32)


@pytest.fixture(scope="module")
def escrow() -> BuyoutEscrow:
    return BuyoutEscrow(
        buyer_escrow_pubkey=_pubkey(BUYER_ESCROW_SECRET),
        counterparty_escrow_pubkey=_pubkey(COUNTERPARTY_ESCROW_SECRET),
        buyer_claim_pubkey=_pubkey(BUYER_CLAIM_SECRET),
        payment_hash=PAYMENT_HASH,
    )


def _escrow_address(node: RegtestNode, escrow: BuyoutEscrow) -> str:
    """Let Core itself turn the escrow output key into the address we pay to."""
    descriptor = f"rawtr({escrow.output_script()[2:].hex()})"
    info = node.rpc("getdescriptorinfo", descriptor)
    addresses = node.rpc("deriveaddresses", str(info["descriptor"]))
    address = str(addresses[0])
    derived = node.rpc("validateaddress", address)
    assert derived["scriptPubKey"] == escrow.output_script().hex()
    return address


def _fund_escrow(node: RegtestNode, escrow: BuyoutEscrow) -> tuple[EscrowOutpoint, int]:
    """Pay one fresh escrow output from the local wallet and confirm it.

    Returns the outpoint plus the height its funding transaction confirmed at,
    which is what the BIP68 relative lock is measured from.
    """
    address = _escrow_address(node, escrow)
    amount = f"{Decimal(ESCROW_VALUE) / SATS_PER_BTC:.8f}"
    txid = str(node.rpc("sendtoaddress", address, amount, wallet=True))
    node.mine(1)
    funding = node.rpc("getrawtransaction", txid, "true")
    script = escrow.output_script().hex()
    for output in funding["vout"]:
        if output["scriptPubKey"]["hex"] == script:
            outpoint = EscrowOutpoint(
                txid=txid,
                vout=int(output["n"]),
                value=ESCROW_VALUE,
                scriptpubkey=escrow.output_script(),
            )
            return outpoint, node.height()
    raise AssertionError(f"funding transaction {txid} has no output paying the escrow script")


def _sign_key_path(escrow: BuyoutEscrow, spend: UnsignedKeyPathSpend) -> bytes:
    """Run a real two-party MuSig2 session and return the serialized signed spend."""
    buyer_secnonce, buyer_pubnonce = escrow_nonce(
        escrow.buyer_escrow_pubkey,
        os.urandom(32),
        privkey=BUYER_ESCROW_SECRET,
        sighash=spend.sighash,
    )
    cp_secnonce, cp_pubnonce = escrow_nonce(
        escrow.counterparty_escrow_pubkey,
        os.urandom(32),
        privkey=COUNTERPARTY_ESCROW_SECRET,
        sighash=spend.sighash,
    )
    session = escrow_session(escrow, nonce_agg([buyer_pubnonce, cp_pubnonce]), spend.sighash)
    buyer_partial = sign_partial(buyer_secnonce, BUYER_ESCROW_SECRET, session)
    cp_partial = sign_partial(cp_secnonce, COUNTERPARTY_ESCROW_SECRET, session)
    return finalize_key_path_spend(
        escrow, spend, buyer_pubnonce, cp_pubnonce, buyer_partial, cp_partial
    )


def _replace_witness_item(signed_tx: bytes, index: int, value: bytes) -> bytes:
    """Re-serialize a signed spend with one witness element swapped out."""
    parsed = parse_transaction_bytes(signed_tx)
    witness = list(parsed.witnesses[0])
    witness[index] = value
    tx = TaprootTx(
        inputs=[TxIn(txid=inp.txid, vout=inp.vout, sequence=inp.sequence) for inp in parsed.inputs],
        outputs=[TxOut(value=out.value, scriptpubkey=out.script) for out in parsed.outputs],
        version=parsed.version,
        locktime=parsed.locktime,
        witnesses=[witness],
    )
    return tx.serialize()


def _signed_claim(escrow: BuyoutEscrow, outpoint: EscrowOutpoint, fee_sats: int) -> bytes:
    return build_claim(escrow, outpoint, PREIMAGE, BUYER_CLAIM_SECRET, BUYER_SWEEP_SCRIPT, fee_sats)


# --------------------------------------------------------------------------- #
# Consensus and policy acceptance
# --------------------------------------------------------------------------- #


class TestEscrowSpendsOnRegtest:
    def test_split_is_rejected_one_block_early_and_mined_once_mature(
        self, node: RegtestNode, escrow: BuyoutEscrow
    ) -> None:
        """BIP68 must bite at exactly the agreed block delay, not one block sooner."""
        outpoint, funded_height = _fund_escrow(node, escrow)
        spend = build_split(
            escrow,
            outpoint,
            BUYER_SPLIT_SCRIPT,
            COUNTERPARTY_SPLIT_SCRIPT,
            COUNTERPARTY_VALUE,
            SPLIT_FEE,
            MIN_CSV_DELAY,
        )
        signed = _sign_key_path(escrow, spend)
        assert verify_signed_split(
            escrow,
            outpoint,
            BUYER_SPLIT_SCRIPT,
            COUNTERPARTY_SPLIT_SCRIPT,
            COUNTERPARTY_VALUE,
            SPLIT_FEE,
            MIN_CSV_DELAY,
            signed,
        )

        # Relative locks are evaluated against the next block, so at a tip of
        # funded_height + delay - 2 the input is one block short of eligible.
        node.mine_to_height(funded_height + MIN_CSV_DELAY - 2)
        assert int(node.rpc("getrawtransaction", outpoint.txid, "true")["confirmations"]) == (
            MIN_CSV_DELAY - 1
        )
        error = node.rpc_error("sendrawtransaction", signed.hex())
        assert "non-bip68-final" in error

        node.mine(1)
        txid = node.broadcast(signed)
        assert txid in node.mempool()
        node.mine_and_confirm(txid)

    def test_cooperative_sweep_is_accepted_and_mined(
        self, node: RegtestNode, escrow: BuyoutEscrow
    ) -> None:
        """The post-settlement key-path sweep spends a separate escrow output."""
        outpoint, _ = _fund_escrow(node, escrow)
        spend = build_cooperative_sweep(escrow, outpoint, BUYER_SWEEP_SCRIPT, SWEEP_FEE)
        signed = _sign_key_path(escrow, spend)
        assert verify_signed_cooperative_sweep(
            escrow, outpoint, BUYER_SWEEP_SCRIPT, SWEEP_FEE, signed
        )

        txid = node.broadcast(signed)
        assert txid in node.mempool()
        node.mine_and_confirm(txid)

    def test_claim_is_mined_while_bad_witnesses_are_rejected(
        self, node: RegtestNode, escrow: BuyoutEscrow
    ) -> None:
        """Core must enforce the claim leaf: wrong preimage or signature never relays."""
        outpoint, _ = _fund_escrow(node, escrow)
        signed = _signed_claim(escrow, outpoint, CLAIM_FEE)
        assert verify_signed_claim(escrow, outpoint, BUYER_SWEEP_SCRIPT, CLAIM_FEE, signed)

        wrong_preimage = _replace_witness_item(signed, 1, WRONG_PREIMAGE)
        assert "script-verify-flag-failed" in node.rpc_error(
            "sendrawtransaction", wrong_preimage.hex()
        )

        signature = parse_transaction_bytes(signed).witnesses[0][0]
        corrupted = bytes([signature[0] ^ 0x01]) + signature[1:]
        bad_signature = _replace_witness_item(signed, 0, corrupted)
        assert "script-verify-flag-failed" in node.rpc_error(
            "sendrawtransaction", bad_signature.hex()
        )
        assert node.mempool() == []

        txid = node.broadcast(signed)
        assert txid in node.mempool()
        node.mine_and_confirm(txid)

    def test_unconfirmed_claim_can_be_fee_bumped_by_replacement(
        self, node: RegtestNode, escrow: BuyoutEscrow
    ) -> None:
        """The claim signals BIP125, so it stays bumpable until it confirms."""
        outpoint, _ = _fund_escrow(node, escrow)
        original = _signed_claim(escrow, outpoint, CLAIM_FEE)
        original_txid = node.broadcast(original)
        assert original_txid in node.mempool()

        replacement = _signed_claim(escrow, outpoint, REPLACEMENT_CLAIM_FEE)
        assert verify_signed_claim(
            escrow, outpoint, BUYER_SWEEP_SCRIPT, REPLACEMENT_CLAIM_FEE, replacement
        )
        replacement_txid = node.broadcast(replacement)

        mempool = node.mempool()
        assert replacement_txid in mempool
        assert original_txid not in mempool
        node.mine_and_confirm(replacement_txid)
