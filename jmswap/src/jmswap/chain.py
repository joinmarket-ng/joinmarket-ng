"""
Chain backend abstraction for the swap protocol.

``jmswap`` watches a swap funding address, broadcasts claim/refund/cooperative
transactions, and reads the current block height. It does not care *how* those
operations are served (Bitcoin Core RPC, a mempool API, a light client), so the
provider/client are written against the small :class:`ChainBackend` interface
and the host application supplies a concrete implementation. An in-memory
:class:`FakeChainBackend` is provided for deterministic tests.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from jmcore.bitcoin import hash256

from jmswap.errors import ChainError


@dataclass(frozen=True)
class AddressUtxo:
    """An unspent output paying a watched address."""

    txid: str
    vout: int
    value: int
    confirmations: int
    scriptpubkey: bytes


class ChainBackend(abc.ABC):
    """Minimal chain access required by the swap protocol."""

    @abc.abstractmethod
    async def block_height(self) -> int:
        """Return the current best block height."""

    @abc.abstractmethod
    async def get_address_utxos(self, address: str) -> list[AddressUtxo]:
        """Return UTXOs paying ``address`` (mempool + confirmed)."""

    @abc.abstractmethod
    async def broadcast(self, tx_hex: str) -> str:
        """Broadcast a raw transaction, returning its txid."""

    @abc.abstractmethod
    async def get_transaction(self, txid: str) -> str | None:
        """Return the raw transaction hex for ``txid`` if known, else None."""

    @abc.abstractmethod
    async def find_spending_tx(self, txid: str, vout: int) -> str | None:
        """Return the raw hex of a known transaction spending ``(txid, vout)``."""

    async def estimate_feerate(self, target_blocks: int = 6) -> float:
        """Return a fee rate estimate in sat/vB (default: a conservative constant)."""
        return 2.0


@dataclass
class _FakeTx:
    raw: str
    confirmations: int


@dataclass
class FakeChainBackend(ChainBackend):
    """Deterministic in-memory chain for tests.

    Addresses are funded explicitly with :meth:`fund_address`; mining with
    :meth:`mine` advances the height and confirms mempool transactions. UTXO
    bookkeeping is intentionally simple: it tracks outputs by ``(txid, vout)``
    and a watched-address -> outpoints index.
    """

    height: int = 100
    feerate: float = 2.0
    _utxos: dict[tuple[str, int], AddressUtxo] = field(default_factory=dict)
    _addr_index: dict[str, set[tuple[str, int]]] = field(default_factory=dict)
    _txs: dict[str, _FakeTx] = field(default_factory=dict)
    _spent: set[tuple[str, int]] = field(default_factory=set)

    async def block_height(self) -> int:
        return self.height

    async def estimate_feerate(self, target_blocks: int = 6) -> float:
        return self.feerate

    def fund_address(
        self, address: str, value: int, scriptpubkey: bytes, *, confirmations: int = 1
    ) -> tuple[str, int]:
        """Create a synthetic funding UTXO paying ``address``. Returns (txid, vout)."""
        txid = hash256(f"{address}:{value}:{len(self._utxos)}".encode()).hex()
        vout = 0
        utxo = AddressUtxo(
            txid=txid,
            vout=vout,
            value=value,
            confirmations=confirmations,
            scriptpubkey=scriptpubkey,
        )
        self._utxos[(txid, vout)] = utxo
        self._addr_index.setdefault(address, set()).add((txid, vout))
        return txid, vout

    def mine(self, blocks: int = 1) -> int:
        self.height += blocks
        for tx in self._txs.values():
            tx.confirmations += blocks
        for outpoint, utxo in list(self._utxos.items()):
            self._utxos[outpoint] = AddressUtxo(
                txid=utxo.txid,
                vout=utxo.vout,
                value=utxo.value,
                confirmations=utxo.confirmations + blocks,
                scriptpubkey=utxo.scriptpubkey,
            )
        return self.height

    async def get_address_utxos(self, address: str) -> list[AddressUtxo]:
        outpoints = self._addr_index.get(address, set())
        return [self._utxos[op] for op in outpoints if op in self._utxos and op not in self._spent]

    async def broadcast(self, tx_hex: str) -> str:
        try:
            raw = bytes.fromhex(tx_hex)
        except ValueError as exc:
            raise ChainError("invalid transaction hex") from exc
        # Compute txid from the non-witness serialization.
        txid = _txid_from_raw(raw)
        if txid in self._txs:
            raise ChainError("transaction already broadcast")
        self._txs[txid] = _FakeTx(raw=tx_hex, confirmations=0)
        return txid

    async def get_transaction(self, txid: str) -> str | None:
        tx = self._txs.get(txid)
        return tx.raw if tx else None

    async def find_spending_tx(self, txid: str, vout: int) -> str | None:
        target = bytes.fromhex(txid)[::-1] + vout.to_bytes(4, "little")
        for tx in self._txs.values():
            for op in _input_outpoints(bytes.fromhex(tx.raw)):
                if op == target:
                    return tx.raw
        return None


def _txid_from_raw(raw: bytes) -> str:
    """Compute a txid from a raw (possibly SegWit) serialization."""
    if len(raw) >= 6 and raw[4] == 0x00 and raw[5] == 0x01:
        # Strip the witness for txid computation.
        version = raw[:4]
        offset = 6
        body_start = offset  # start of the input-count varint
        n_in, offset = _read_varint(raw, offset)
        for _ in range(n_in):
            offset += 36  # outpoint
            sl, offset = _read_varint(raw, offset)
            offset += sl
            offset += 4  # sequence
        n_out, offset = _read_varint(raw, offset)
        for _ in range(n_out):
            offset += 8
            sl, offset = _read_varint(raw, offset)
            offset += sl
        inputs_outputs = raw[body_start:offset]
        # Skip the witness stacks to reach the locktime.
        for _ in range(n_in):
            n_items, offset = _read_varint(raw, offset)
            for _ in range(n_items):
                il, offset = _read_varint(raw, offset)
                offset += il
        locktime = raw[offset : offset + 4]
        stripped = version + inputs_outputs + locktime
        return hash256(stripped)[::-1].hex()
    return hash256(raw)[::-1].hex()


def _input_outpoints(raw: bytes) -> list[bytes]:
    """Return the 36-byte outpoints referenced by a tx's inputs."""
    offset = 4
    if len(raw) >= 6 and raw[4] == 0x00 and raw[5] == 0x01:
        offset = 6
    n_in, offset = _read_varint(raw, offset)
    outpoints: list[bytes] = []
    for _ in range(n_in):
        outpoints.append(raw[offset : offset + 36])
        offset += 36
        sl, offset = _read_varint(raw, offset)
        offset += sl + 4
    return outpoints


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    n = data[offset]
    offset += 1
    if n < 0xFD:
        return n, offset
    if n == 0xFD:
        return int.from_bytes(data[offset : offset + 2], "little"), offset + 2
    if n == 0xFE:
        return int.from_bytes(data[offset : offset + 4], "little"), offset + 4
    return int.from_bytes(data[offset : offset + 8], "little"), offset + 8
