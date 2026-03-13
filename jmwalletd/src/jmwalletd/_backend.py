"""Blockchain backend factory.

Provides a single ``get_backend()`` coroutine that creates and caches the
blockchain backend (Bitcoin Core RPC, descriptor wallet, etc.) based on the
application configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

_backend_instance: Any = None


async def get_backend(data_dir: Path, force_new: bool = False) -> Any:
    """Return a shared blockchain backend instance.

    Creates the backend on first call based on the JoinMarket settings,
    then caches it for subsequent calls.

    Args:
        data_dir: Path to data directory (unused by current backends but kept for compat)
        force_new: If True, create and return a new instance (do not cache it).
                   Useful for tasks like Maker/Taker that close the backend on exit.
    """
    global _backend_instance

    if _backend_instance is not None and not force_new:
        return _backend_instance

    from jmcore.settings import get_settings

    settings = get_settings()
    backend_type = settings.bitcoin.backend_type

    logger.info(
        f"Initializing blockchain backend: {backend_type}"
        + (" (new instance)" if force_new else "")
    )

    rpc_url = settings.bitcoin.rpc_url
    rpc_user = settings.bitcoin.rpc_user
    # SecretStr -> str for backend constructors.
    raw_password = settings.bitcoin.rpc_password
    rpc_password: str = (
        raw_password.get_secret_value()
        if hasattr(raw_password, "get_secret_value")
        else str(raw_password)
    )

    instance = None

    if backend_type == "descriptor_wallet":
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

        instance = DescriptorWalletBackend(
            rpc_url=rpc_url,
            rpc_user=rpc_user,
            rpc_password=rpc_password,
            wallet_name=settings.bitcoin.descriptor_wallet_name,
        )
    elif backend_type == "scantxoutset":
        from jmwallet.backends.bitcoin_core import BitcoinCoreBackend

        instance = BitcoinCoreBackend(
            rpc_url=rpc_url,
            rpc_user=rpc_user,
            rpc_password=rpc_password,
        )
    elif backend_type == "neutrino":
        from jmwallet.backends.neutrino import NeutrinoBackend

        neutrino_url = getattr(settings.bitcoin, "neutrino_url", rpc_url)
        network = settings.network_config.network.value

        instance = NeutrinoBackend(
            neutrino_url=neutrino_url,
            network=network,
            scan_start_height=settings.wallet.scan_start_height,
            connect_peers=settings.get_neutrino_connect_peers(),
        )
    else:
        msg = f"Unknown backend type: {backend_type}"
        raise ValueError(msg)

    if force_new:
        return instance

    _backend_instance = instance
    return _backend_instance


def reset_backend() -> None:
    """Reset the cached backend (for testing)."""
    global _backend_instance
    _backend_instance = None
