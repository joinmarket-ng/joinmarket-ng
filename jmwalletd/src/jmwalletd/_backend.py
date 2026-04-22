"""Blockchain backend factory.

Provides a ``get_backend()`` coroutine that creates and caches blockchain
backend instances (Bitcoin Core RPC, descriptor wallet, Neutrino, etc.)
based on application configuration.

Descriptor wallets are cached **per JoinMarket wallet** rather than as a
single global instance.  Bitcoin Core descriptor wallets carry per-wallet
import state (imported xpubs, rescan cursor); sharing a single bitcoind
descriptor wallet across multiple JM wallets means the second JM wallet's
descriptors are never imported, so ``listunspent`` returns no UTXOs for
it.  The cache key is the deterministic wallet name derived from the JM
wallet's BIP32 m/0 fingerprint (see
:func:`jmwallet.backends.descriptor_wallet.generate_wallet_name`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from jmwallet.wallet.service import WalletService

# Cache keyed by bitcoind descriptor wallet name.  A None-keyed entry is
# used for non-descriptor backends (Neutrino, scantxoutset) which do not
# depend on a per-wallet bitcoind descriptor wallet at all.
_backend_cache: dict[str | None, Any] = {}


def wallet_name_for_service(wallet_service: WalletService) -> str:
    """Return the bitcoind descriptor wallet name for a ``WalletService``.

    Mirrors :func:`jmwallet.backends.descriptor_wallet.generate_wallet_name`
    but derives the fingerprint from the already-initialised master key on
    ``wallet_service`` so callers that do not hold the raw mnemonic (e.g.
    helpers that only receive a :class:`WalletService`) can still address
    the correct backend.
    """
    from jmwallet.backends.descriptor_wallet import generate_wallet_name

    fingerprint = wallet_service.master_key.derive("m/0").fingerprint.hex()
    return generate_wallet_name(fingerprint, wallet_service.network)


def _wallet_name_for_mnemonic(mnemonic: str, network: str, passphrase: str = "") -> str:
    from jmwallet.backends.descriptor_wallet import (
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )

    return generate_wallet_name(get_mnemonic_fingerprint(mnemonic, passphrase), network)


async def get_backend(
    data_dir: Path,
    force_new: bool = False,
    *,
    mnemonic: str | None = None,
    network: str | None = None,
    wallet_service: WalletService | None = None,
) -> Any:
    """Return a blockchain backend instance.

    When ``wallet_service`` or ``mnemonic`` identifies a specific JoinMarket
    wallet, the descriptor backend is built (and cached) with a per-wallet
    bitcoind descriptor wallet name.  Otherwise the legacy
    ``settings.bitcoin.descriptor_wallet_name`` is used.

    Args:
        data_dir: Path to data directory (kept for API compatibility).
        force_new: If True, always build a fresh instance and do not cache
            it.  Used by long-lived components (Maker, Taker) that close
            the backend on exit.
        mnemonic: BIP39 seed phrase.  When given, ``network`` must also be
            provided.  Takes precedence over ``wallet_service`` if both are
            supplied.
        network: Network name (``mainnet`` / ``testnet`` / ``regtest``).
            Required alongside ``mnemonic``.
        wallet_service: Already-initialised wallet service.  Its master
            key is used to derive the per-wallet bitcoind wallet name.

    Returns:
        A backend instance implementing :class:`BlockchainBackend`.
    """
    from jmcore.settings import get_settings

    settings = get_settings()
    backend_type = settings.bitcoin.backend_type

    # Resolve descriptor wallet name for this JM wallet.
    descriptor_wallet_name: str | None = None
    if backend_type == "descriptor_wallet":
        if mnemonic is not None:
            if network is None:
                msg = "get_backend: network is required when mnemonic is passed"
                raise ValueError(msg)
            descriptor_wallet_name = _wallet_name_for_mnemonic(mnemonic, network)
        elif wallet_service is not None:
            descriptor_wallet_name = wallet_name_for_service(wallet_service)
        else:
            descriptor_wallet_name = settings.bitcoin.descriptor_wallet_name

    cache_key = descriptor_wallet_name

    if not force_new:
        cached = _backend_cache.get(cache_key)
        if cached is not None:
            return cached

    logger.info(
        "Initializing blockchain backend: {}{}{}",
        backend_type,
        f" (wallet={descriptor_wallet_name})" if descriptor_wallet_name else "",
        " (new instance)" if force_new else "",
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

    instance: Any

    if backend_type == "descriptor_wallet":
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

        assert descriptor_wallet_name is not None  # narrowed above
        instance = DescriptorWalletBackend(
            rpc_url=rpc_url,
            rpc_user=rpc_user,
            rpc_password=rpc_password,
            wallet_name=descriptor_wallet_name,
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
        resolved_network = network or settings.network_config.network.value

        scan_lookback = getattr(
            settings.bitcoin,
            "neutrino_scan_lookback_blocks",
            settings.wallet.scan_lookback_blocks,
        )

        instance = NeutrinoBackend(
            neutrino_url=neutrino_url,
            network=resolved_network,
            scan_start_height=settings.wallet.scan_start_height,
            scan_lookback_blocks=scan_lookback,
            add_peers=settings.get_neutrino_add_peers(),
            tls_cert_path=settings.bitcoin.neutrino_tls_cert,
            auth_token=settings.bitcoin.neutrino_auth_token,
        )
    else:
        msg = f"Unknown backend type: {backend_type}"
        raise ValueError(msg)

    if force_new:
        return instance

    _backend_cache[cache_key] = instance
    return instance


def reset_backend() -> None:
    """Reset the backend cache (for testing)."""
    _backend_cache.clear()
