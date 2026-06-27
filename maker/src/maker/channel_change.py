"""
Channel-as-change for the maker's CoinJoin output (experimental).

When enabled, the maker emits its CoinJoin change as a Taproot channel-funding
output (a simple-taproot 2-of-2 ``P2TR``) instead of a plain change output, so
the change has no chaining successor in the wallet (paper Sections 4.3/4.7).

For now both funding keys are derived from the maker's own seed (a *self*
2-of-2), so the maker fully controls and can recover the output (cooperatively
spend it via :func:`jmswap.channel.cooperative_close`). This realizes the
on-chain *shape* and its privacy property; the real Lightning dual-funding
negotiation with a peer (and P2TR balance tracking) is the documented follow-up.
Each emitted output is persisted so the funds are always recoverable from the
seed plus the record's key index.

WARNING: the maker's P2WPKH wallet does not yet scan P2TR outputs, so channelled
change will not show in the standard balance until a recovery sweep. This is why
the feature is OFF by default.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jmcore.bitcoin import scriptpubkey_to_address
from jmswap.channel import channel_funding_output

if TYPE_CHECKING:
    from jmwallet.wallet.service import WalletService

# Reserved swap/channel account (matches maker.swap_adapter.SWAP_ACCOUNT); the
# channel-change funding keys live on change branch 4.
SWAP_ACCOUNT = 0x73_77_61_70
CHANNEL_CHANGE_BRANCH = 4


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
