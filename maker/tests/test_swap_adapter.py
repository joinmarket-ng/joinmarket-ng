"""Tests for the jmswap <-> JoinMarket wallet/backend adapters and bot wiring."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from jmwallet.backends.base import UTXO
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.service import WalletService

from maker.config import MakerConfig, SwapProviderConfig
from maker.swap_adapter import WalletChainBackend, WalletSwapWallet

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)


@pytest.fixture
def wallet() -> WalletService:
    backend = DescriptorWalletBackend(wallet_name="swap_test")
    backend._wallet_loaded = True
    backend._descriptors_imported = True
    return WalletService(
        mnemonic=TEST_MNEMONIC, backend=backend, network="regtest", mixdepth_count=5
    )


async def test_new_swap_keypair_deterministic_and_distinct(wallet: WalletService) -> None:
    sw = WalletSwapWallet(wallet)
    priv1, pub1 = await sw.new_swap_keypair()
    priv2, pub2 = await sw.new_swap_keypair()
    assert len(priv1) == 32 and len(pub1) == 33 and pub1[0] in (0x02, 0x03)
    assert priv1 != priv2 and pub1 != pub2
    # deterministic: a fresh adapter regenerates the same first key
    sw2 = WalletSwapWallet(wallet)
    priv1b, pub1b = await sw2.new_swap_keypair()
    assert (priv1b, pub1b) == (priv1, pub1)


class _StubBackend:
    """Minimal async backend stub for WalletChainBackend mapping tests."""

    def __init__(self) -> None:
        self.broadcasted: str | None = None

    async def get_block_height(self) -> int:
        return 777

    async def get_utxos(self, addresses: list[str]) -> list[UTXO]:
        return [
            UTXO(
                txid="ab" * 32,
                vout=1,
                value=12345,
                address=addresses[0],
                confirmations=3,
                scriptpubkey="0014" + "11" * 20,
            )
        ]

    async def broadcast_transaction(self, tx_hex: str) -> str:
        self.broadcasted = tx_hex
        return "cd" * 32

    async def get_transaction(self, txid: str):
        return SimpleNamespace(raw="deadbeef") if txid == "cd" * 32 else None


async def test_chain_backend_mapping() -> None:
    backend = _StubBackend()
    chain = WalletChainBackend(backend)  # type: ignore[arg-type]
    assert await chain.block_height() == 777
    utxos = await chain.get_address_utxos("bcrt1qexample")
    assert len(utxos) == 1 and utxos[0].value == 12345
    assert utxos[0].scriptpubkey == bytes.fromhex("0014" + "11" * 20)
    assert await chain.broadcast("00aa") == "cd" * 32
    assert backend.broadcasted == "00aa"
    assert await chain.get_transaction("cd" * 32) == "deadbeef"
    assert await chain.get_transaction("ff" * 32) is None


async def test_find_spending_tx_requires_descriptor_backend() -> None:
    from jmswap.errors import ChainError

    chain = WalletChainBackend(_StubBackend())  # type: ignore[arg-type]
    with pytest.raises(ChainError):
        await chain.find_spending_tx("ab" * 32, 0)


def test_build_swap_server_wires_terms(wallet: WalletService) -> None:
    """MakerBot._build_swap_server builds a working server from the config."""
    from maker.bot import MakerBot

    config = MakerConfig(
        mnemonic=TEST_MNEMONIC,
        network="regtest",
        swap_provider=SwapProviderConfig(
            enabled=True,
            directions=["swapin", "swapout"],
            min_amount=40_000,
            max_amount=2_000_000,
            percentage_fee="0.2",
            base_fee=400,
            accepts_whole_coin=True,
            nostr_relays=["ws://localhost:7000"],
        ),
    )
    bot = SimpleNamespace(config=config, wallet=wallet, backend=wallet.backend)
    server, nostr_node, identity = MakerBot._build_swap_server(bot)  # type: ignore[arg-type]
    assert server.terms.network == "regtest"
    assert server.terms.min_amount == 40_000
    assert server.terms.accepts_whole_coin is True
    assert len(identity) == 66
    assert nostr_node.pubkey  # derived Nostr identity
