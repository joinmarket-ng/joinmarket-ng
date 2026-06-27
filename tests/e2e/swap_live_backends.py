"""bitcoind-backed jmswap ChainBackend / SwapWallet for the live swap e2e test."""

from __future__ import annotations

from typing import Any

from coincurve import PrivateKey
from jmcore.bitcoin import address_to_scriptpubkey
from jmswap.chain import AddressUtxo, ChainBackend
from jmswap.spend import SwapOutpoint
from jmswap.wallet import SwapWallet

MINER_WALLET = "miner"


def _btc_to_sats(value: float) -> int:
    return int(round(value * 1e8))


class BitcoindChain(ChainBackend):
    """A jmswap ChainBackend over a regtest bitcoind (via the test RPC client)."""

    def __init__(self, rpc: Any) -> None:
        self._rpc = rpc

    async def block_height(self) -> int:
        return int(self._rpc.call("getblockcount"))

    async def get_address_utxos(self, address: str) -> list[AddressUtxo]:
        result = self._rpc.call(
            "scantxoutset", ["start", [{"desc": f"addr({address})"}]]
        )
        tip = int(result.get("height", 0))
        utxos: list[AddressUtxo] = []
        for u in result.get("unspents", []):
            height = int(u.get("height", tip))
            utxos.append(
                AddressUtxo(
                    txid=u["txid"],
                    vout=int(u["vout"]),
                    value=_btc_to_sats(float(u["amount"])),
                    confirmations=max(tip - height + 1, 0),
                    scriptpubkey=bytes.fromhex(u["scriptPubKey"]),
                )
            )
        return utxos

    async def broadcast(self, tx_hex: str) -> str:
        return str(self._rpc.call("sendrawtransaction", [tx_hex]))

    async def get_transaction(self, txid: str) -> str | None:
        try:
            return str(self._rpc.call("getrawtransaction", [txid]))
        except RuntimeError:
            return None

    async def find_spending_tx(self, txid: str, vout: int) -> str | None:
        # mempool
        for mp in self._rpc.call("getrawmempool"):
            tx = self._rpc.call("getrawtransaction", [mp, True])
            if any(
                v.get("txid") == txid and v.get("vout") == vout
                for v in tx.get("vin", [])
            ):
                return str(tx["hex"])
        # recent blocks
        tip = int(self._rpc.call("getblockcount"))
        for height in range(tip, max(tip - 20, 0) - 1, -1):
            block_hash = self._rpc.call("getblockhash", [height])
            block = self._rpc.call("getblock", [block_hash, 2])
            for tx in block.get("tx", []):
                if any(
                    v.get("txid") == txid and v.get("vout") == vout
                    for v in tx.get("vin", [])
                ):
                    return str(tx["hex"])
        return None

    async def estimate_feerate(self, target_blocks: int = 6) -> float:
        return 2.0  # regtest has no fee market


class BitcoindWallet(SwapWallet):
    """A jmswap SwapWallet over bitcoind's miner wallet (funds + sweeps)."""

    network = "regtest"

    def __init__(self, rpc: Any) -> None:
        self._rpc = rpc

    async def new_swap_keypair(self) -> tuple[bytes, bytes]:
        priv = PrivateKey()
        return priv.secret, priv.public_key.format(compressed=True)

    async def fund_swap(self, address: str, amount: int) -> SwapOutpoint:
        txid = self._rpc.call(
            "sendtoaddress", [address, amount / 1e8], wallet=MINER_WALLET
        )
        self._rpc.mine(1)
        tx = self._rpc.call("getrawtransaction", [txid, True])
        vout = 0
        for out in tx.get("vout", []):
            spk = out.get("scriptPubKey", {})
            if spk.get("address") == address or address in spk.get("addresses", []):
                vout = int(out["n"])
                break
        return SwapOutpoint(
            txid=txid,
            vout=vout,
            value=amount,
            scriptpubkey=address_to_scriptpubkey(address),
        )

    async def destination_scriptpubkey(self) -> bytes:
        address = self._rpc.call("getnewaddress", ["", "bech32"], wallet=MINER_WALLET)
        return address_to_scriptpubkey(address)
