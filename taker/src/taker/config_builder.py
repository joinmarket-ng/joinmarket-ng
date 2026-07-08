"""Shared settings-to-TakerConfig mapping.

Single source of truth for turning :class:`jmcore.settings.JoinMarketSettings`
(plus optional CLI/API overrides) into :class:`taker.config.TakerConfig`
construction kwargs. Consumers:

- ``taker.cli`` (CLI taker, also reused by the standalone tumbler CLI)
- ``jmwalletd.routers.coinjoin`` (one-shot ``do_coinjoin`` endpoint)
- ``jmwalletd.routers.tumbler`` (per-phase tumbler taker factory)

Keeping the mapping in one place prevents the recurring drift where a
hand-maintained mirror builder misses newly added policy fields and the
documented config keys are silently ignored for one entry point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jmcore.models import NetworkType
from jmcore.settings import DEFAULT_DIRECTORY_SERVERS, JoinMarketSettings

from taker.config import (
    DEFAULT_COUNTERPARTY_COUNT_MAX,
    BroadcastPolicy,
    MaxCjFee,
    TakerConfig,
)


def build_taker_config_kwargs(
    settings: JoinMarketSettings,
    mnemonic: str,
    passphrase: str,
    # CoinJoin specific settings
    amount: int = 0,
    destination: str = "",
    mixdepth: int = 0,
    counterparties: int | None = None,
    select_utxos: bool = False,
    # CLI overrides (None means use settings value)
    network: NetworkType | None = None,
    bitcoin_network: NetworkType | None = None,
    data_dir: Path | None = None,
    backend_type: str | None = None,
    rpc_url: str | None = None,
    rpc_user: str | None = None,
    rpc_password: str | None = None,
    neutrino_url: str | None = None,
    neutrino_tls_cert: str | None = None,
    neutrino_auth_token: str | None = None,
    directory_servers: str | None = None,
    tor_socks_host: str | None = None,
    tor_socks_port: int | None = None,
    max_abs_fee: int | None = None,
    max_rel_fee: str | None = None,
    fee_rate: float | None = None,
    block_target: int | None = None,
    bondless_makers_allowance: float | None = None,
    bond_value_exponent: float | None = None,
    bondless_require_zero_fee: bool | None = None,
) -> dict[str, Any]:
    """
    Resolve unified settings plus overrides into ``TakerConfig`` kwargs.

    Overrides (when not None) take precedence over settings from the config
    file and env vars. Returns a plain kwargs dict so callers that need to
    inject a config class for testing (jmwalletd) can construct it themselves;
    everyone else should call :func:`build_taker_config`.
    """
    # Resolve network settings
    effective_network = network if network is not None else settings.network_config.network
    effective_bitcoin_network = (
        bitcoin_network
        if bitcoin_network is not None
        else settings.network_config.bitcoin_network or effective_network
    )
    effective_data_dir = data_dir if data_dir is not None else settings.get_data_dir()

    # Resolve backend settings
    effective_backend_type = (
        backend_type if backend_type is not None else settings.bitcoin.backend_type
    )
    effective_rpc_url = rpc_url if rpc_url is not None else settings.bitcoin.rpc_url
    effective_rpc_user = rpc_user if rpc_user is not None else settings.bitcoin.rpc_user
    effective_rpc_password = (
        rpc_password
        if rpc_password is not None
        else settings.bitcoin.rpc_password.get_secret_value()
    )
    # Resolve neutrino TLS/auth consistently with the jmwallet CLIs: relative
    # cert/token paths are joined onto the data dir, the auth-token file is read
    # when present, and the URL is upgraded to HTTPS when auth is enabled.
    from jmcore.cli_common import resolve_backend_settings

    resolved_backend = resolve_backend_settings(
        settings,
        neutrino_url=neutrino_url,
        neutrino_tls_cert=neutrino_tls_cert,
        neutrino_auth_token=neutrino_auth_token,
        data_dir=effective_data_dir,
    )
    effective_neutrino_url = resolved_backend.neutrino_url
    effective_neutrino_tls_cert = resolved_backend.neutrino_tls_cert
    effective_neutrino_auth_token = resolved_backend.neutrino_auth_token

    # Build backend config
    backend_config: dict[str, Any] = {}
    if effective_backend_type == "descriptor_wallet":
        backend_config = {
            "rpc_url": effective_rpc_url,
            "rpc_user": effective_rpc_user,
            "rpc_password": effective_rpc_password,
        }
    elif effective_backend_type == "neutrino":
        backend_config = {
            "neutrino_url": effective_neutrino_url,
            "network": (
                effective_bitcoin_network.value
                if hasattr(effective_bitcoin_network, "value")
                else str(effective_bitcoin_network)
            ),
            "scan_start_height": settings.wallet.scan_start_height,
            "add_peers": settings.get_neutrino_add_peers(),
            "tls_cert_path": effective_neutrino_tls_cert,
            "auth_token": effective_neutrino_auth_token,
            "include_mempool": settings.bitcoin.neutrino_include_mempool,
        }

    # Resolve directory servers
    if directory_servers:
        dir_servers = [s.strip() for s in directory_servers.split(",")]
    elif settings.network_config.directory_servers:
        dir_servers = settings.network_config.directory_servers
    elif network is not None:
        # Network was overridden via CLI, get defaults for that network
        dir_servers = DEFAULT_DIRECTORY_SERVERS.get(effective_network.value, [])
    else:
        dir_servers = settings.get_directory_servers()

    # Resolve Tor settings
    effective_socks_host = tor_socks_host if tor_socks_host is not None else settings.tor.socks_host
    effective_socks_port = tor_socks_port if tor_socks_port is not None else settings.tor.socks_port

    # Resolve taker-specific settings
    effective_counterparties = (
        counterparties if counterparties is not None else settings.taker.counterparty_count
    )
    # If the caller explicitly lowers the maker count for this run (for example
    # a signet / testnet tumbler override), keep the effective minimum-maker
    # threshold consistent with that request. Otherwise sweep mode can select a
    # valid 1-maker CoinJoin and then reject it against a stale higher
    # ``minimum_makers`` from config.
    # When effective_counterparties is None the per-CoinJoin count is drawn
    # randomly at runtime; cap minimum_makers against the configured default max
    # so we never block a valid selection.
    _counterparties_for_min = (
        effective_counterparties
        if effective_counterparties is not None
        else DEFAULT_COUNTERPARTY_COUNT_MAX
    )
    effective_minimum_makers = min(settings.taker.minimum_makers, _counterparties_for_min)
    effective_max_abs_fee = (
        max_abs_fee if max_abs_fee is not None else settings.taker.max_cj_fee_abs
    )
    effective_max_rel_fee = (
        max_rel_fee if max_rel_fee is not None else settings.taker.max_cj_fee_rel
    )
    # Resolve fee settings together so CLI overrides can switch modes cleanly:
    # CLI fee_rate > CLI block_target > config fee_rate > config/default block_target.
    effective_fee_rate: float | None = None
    effective_block_target: int | None = None
    if fee_rate is not None:
        effective_fee_rate = fee_rate
    elif block_target is not None:
        effective_block_target = block_target
    elif settings.taker.fee_rate is not None:
        effective_fee_rate = settings.taker.fee_rate
    else:
        effective_block_target = (
            settings.taker.fee_block_target
            if settings.taker.fee_block_target is not None
            else settings.wallet.default_fee_block_target
        )
    effective_bondless = (
        bondless_makers_allowance
        if bondless_makers_allowance is not None
        else settings.taker.bondless_makers_allowance
    )
    effective_bond_exp = (
        bond_value_exponent
        if bond_value_exponent is not None
        else settings.taker.bond_value_exponent
    )
    effective_bondless_zero_fee = (
        bondless_require_zero_fee
        if bondless_require_zero_fee is not None
        else settings.taker.bondless_require_zero_fee
    )

    # Parse broadcast policy
    try:
        broadcast_policy = BroadcastPolicy(settings.taker.tx_broadcast)
    except ValueError:
        broadcast_policy = BroadcastPolicy.MULTIPLE_PEERS

    # Import SecretStr for wrapping sensitive values
    from pydantic import SecretStr

    return {
        "mnemonic": SecretStr(mnemonic),
        "passphrase": SecretStr(passphrase),
        "network": effective_network,
        "bitcoin_network": effective_bitcoin_network,
        "data_dir": effective_data_dir,
        "backend_type": effective_backend_type,
        "backend_config": backend_config,
        "directory_servers": dir_servers,
        "socks_host": effective_socks_host,
        "socks_port": effective_socks_port,
        "stream_isolation": settings.tor.stream_isolation,
        "connection_timeout": settings.tor.connection_timeout,
        "mixdepth_count": settings.wallet.mixdepth_count,
        "gap_limit": settings.wallet.gap_limit,
        "scan_range": settings.wallet.scan_range,
        "dust_threshold": settings.wallet.dust_threshold,
        "max_sats_freeze_reuse": settings.wallet.max_sats_freeze_reuse,
        "smart_scan": settings.wallet.smart_scan,
        "background_full_rescan": settings.wallet.background_full_rescan,
        "scan_lookback_blocks": settings.wallet.scan_lookback_blocks,
        "destination_address": SecretStr(destination),
        "amount": amount,
        "mixdepth": mixdepth,
        "counterparty_count": effective_counterparties,
        "max_cj_fee": MaxCjFee(abs_fee=effective_max_abs_fee, rel_fee=effective_max_rel_fee),
        "tx_fee_factor": settings.taker.tx_fee_factor,
        "fee_rate": effective_fee_rate,
        "fee_block_target": effective_block_target,
        "max_fee_rate_sat_vb": settings.wallet.max_fee_rate_sat_vb,
        "bondless_makers_allowance": effective_bondless,
        "bond_value_exponent": effective_bond_exp,
        "bondless_makers_allowance_require_zero_fee": effective_bondless_zero_fee,
        "maker_timeout_sec": settings.taker.maker_timeout_sec,
        "order_wait_time": settings.taker.order_wait_time,
        "orderbook_min_wait": settings.taker.orderbook_min_wait,
        "orderbook_quiet_period": settings.taker.orderbook_quiet_period,
        "tx_broadcast": broadcast_policy,
        "broadcast_peer_count": settings.taker.broadcast_peer_count,
        "minimum_makers": effective_minimum_makers,
        "rescan_interval_sec": settings.taker.rescan_interval_sec,
        "pending_tx_abandon_hours": settings.taker.pending_tx_abandon_hours,
        "select_utxos": select_utxos,
        "taker_utxo_age": settings.taker.taker_utxo_age,
        "taker_utxo_retries": settings.taker.taker_utxo_retries,
        "taker_utxo_amtpercent": settings.taker.taker_utxo_amtpercent,
    }


def build_taker_config(
    settings: JoinMarketSettings,
    mnemonic: str,
    passphrase: str,
    # CoinJoin specific settings
    amount: int = 0,
    destination: str = "",
    mixdepth: int = 0,
    counterparties: int | None = None,
    select_utxos: bool = False,
    # CLI overrides (None means use settings value)
    network: NetworkType | None = None,
    bitcoin_network: NetworkType | None = None,
    data_dir: Path | None = None,
    backend_type: str | None = None,
    rpc_url: str | None = None,
    rpc_user: str | None = None,
    rpc_password: str | None = None,
    neutrino_url: str | None = None,
    neutrino_tls_cert: str | None = None,
    neutrino_auth_token: str | None = None,
    directory_servers: str | None = None,
    tor_socks_host: str | None = None,
    tor_socks_port: int | None = None,
    max_abs_fee: int | None = None,
    max_rel_fee: str | None = None,
    fee_rate: float | None = None,
    block_target: int | None = None,
    bondless_makers_allowance: float | None = None,
    bond_value_exponent: float | None = None,
    bondless_require_zero_fee: bool | None = None,
) -> TakerConfig:
    """
    Build TakerConfig from unified settings with CLI overrides.

    CLI arguments (when not None) override settings from config file and env vars.
    """
    return TakerConfig(
        **build_taker_config_kwargs(
            settings,
            mnemonic,
            passphrase,
            amount=amount,
            destination=destination,
            mixdepth=mixdepth,
            counterparties=counterparties,
            select_utxos=select_utxos,
            network=network,
            bitcoin_network=bitcoin_network,
            data_dir=data_dir,
            backend_type=backend_type,
            rpc_url=rpc_url,
            rpc_user=rpc_user,
            rpc_password=rpc_password,
            neutrino_url=neutrino_url,
            neutrino_tls_cert=neutrino_tls_cert,
            neutrino_auth_token=neutrino_auth_token,
            directory_servers=directory_servers,
            tor_socks_host=tor_socks_host,
            tor_socks_port=tor_socks_port,
            max_abs_fee=max_abs_fee,
            max_rel_fee=max_rel_fee,
            fee_rate=fee_rate,
            block_target=block_target,
            bondless_makers_allowance=bondless_makers_allowance,
            bond_value_exponent=bond_value_exponent,
            bondless_require_zero_fee=bondless_require_zero_fee,
        )
    )
