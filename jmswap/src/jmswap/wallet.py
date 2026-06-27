"""
Wallet abstraction for the swap protocol.

A swap peer needs to: derive a per-swap key (the MuSig2 share and the
claim/refund signing key), fund a swap address (the provider on a swap-out, the
client on a swap-in), and supply a destination scriptPubKey to sweep claimed or
refunded coins into. These are abstracted behind :class:`SwapWallet` so the
host wallet (e.g. JoinMarket's ``jmwallet``) plugs in, while a
:class:`FakeSwapWallet` backed by :class:`~jmswap.chain.FakeChainBackend` drives
in-process tests.
"""

from __future__ import annotations

import abc
import secrets

from coincurve import PrivateKey
from jmcore.bitcoin import address_to_scriptpubkey, create_p2tr_scriptpubkey

from jmswap.chain import ChainBackend, FakeChainBackend
from jmswap.spend import SwapOutpoint


class SwapWallet(abc.ABC):
    """On-chain wallet operations a swap peer needs."""

    network: str = "mainnet"

    @abc.abstractmethod
    async def new_swap_keypair(self) -> tuple[bytes, bytes]:
        """Return a fresh ``(privkey, compressed_pubkey)`` for a swap."""

    @abc.abstractmethod
    async def fund_swap(self, address: str, amount: int) -> SwapOutpoint:
        """Pay ``amount`` sats to the swap ``address`` and return its outpoint."""

    @abc.abstractmethod
    async def destination_scriptpubkey(self) -> bytes:
        """Return a fresh scriptPubKey to receive claimed/refunded coins."""


class FakeSwapWallet(SwapWallet):
    """An in-memory wallet over a :class:`FakeChainBackend` for tests."""

    def __init__(self, chain: ChainBackend, network: str = "regtest") -> None:
        self._chain = chain
        self.network = network

    async def new_swap_keypair(self) -> tuple[bytes, bytes]:
        priv = PrivateKey()
        return priv.secret, priv.public_key.format(compressed=True)

    async def fund_swap(self, address: str, amount: int) -> SwapOutpoint:
        if not isinstance(self._chain, FakeChainBackend):
            raise TypeError("FakeSwapWallet requires a FakeChainBackend")
        spk = address_to_scriptpubkey(address)
        txid, vout = self._chain.fund_address(address, amount, spk, confirmations=1)
        return SwapOutpoint(txid=txid, vout=vout, value=amount, scriptpubkey=spk)

    async def destination_scriptpubkey(self) -> bytes:
        return create_p2tr_scriptpubkey(secrets.token_bytes(32))
