"""Resolve one wallet's managed Lightning nodes from settings and its seed."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bitcointx.core.key import CExtKey
from jmcore.lightning_keys import WalletLightningKeys

from jmswap.channel_ring_nodes import channel_ring_wallet_identity
from jmswap.lnd_config import LndEnvironment
from jmswap.lnd_release import default_lnd_binary
from jmswap.managed_nodes import ManagedNodePaths, RegistryStore


@dataclass
class LightningContext:
    """Everything the managed node commands need for one wallet and network."""

    settings: Any
    network: str
    wallet_identity: str
    paths: ManagedNodePaths
    store: RegistryStore
    env: LndEnvironment
    keys: WalletLightningKeys | None = field(default=None, repr=False)

    @property
    def mixdepths(self) -> range:
        return range(int(self.settings.wallet.mixdepth_count))


def settings_network(settings: Any) -> str:
    network_config = settings.network_config
    network = network_config.bitcoin_network or network_config.network
    return str(getattr(network, "value", network))


def lightning_context(settings: Any, seed: bytes, *, with_keys: bool = True) -> LightningContext:
    network = settings_network(settings)
    wallet_identity = channel_ring_wallet_identity(bytes(CExtKey.from_seed(seed).pub))
    data_dir = Path(settings.get_data_dir())
    lnd_binary = (
        Path(settings.lightning.lnd_binary).expanduser()
        if settings.lightning.lnd_binary
        else default_lnd_binary(data_dir)
    )
    paths = ManagedNodePaths.for_wallet(data_dir, network, wallet_identity)
    return LightningContext(
        settings=settings,
        network=network,
        wallet_identity=wallet_identity,
        paths=paths,
        store=RegistryStore(paths),
        env=LndEnvironment.from_settings(settings, network=network, lnd_binary=lnd_binary),
        keys=WalletLightningKeys(seed, network) if with_keys else None,
    )
