"""Resolve one wallet's managed Lightning nodes from settings and its seed."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bitcointx.core.key import CExtKey
from jmcore.channel_ring import ChannelRingConfig, ChannelRingSettings
from jmcore.lightning_keys import WalletLightningKeys
from loguru import logger

from jmswap.channel_ring_nodes import channel_ring_wallet_identity
from jmswap.lnd_config import LndEnvironment
from jmswap.lnd_release import default_lnd_binary
from jmswap.managed_nodes import ManagedNodeError, ManagedNodePaths, RegistryStore, ring_nodes


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


def wallet_identity_of(seed: bytes) -> str:
    """The ring wallet identity (hash of the BIP32 master public key)."""
    return channel_ring_wallet_identity(bytes(CExtKey.from_seed(seed).pub))


def lightning_context(settings: Any, seed: bytes, *, with_keys: bool = True) -> LightningContext:
    network = settings_network(settings)
    wallet_identity = wallet_identity_of(seed)
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


def channel_ring_config(
    lightning_mode: str,
    ring_settings: ChannelRingSettings,
    *,
    mnemonic: str,
    passphrase: str,
    data_dir: Path,
    network: str,
    reserve_sats: tuple[int, int] | None = None,
) -> ChannelRingConfig:
    """The runtime ring config, wired to the wallet's managed nodes in managed mode.

    ``reserve_sats`` is ``[lightning]`` (reserve_min_sats, reserve_max_sats);
    only managed nodes get their reserves funded by CoinJoin outputs.

    Other modes use the explicit ``[maker/taker.channel_ring]`` settings as they
    are. Managed mode owns the node list: explicit nodes are refused rather than
    merged, and a missing registry leaves rings disabled. Errors are ValueErrors,
    like every other config builder failure.
    """
    config = ChannelRingConfig.from_settings(ring_settings)
    if lightning_mode != "managed":
        return config
    if ring_settings.nodes or ring_settings.mixdepth_nodes or ring_settings.node_binding_directory:
        raise ValueError(
            "lightning.mode = 'managed' configures the channel ring nodes itself; remove "
            "nodes, mixdepth_nodes and node_binding_directory from the channel_ring "
            "section, or use lightning.mode = 'manual'"
        )
    from jmcore.crypto import mnemonic_to_seed

    seed = mnemonic_to_seed(mnemonic, passphrase)
    paths = ManagedNodePaths.for_wallet(data_dir, network, wallet_identity_of(seed))
    try:
        registry = RegistryStore(paths).load()
    except ManagedNodeError as exc:
        raise ValueError(str(exc)) from exc
    if registry is None:
        logger.warning("No managed Lightning nodes yet; run `jm-lightning init` to enable rings")
        return config
    nodes, mapping = ring_nodes(registry, paths)
    explicit_enabled = "enabled" in ring_settings.model_fields_set
    return ChannelRingConfig.model_validate(
        {
            **config.model_dump(),
            "enabled": ring_settings.enabled if explicit_enabled else bool(mapping),
            "nodes": {name: node.model_dump() for name, node in nodes.items()},
            "mixdepth_nodes": mapping,
            "node_binding_directory": paths.bindings_dir,
            "reserve_funding": reserve_sats if reserve_sats and reserve_sats[0] > 0 else None,
        }
    )
