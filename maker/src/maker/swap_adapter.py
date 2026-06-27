"""
Adapters binding ``jmswap`` to the maker's JoinMarket wallet and backend.

``jmswap`` is written against small backend interfaces so it stays standalone;
this module supplies the concrete JoinMarket implementations:

* :class:`WalletChainBackend` - a :class:`jmswap.ChainBackend` over the maker's
  :class:`jmwallet.backends.base.BlockchainBackend`.
* :class:`WalletSwapWallet` - a :class:`jmswap.SwapWallet` over the maker's
  :class:`jmwallet.wallet.service.WalletService`.

Swap keys are derived deterministically from the wallet seed on a dedicated
BIP32 branch (``m/84'/coin'/SWAP_ACCOUNT'/0/index``) so they are recoverable
after a restart and isolated from ordinary wallet addresses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jmswap.chain import AddressUtxo, ChainBackend
from jmswap.errors import ChainError
from jmswap.spend import SwapOutpoint
from jmswap.wallet import SwapWallet
from loguru import logger

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend
    from jmwallet.wallet.service import WalletService

# A high, hardened account index reserved for swap keys (kept clear of the
# maker's mixdepth accounts, which are small indices).
SWAP_ACCOUNT = 0x73_77_61_70  # "swap"


class WalletChainBackend(ChainBackend):
    """A :class:`jmswap.ChainBackend` backed by the maker's blockchain backend."""

    def __init__(self, backend: BlockchainBackend) -> None:
        self._backend = backend

    async def block_height(self) -> int:
        return await self._backend.get_block_height()

    async def get_address_utxos(self, address: str) -> list[AddressUtxo]:
        utxos = await self._backend.get_utxos([address])
        return [
            AddressUtxo(
                txid=u.txid,
                vout=u.vout,
                value=u.value,
                confirmations=u.confirmations,
                scriptpubkey=bytes.fromhex(u.scriptpubkey) if u.scriptpubkey else b"",
            )
            for u in utxos
            if u.address == address
        ]

    async def broadcast(self, tx_hex: str) -> str:
        try:
            return await self._backend.broadcast_transaction(tx_hex)
        except Exception as exc:  # noqa: BLE001 - normalise to a swap error
            raise ChainError(f"broadcast failed: {exc}") from exc

    async def get_transaction(self, txid: str) -> str | None:
        tx = await self._backend.get_transaction(txid)
        return tx.raw if tx is not None else None

    async def estimate_feerate(self, target_blocks: int = 6) -> float:
        try:
            return await self._backend.estimate_fee(target_blocks)
        except Exception:  # noqa: BLE001 - fall back to a safe constant
            return 2.0

    async def find_spending_tx(self, txid: str, vout: int) -> str | None:
        """Find a transaction spending ``(txid, vout)`` (mempool + recent blocks).

        Uses the Bitcoin Core descriptor backend's RPC where available; other
        backends (e.g. Neutrino) raise :class:`ChainError` since they cannot
        answer outpoint-spend queries without extra indexing.
        """
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

        if not isinstance(self._backend, DescriptorWalletBackend):
            raise ChainError("find_spending_tx requires the descriptor (Core) backend")

        target = (txid, vout)
        # 1) mempool
        mempool = await self._backend._rpc_call("getrawmempool", [True], use_wallet=False)
        for mp_txid in mempool:
            raw = await self._spending_match(mp_txid, target)
            if raw is not None:
                return raw
        # 2) recent confirmed blocks (bounded scan back from the tip)
        tip = await self._backend.get_block_height()
        for height in range(tip, max(tip - 144, 0) - 1, -1):
            block_hash = await self._backend.get_block_hash(height)
            block = await self._backend._rpc_call("getblock", [block_hash, 2], use_wallet=False)
            for tx in block.get("tx", []):
                for vin in tx.get("vin", []):
                    if vin.get("txid") == txid and vin.get("vout") == vout:
                        return str(tx["hex"])
        return None

    async def _spending_match(self, candidate_txid: str, target: tuple[str, int]) -> str | None:
        tx = await self._backend.get_transaction(candidate_txid)
        if tx is None:
            return None
        from jmswap.chain import _input_outpoints

        wanted = bytes.fromhex(target[0])[::-1] + target[1].to_bytes(4, "little")
        if wanted in _input_outpoints(bytes.fromhex(tx.raw)):
            return tx.raw
        return None


class WalletSwapWallet(SwapWallet):
    """A :class:`jmswap.SwapWallet` backed by the maker's wallet."""

    def __init__(self, wallet: WalletService, *, funding_mixdepth: int | None = None) -> None:
        self._wallet = wallet
        self.network = wallet.network
        self._funding_mixdepth = funding_mixdepth
        self._key_index = 0

    def _coin_type(self) -> int:
        return 0 if self.network == "mainnet" else 1

    async def new_swap_keypair(self) -> tuple[bytes, bytes]:
        index = self._key_index
        self._key_index += 1
        path = f"m/84'/{self._coin_type()}'/{SWAP_ACCOUNT}'/0/{index}"
        key = self._wallet.master_key.derive(path)
        return key.get_private_key_bytes(), key.get_public_key_bytes(compressed=True)

    async def fund_swap(self, address: str, amount: int) -> SwapOutpoint:
        from jmwallet.wallet.spend import direct_send

        mixdepth = self._funding_mixdepth
        if mixdepth is None:
            mixdepth = await self._max_balance_mixdepth()
        result = await direct_send(
            wallet=self._wallet,
            backend=self._wallet.backend,
            mixdepth=mixdepth,
            amount_sats=amount,
            destination=address,
        )
        vout = self._find_destination_vout(result, address)
        from jmcore.bitcoin import address_to_scriptpubkey

        spk = address_to_scriptpubkey(address)
        logger.info("funded swap output {}:{} ({} sats) for {}", result.txid, vout, amount, address)
        return SwapOutpoint(txid=result.txid, vout=vout, value=amount, scriptpubkey=spk)

    async def destination_scriptpubkey(self) -> bytes:
        from jmcore.bitcoin import address_to_scriptpubkey

        mixdepth = self._funding_mixdepth
        if mixdepth is None:
            mixdepth = await self._max_balance_mixdepth()
        address = self._wallet.get_new_address(mixdepth)
        return address_to_scriptpubkey(address)

    async def _max_balance_mixdepth(self) -> int:
        balances = {
            md: await self._wallet.get_balance(md) for md in range(self._wallet.mixdepth_count)
        }
        return max(balances, key=lambda md: balances[md])

    @staticmethod
    def _find_destination_vout(result: object, address: str) -> int:
        for i, output in enumerate(getattr(result, "outputs", []) or []):
            if isinstance(output, dict):
                out_addr = output.get("address")
            else:
                out_addr = getattr(output, "address", None)
            if out_addr == address:
                return i
        return 0
