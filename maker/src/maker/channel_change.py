"""
Channel-as-change for the maker's CoinJoin output (experimental).

When enabled, the maker emits its CoinJoin change as a Taproot channel-funding
output (a simple-taproot 2-of-2 ``P2TR``) instead of a plain change output, so
the change has no chaining successor in the wallet (paper Sections 4.3/4.7).

For now both funding keys are derived from the maker's own seed (a *self*
2-of-2), so the maker fully controls the output: it is tracked on-chain
(:func:`scan_channel_change_utxos`, :func:`channel_change_balance`) and can be
cooperatively swept back into the wallet (:func:`sweep_channel_change`, an
ordinary P2TR key-path spend), with each emitted output persisted so the funds
are always recoverable from the seed plus the record's key index.

Opening a *real Lightning channel* whose funding output is this CoinJoin output
is supported via a forked LDK Server (see ``ldk_server/patches/``) and the
``jmswap.channel_funding`` helpers; the maker uses the channel's funding address
as its CoinJoin change output and then provides the resulting outpoint. The
feature stays OFF by default until the maker-to-maker negotiation of who opens
the channel (and the in-CoinJoin coordination of the channel amount) lands.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jmcore.bitcoin import address_to_scriptpubkey, scriptpubkey_to_address
from jmcore.taproot import TaprootTx, TxOut
from jmswap.channel import channel_funding_output, cooperative_close
from jmswap.spend import COOPERATIVE_TX_VBYTES, SwapOutpoint, estimate_fee
from loguru import logger

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend
    from jmwallet.wallet.service import WalletService

# Reserved swap/channel account (matches maker.swap_adapter.SWAP_ACCOUNT); the
# channel-change funding keys live on change branch 4.
SWAP_ACCOUNT = 0x73_77_61_70
CHANNEL_CHANGE_BRANCH = 4
# Maker-to-maker combine funding keys (a single key per round, contributed to the
# shared 2-of-2 with a fellow maker) live on branch 5.
COMBINE_FUNDING_BRANCH = 5


def derive_combine_funding_key(wallet: WalletService, index: int) -> tuple[bytes, bytes, str]:
    """Derive a recoverable funding keypair for maker-to-maker channel-as-change.

    Returns ``(private_key_bytes, public_key_bytes, path)``. The maker contributes
    the public key to a shared 2-of-2 with a fellow CoinJoin maker (the taker pairs
    them and relays pubkeys), and keeps the private key to later cooperatively spend
    the pooled funding output. Deterministic so it survives a restart.
    """
    coin = _coin_type(wallet.network)
    path = f"m/84'/{coin}'/{SWAP_ACCOUNT}'/{COMBINE_FUNDING_BRANCH}/{index}"
    key = wallet.master_key.derive(path)
    return (
        key.get_private_key_bytes(),
        key.get_public_key_bytes(compressed=True),
        path,
    )


@dataclass(frozen=True)
class ChannelChangeRecord:
    """A persisted record of an emitted channel-change funding output."""

    index: int
    address: str
    amount: int
    our_key_a_path: str
    our_key_b_path: str
    network: str


class ChannelChangeStore:
    """Append-only JSONL store of emitted channel-change records under a dir."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "channel_changes.jsonl"

    def next_index(self) -> int:
        if not self._path.exists():
            return 0
        with self._path.open() as fh:
            return sum(1 for line in fh if line.strip())

    def append(self, record: ChannelChangeRecord) -> None:
        line = json.dumps(asdict(record))
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".cc-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(line + "\n")
            with self._path.open("a") as out, open(tmp) as src:
                out.write(src.read())
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    def list(self) -> list[ChannelChangeRecord]:
        if not self._path.exists():
            return []
        records: list[ChannelChangeRecord] = []
        with self._path.open() as fh:
            for line in fh:
                if line.strip():
                    records.append(ChannelChangeRecord(**json.loads(line)))
        return records


def _coin_type(network: str) -> int:
    return 0 if network == "mainnet" else 1


def derive_channel_change_address(
    wallet: WalletService, index: int
) -> tuple[str, ChannelChangeRecord]:
    """Derive a recoverable self-2-of-2 channel-funding change address.

    Returns ``(address, record)``; both funding keys are derived from the wallet
    seed so the maker can later cooperatively spend the output.
    """
    coin = _coin_type(wallet.network)
    path_a = f"m/84'/{coin}'/{SWAP_ACCOUNT}'/{CHANNEL_CHANGE_BRANCH}/{2 * index}"
    path_b = f"m/84'/{coin}'/{SWAP_ACCOUNT}'/{CHANNEL_CHANGE_BRANCH}/{2 * index + 1}"
    key_a = wallet.master_key.derive(path_a)
    key_b = wallet.master_key.derive(path_b)
    spk, _internal = channel_funding_output(
        key_a.get_public_key_bytes(compressed=True),
        key_b.get_public_key_bytes(compressed=True),
    )
    address = scriptpubkey_to_address(spk, wallet.network)
    record = ChannelChangeRecord(
        index=index,
        address=address,
        amount=0,
        our_key_a_path=path_a,
        our_key_b_path=path_b,
        network=wallet.network,
    )
    return address, record


def emit_channel_change_address(wallet: WalletService, store: ChannelChangeStore) -> str:
    """Derive the next channel-change address, persist its record, and return it."""
    index = store.next_index()
    address, record = derive_channel_change_address(wallet, index)
    store.append(record)
    return address


# --- P2TR channel-change tracking and cooperative sweep ---------------------


@dataclass(frozen=True)
class ChannelChangeUtxo:
    """A confirmed channel-change funding output found on-chain."""

    record: ChannelChangeRecord
    txid: str
    vout: int
    value: int


def build_channel_change_sweep(
    wallet: WalletService,
    record: ChannelChangeRecord,
    txid: str,
    vout: int,
    value: int,
    destination: str,
    feerate_sat_vb: float = 2.0,
) -> TaprootTx:
    """Build a signed MuSig2 cooperative-close sweep of a channel-change output.

    The maker holds both funding keys (self 2-of-2), so it can sweep the output
    back into its wallet with an ordinary P2TR key-path spend.
    """
    key_a = wallet.master_key.derive(record.our_key_a_path)
    key_b = wallet.master_key.derive(record.our_key_b_path)
    spk, _ = channel_funding_output(
        key_a.get_public_key_bytes(compressed=True),
        key_b.get_public_key_bytes(compressed=True),
    )
    outpoint = SwapOutpoint(txid=txid, vout=vout, value=value, scriptpubkey=spk)
    fee = estimate_fee(COOPERATIVE_TX_VBYTES, feerate_sat_vb)
    dest_spk = address_to_scriptpubkey(destination)
    return cooperative_close(
        key_a.get_public_key_bytes(compressed=True),
        key_b.get_public_key_bytes(compressed=True),
        outpoint,
        [TxOut(value=value - fee, scriptpubkey=dest_spk)],
        key_a.get_private_key_bytes(),
        key_b.get_private_key_bytes(),
    )


async def _scantxoutset_utxos(
    backend: BlockchainBackend, address: str
) -> list[tuple[str, int, int]]:
    """Return ``(txid, vout, value_sats)`` for an arbitrary address via the Core RPC."""
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

    if not isinstance(backend, DescriptorWalletBackend):
        raise TypeError("channel-change scanning requires the descriptor (Core) backend")
    result = await backend._rpc_call(
        "scantxoutset", ["start", [{"desc": f"addr({address})"}]], use_wallet=False
    )
    return [
        (u["txid"], int(u["vout"]), int(round(float(u["amount"]) * 1e8)))
        for u in result.get("unspents", [])
    ]


async def scan_channel_change_utxos(
    backend: BlockchainBackend, store: ChannelChangeStore
) -> list[ChannelChangeUtxo]:
    """Scan the chain for unspent channel-change outputs from persisted records."""
    found: list[ChannelChangeUtxo] = []
    for record in store.list():
        for txid, vout, value in await _scantxoutset_utxos(backend, record.address):
            found.append(ChannelChangeUtxo(record=record, txid=txid, vout=vout, value=value))
    return found


async def channel_change_balance(backend: BlockchainBackend, store: ChannelChangeStore) -> int:
    """Total spendable value (sats) currently held in channel-change outputs."""
    return sum(u.value for u in await scan_channel_change_utxos(backend, store))


async def sweep_channel_change(
    wallet: WalletService,
    backend: BlockchainBackend,
    store: ChannelChangeStore,
    destination: str,
    feerate_sat_vb: float = 2.0,
) -> list[str]:
    """Sweep all unspent channel-change outputs to ``destination``. Returns txids."""
    txids: list[str] = []
    for utxo in await scan_channel_change_utxos(backend, store):
        tx = build_channel_change_sweep(
            wallet, utxo.record, utxo.txid, utxo.vout, utxo.value, destination, feerate_sat_vb
        )
        txid = await backend.broadcast_transaction(tx.serialize().hex())
        logger.info(
            "swept channel-change {}:{} -> {} via {}", utxo.txid, utxo.vout, destination, txid
        )
        txids.append(txid)
    return txids
