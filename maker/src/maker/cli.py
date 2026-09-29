"""
Maker bot CLI using Typer.

Configuration is loaded with the following priority (highest to lowest):
1. CLI arguments
2. Environment variables
3. Config file (~/.joinmarket-ng/config.toml)
4. Built-in defaults
"""

from __future__ import annotations

import asyncio
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from jmcore.channel_ring import ChannelRingConfig
from jmcore.cli_common import resolve_mnemonic, setup_cli
from jmcore.cli_help import SortedTyper
from jmcore.config import build_tor_control_config
from jmcore.models import NetworkType, OfferType, is_absolute_offer_type
from jmcore.notifications import get_notifier
from jmcore.paths import get_nick_state_component, remove_nick_state, write_nick_state
from jmcore.settings import (
    JoinMarketSettings,
    ensure_config_file,
)
from jmwallet.wallet.service import WalletService
from loguru import logger
from pydantic import SecretStr

from maker.bot import MakerBot
from maker.config import MakerConfig, MergeAlgorithm, OfferConfig
from maker.fidelity import ExpiredFidelityBondCertificateError
from maker.mixdepth_selection import MixdepthSelectionPolicy

if TYPE_CHECKING:
    # A maker without a prepared channel buyout never imports jmswap.
    from jmswap.buyout_config import BuyoutSettings
    from jmswap.coinjoin_funding import ChannelBuyout

app = SortedTyper(no_args_is_help=True)

_BUYOUT_SESSION_RE = re.compile(r"\A[0-9a-f]{64}\Z")
"""Shape of a buyout session id, checked before any file or journal is touched."""

_BUYOUT_OFFERABLE_STATE = "ACCEPTED"
"""The only journal state whose channels may still back advertised offers."""


def run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def build_maker_config(
    settings: JoinMarketSettings,
    mnemonic: str,
    passphrase: str,
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
    tor_control_host: str | None = None,
    tor_control_port: int | None = None,
    tor_cookie_path: Path | None = None,
    disable_tor_control: bool = False,
    onion_serving_host: str | None = None,
    onion_serving_port: int | None = None,
    tor_target_host: str | None = None,
    min_size: int | None = None,
    cj_fee_relative: str | None = None,
    cj_fee_absolute: int | None = None,
    tx_fee_contribution: int | None = None,
    merge_algorithm: str | None = None,
    mixdepth_selection: str | None = None,
    fidelity_bond_locktimes: list[int] | None = None,
    fidelity_bond_index: int | None = None,
    no_fidelity_bond: bool = False,
    dual_offers: bool | None = None,
) -> MakerConfig:
    """
    Build MakerConfig from unified settings with CLI overrides.

    CLI arguments (when not None) override settings from config file and env vars.
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
            "scan_start_height": resolved_backend.scan_start_height,
            "scan_lookback_blocks": resolved_backend.scan_lookback_blocks,
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
    # If CLI provides directory servers, use those
    # Otherwise, if network was overridden via CLI, use defaults for that network
    # Otherwise, use settings (which may have custom servers or default for settings network)
    if directory_servers:
        dir_servers = [s.strip() for s in directory_servers.split(",")]
    elif settings.network_config.directory_servers:
        dir_servers = settings.network_config.directory_servers
    elif network is not None:
        # Network was overridden via CLI, get defaults for that network
        from jmcore.settings import DEFAULT_DIRECTORY_SERVERS

        dir_servers = DEFAULT_DIRECTORY_SERVERS.get(effective_network.value, [])
    else:
        dir_servers = settings.get_directory_servers()

    # Resolve Tor settings
    effective_socks_host = tor_socks_host if tor_socks_host is not None else settings.tor.socks_host
    effective_socks_port = tor_socks_port if tor_socks_port is not None else settings.tor.socks_port

    # Resolve Tor control settings
    tor_control_cfg = build_tor_control_config(
        settings.tor,
        socks_host=tor_socks_host,
        control_host=tor_control_host,
        control_port=tor_control_port,
        cookie_path=tor_cookie_path,
        disable_control=disable_tor_control,
    )

    # Resolve maker-specific settings
    effective_onion_host = (
        onion_serving_host if onion_serving_host is not None else settings.maker.onion_serving_host
    )
    effective_onion_port = (
        onion_serving_port if onion_serving_port is not None else settings.maker.onion_serving_port
    )
    effective_target_host = (
        tor_target_host if tor_target_host is not None else settings.tor.target_host
    )
    effective_min_size = min_size if min_size is not None else settings.maker.min_size
    effective_tx_fee = (
        tx_fee_contribution
        if tx_fee_contribution is not None
        else settings.maker.tx_fee_contribution
    )

    # Determine offer type and fee values
    # CLI explicit values take precedence
    offer_configs: list[OfferConfig] = []

    # Resolve dual_offers: CLI bool (True/False) overrides settings when explicitly passed
    effective_dual_offers = dual_offers if dual_offers is not None else settings.maker.dual_offers

    # The offer family follows the wallet: a p2tr wallet serves the Taproot pit
    # (tr0), a p2wpkh wallet the native-segwit pit (sw0). See JMP-0010.
    relative_offer_type = (
        OfferType.TR0_RELATIVE if settings.wallet.address_type == "p2tr" else OfferType.SW0_RELATIVE
    )
    absolute_offer_type = (
        OfferType.TR0_ABSOLUTE if settings.wallet.address_type == "p2tr" else OfferType.SW0_ABSOLUTE
    )

    if effective_dual_offers:
        # Create both relative and absolute offers
        # Use CLI values if provided, otherwise use settings
        rel_fee = cj_fee_relative if cj_fee_relative is not None else settings.maker.cj_fee_relative
        abs_fee = cj_fee_absolute if cj_fee_absolute is not None else settings.maker.cj_fee_absolute
        tx_fee = (
            tx_fee_contribution
            if tx_fee_contribution is not None
            else settings.maker.tx_fee_contribution
        )
        min_sz = min_size if min_size is not None else settings.maker.min_size

        offer_configs = [
            OfferConfig(
                offer_type=relative_offer_type,
                min_size=min_sz,
                cj_fee_relative=rel_fee,
                cj_fee_absolute=abs_fee,
                tx_fee_contribution=tx_fee,
                cjfee_factor=settings.maker.cjfee_factor,
                txfee_contribution_factor=settings.maker.txfee_contribution_factor,
                size_factor=settings.maker.size_factor,
            ),
            OfferConfig(
                offer_type=absolute_offer_type,
                min_size=min_sz,
                cj_fee_relative=rel_fee,
                cj_fee_absolute=abs_fee,
                tx_fee_contribution=tx_fee,
                cjfee_factor=settings.maker.cjfee_factor,
                txfee_contribution_factor=settings.maker.txfee_contribution_factor,
                size_factor=settings.maker.size_factor,
            ),
        ]
        # Set dummy values for legacy fields (they won't be used)
        parsed_offer_type = relative_offer_type
        actual_cj_fee_relative = rel_fee
        actual_cj_fee_absolute = abs_fee
    elif cj_fee_relative is not None and cj_fee_absolute is not None:
        raise ValueError(
            "Cannot specify both --cj-fee-relative and --cj-fee-absolute. "
            "Use --dual-offers to create both offer types, or use only one fee option."
        )
    elif cj_fee_absolute is not None:
        # User explicitly set absolute fee via CLI
        parsed_offer_type = absolute_offer_type
        actual_cj_fee_relative = settings.maker.cj_fee_relative
        actual_cj_fee_absolute = cj_fee_absolute
    elif cj_fee_relative is not None:
        # User explicitly set relative fee via CLI
        parsed_offer_type = relative_offer_type
        actual_cj_fee_relative = cj_fee_relative
        actual_cj_fee_absolute = settings.maker.cj_fee_absolute
    else:
        # Use settings values (from config file or defaults)
        # Parse offer_type from settings
        try:
            parsed_offer_type = OfferType(settings.maker.offer_type)
        except ValueError:
            raise ValueError(
                f"Invalid offer_type in config: {settings.maker.offer_type}. "
                "Must be one of: sw0reloffer, sw0absoffer, tr0reloffer, tr0absoffer"
            )
        actual_cj_fee_relative = settings.maker.cj_fee_relative
        actual_cj_fee_absolute = settings.maker.cj_fee_absolute

    # Parse merge algorithm
    effective_merge_algorithm_str = (
        merge_algorithm if merge_algorithm is not None else settings.maker.merge_algorithm
    )
    try:
        parsed_merge_algorithm = MergeAlgorithm(effective_merge_algorithm_str.lower())
    except ValueError:
        raise ValueError(
            f"Invalid merge algorithm: {effective_merge_algorithm_str}. "
            "Must be one of: default, gradual, greedy, random"
        )

    effective_mixdepth_selection = (
        mixdepth_selection
        if mixdepth_selection is not None
        else settings.maker.mixdepth_selection_policy
    )
    try:
        parsed_mixdepth_selection = MixdepthSelectionPolicy(effective_mixdepth_selection.lower())
    except ValueError:
        raise ValueError(
            f"Invalid mixdepth selection policy: {effective_mixdepth_selection}. "
            "Must be one of: balanced, concentrated"
        )

    # Log offer configuration for clarity
    if offer_configs:
        # Dual offers mode
        logger.info(f"Dual offers mode: creating {len(offer_configs)} offers")
        for i, oc in enumerate(offer_configs):
            fee_str = (
                f"abs={oc.cj_fee_absolute} sats"
                if is_absolute_offer_type(oc.offer_type)
                else f"rel={oc.cj_fee_relative}"
            )
            logger.info(f"  Offer {i}: type={oc.offer_type.value}, {fee_str}")
    else:
        # Single offer mode
        rel_pct = float(actual_cj_fee_relative) * 100
        fee_str = (
            f"absolute fee={actual_cj_fee_absolute} sats"
            if is_absolute_offer_type(parsed_offer_type)
            else f"relative fee={actual_cj_fee_relative} ({rel_pct:.4f}%)"
        )
        logger.info(f"Offer config: type={parsed_offer_type.value}, {fee_str}")

    # Fidelity bond settings
    effective_locktimes = fidelity_bond_locktimes if fidelity_bond_locktimes else []
    effective_bond_index = fidelity_bond_index

    # Validate: no_fidelity_bond is mutually exclusive with other bond options
    if no_fidelity_bond and (effective_locktimes or effective_bond_index is not None):
        raise ValueError(
            "--no-fidelity-bond cannot be combined with "
            "--fidelity-bond-locktime or --fidelity-bond-index"
        )

    # Validate fidelity bond index requires locktimes
    if effective_bond_index is not None and not effective_locktimes:
        raise ValueError(
            "When using --fidelity-bond-index, you must also specify at least one "
            "--fidelity-bond-locktime"
        )

    return MakerConfig(
        mnemonic=SecretStr(mnemonic),
        passphrase=SecretStr(passphrase),
        network=effective_network,
        bitcoin_network=effective_bitcoin_network,
        data_dir=effective_data_dir,
        backend_type=effective_backend_type,
        backend_config=backend_config,
        directory_servers=dir_servers,
        allow_clearnet_connections=settings.network_config.allow_clearnet_connections,
        nick_auth_mode=settings.network_config.nick_auth_mode,
        nick_auth_directory_ids=settings.network_config.nick_auth_directory_ids,
        socks_host=effective_socks_host,
        socks_port=effective_socks_port,
        stream_isolation=settings.tor.stream_isolation,
        connection_timeout=settings.tor.connection_timeout,
        mixdepth_count=settings.wallet.mixdepth_count,
        gap_limit=settings.wallet.gap_limit,
        address_type=settings.wallet.address_type,
        scan_range=settings.wallet.scan_range,
        dust_threshold=settings.wallet.dust_threshold,
        max_sats_freeze_reuse=settings.wallet.max_sats_freeze_reuse,
        max_fee_rate_sat_vb=settings.wallet.max_fee_rate_sat_vb,
        reconstruct_history=settings.wallet.reconstruct_history,
        smart_scan=settings.wallet.smart_scan,
        background_full_rescan=settings.wallet.background_full_rescan,
        scan_lookback_blocks=settings.wallet.scan_lookback_blocks,
        tor_control=tor_control_cfg,
        onion_host=settings.maker.onion_host,
        onion_serving_host=effective_onion_host,
        onion_serving_port=effective_onion_port,
        tor_target_host=effective_target_host,
        min_size=effective_min_size,
        min_fee_rate_sat_vb=settings.maker.min_fee_rate_sat_vb,
        min_fee_block_target=settings.maker.min_fee_block_target,
        offer_type=parsed_offer_type,
        cj_fee_relative=actual_cj_fee_relative,
        cj_fee_absolute=actual_cj_fee_absolute,
        tx_fee_contribution=effective_tx_fee,
        cjfee_factor=settings.maker.cjfee_factor,
        txfee_contribution_factor=settings.maker.txfee_contribution_factor,
        size_factor=settings.maker.size_factor,
        min_confirmations=settings.maker.min_confirmations,
        session_timeout_sec=settings.maker.session_timeout_sec,
        pre_sign_timeout_sec=settings.maker.pre_sign_timeout_sec,
        identity_renewal_min_sec=settings.maker.identity_renewal_min_sec,
        identity_renewal_max_sec=settings.maker.identity_renewal_max_sec,
        identity_grace_sec=settings.maker.identity_grace_sec,
        identity_rotation_quiet_min_sec=settings.maker.identity_rotation_quiet_min_sec,
        identity_rotation_quiet_max_sec=settings.maker.identity_rotation_quiet_max_sec,
        pending_tx_timeout_min=settings.maker.pending_tx_timeout_min,
        pending_tx_abandon_hours=settings.maker.pending_tx_abandon_hours,
        rescan_interval_sec=settings.maker.rescan_interval_sec,
        message_rate_limit=settings.maker.message_rate_limit,
        message_burst_limit=settings.maker.message_burst_limit,
        offer_reannounce_delay_max=settings.maker.offer_reannounce_delay_max,
        fidelity_bond_locktimes=list(effective_locktimes),
        fidelity_bond_index=effective_bond_index,
        no_fidelity_bond=no_fidelity_bond,
        merge_algorithm=parsed_merge_algorithm,
        mixdepth_selection_policy=parsed_mixdepth_selection,
        offer_configs=offer_configs,
        allow_mixdepth_zero_merge=settings.maker.allow_mixdepth_zero_merge,
        directory_reconnect_interval=settings.maker.directory_reconnect_interval,
        directory_reconnect_max_retries=settings.maker.directory_reconnect_max_retries,
        directory_startup_timeout=settings.maker.directory_startup_timeout,
        orderbook_rate_limit=settings.maker.orderbook_rate_limit,
        orderbook_rate_interval=settings.maker.orderbook_rate_interval,
        orderbook_violation_ban_threshold=settings.maker.orderbook_violation_ban_threshold,
        orderbook_violation_warning_threshold=settings.maker.orderbook_violation_warning_threshold,
        orderbook_violation_severe_threshold=settings.maker.orderbook_violation_severe_threshold,
        orderbook_ban_duration=settings.maker.orderbook_ban_duration,
        channel_ring=ChannelRingConfig.from_settings(settings.maker.channel_ring),
    )


def create_wallet_service(config: MakerConfig) -> WalletService:
    backend_type = config.backend_type.lower()
    # Use bitcoin_network for address generation (bcrt1 vs tb1 vs bc1)
    bitcoin_network = config.bitcoin_network or config.network

    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend

    backend: DescriptorWalletBackend | NeutrinoBackend
    if backend_type == "descriptor_wallet":
        backend_cfg = config.backend_config
        fingerprint = get_mnemonic_fingerprint(
            config.mnemonic.get_secret_value(), config.passphrase.get_secret_value() or ""
        )
        # Convert NetworkType enum to string value
        network_str = (
            bitcoin_network.value if hasattr(bitcoin_network, "value") else str(bitcoin_network)
        )
        wallet_name = generate_wallet_name(fingerprint, network_str)
        backend = DescriptorWalletBackend(
            rpc_url=backend_cfg.get("rpc_url", "http://127.0.0.1:8332"),
            rpc_user=backend_cfg.get("rpc_user", ""),
            rpc_password=backend_cfg.get("rpc_password", ""),
            wallet_name=wallet_name,
            scan_start_height=backend_cfg.get("scan_start_height"),
            scan_lookback_blocks=backend_cfg.get("scan_lookback_blocks", 52_560),
        )
    elif backend_type == "neutrino":
        backend_cfg = config.backend_config
        backend = NeutrinoBackend(
            neutrino_url=backend_cfg.get("neutrino_url", "http://127.0.0.1:8334"),
            network=bitcoin_network.value,
            add_peers=backend_cfg.get("add_peers", []),
            data_dir=backend_cfg.get("data_dir", "/data/neutrino"),
            scan_start_height=backend_cfg.get("scan_start_height"),
            tls_cert_path=backend_cfg.get("tls_cert_path"),
            auth_token=backend_cfg.get("auth_token"),
            include_mempool=backend_cfg.get("include_mempool", True),
        )
    else:
        raise typer.BadParameter(f"Unsupported backend: {backend_type}")

    if config.creation_height is not None:
        backend.set_wallet_creation_height(config.creation_height)

    wallet = WalletService(
        mnemonic=config.mnemonic.get_secret_value(),
        backend=backend,
        network=bitcoin_network.value,
        mixdepth_count=config.mixdepth_count,
        gap_limit=config.gap_limit,
        scan_range=config.scan_range,
        passphrase=config.passphrase.get_secret_value(),
        data_dir=config.data_dir,
        max_sats_freeze_reuse=config.max_sats_freeze_reuse,
        reconstruct_history=config.reconstruct_history,
        mnemonic_file=config.mnemonic_file,
        address_type=config.address_type,
    )
    return wallet


def _validated_buyout_settings(
    path: Path, *, config: MakerConfig, wallet_fingerprint: str
) -> BuyoutSettings:
    """Check one explicit buyout file against this maker, before anything opens.

    The file is named on the command line and is never discovered, adopted or
    migrated: a maker started without it is an ordinary maker. Every rule here
    is decided before a journal, a node connection or a channel signature
    exists, so a file that does not describe *this* wallet, network or pit
    cannot reach a durable resource. The mixdepth is only bounds-checked here;
    the binding that decides which rounds may spend the channels is re-derived
    from the journal by :func:`maker.coinjoin.bound_buyout_mixdepth`.
    """
    from jmswap.buyout_config import load_buyout_settings

    settings = load_buyout_settings(path)
    if not settings.enabled:
        raise ValueError("The buyout service is disabled in the given configuration file")
    if config.address_type != "p2tr":
        # Escrow spends and maker inputs share one Taproot round: a segwit v0
        # pit could never sign it.
        raise ValueError("A channel buyout requires a Taproot wallet and a Taproot pit")
    if settings.network != (config.bitcoin_network or config.network).value:
        raise ValueError("Buyout and maker Bitcoin networks differ")
    if not settings.wallet_fingerprint:
        raise ValueError("A buyer session requires a configured wallet_fingerprint")
    if settings.wallet_fingerprint != wallet_fingerprint:
        raise ValueError("The buyout configuration is bound to a different wallet fingerprint")
    mixdepth = settings.mixdepth
    if mixdepth is None or isinstance(mixdepth, bool):
        raise ValueError("An enabled buyout service requires an integer mixdepth")
    if not 0 <= mixdepth < config.mixdepth_count:
        raise ValueError(
            f"The configured buyout mixdepth {mixdepth} is outside this "
            f"wallet's {config.mixdepth_count} mixdepths"
        )
    return settings


async def _open_prepared_buyout(
    stack: AsyncExitStack, settings: BuyoutSettings, session_id: str
) -> ChannelBuyout:
    """Open one prepared buyout for the maker's whole lifetime.

    The runtime refuses a session it is not bound to, so a session id from
    another endpoint never becomes an offer. Its settlement monitor runs inside
    a task group that spans the maker: a monitor that dies takes the maker down
    with it, instead of leaving it advertising channels behind an endpoint that
    answers nobody. ``stack`` unwinds in registration order reversed, so the
    monitor is signalled and awaited before the journal and nodes close.

    Nothing here cancels, force-closes or otherwise resolves a session: the
    journal owns that decision, and a maker that stops leaves it as it was.
    """
    from jmswap.buyout_runtime import BuyoutRuntime
    from jmswap.coinjoin_funding import ChannelBuyout

    runtime = await stack.enter_async_context(BuyoutRuntime(settings))
    runtime.require_buyer_session(session_id)
    adapter = ChannelBuyout(runtime.buyer, session_id)
    stop = asyncio.Event()
    group = await stack.enter_async_context(asyncio.TaskGroup())
    # Registered after the group, so unwinding stops the monitor before the
    # group waits for it and before the runtime releases the journal.
    stack.callback(stop.set)
    group.create_task(runtime.run(stop))
    return adapter


def _buyout_still_offerable(buyout: ChannelBuyout) -> bool:
    """Whether the durable record still backs the advertised channel liquidity.

    Only an accepted, unused record can fund a new CoinJoin: once a round has
    reserved it, or the operator or the journal has resolved it, the channels
    behind the offers are gone. A journal read that fails is never answered
    with a guess; it propagates and stops the maker.
    """
    return bool(buyout.buyer.store.get(buyout.session_id).state == _BUYOUT_OFFERABLE_STATE)


def _task_group_failure(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """The single failure behind a task group wrapper, or the group itself."""
    error: BaseException = group
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


# Use a sentinel value for CLI defaults to distinguish "not provided" from explicit values
# This allows us to know when to use settings vs CLI override
_NOT_PROVIDED = object()


@app.command()
def start(
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", help="Path to mnemonic file")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool,
        typer.Option(
            "--prompt-bip39-passphrase",
            help="Prompt for BIP39 passphrase interactively",
        ),
    ] = False,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            "-d",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory for JoinMarket files. Defaults to ~/.joinmarket-ng",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
            help="Config file path (decoupled from data dir). Defaults to <data-dir>/config.toml",
        ),
    ] = None,
    network: Annotated[
        NetworkType | None,
        typer.Option(
            case_sensitive=False,
            help="Protocol network (mainnet, testnet, signet, regtest)",
        ),
    ] = None,
    bitcoin_network: Annotated[
        NetworkType | None,
        typer.Option(
            case_sensitive=False,
            help="Bitcoin network for address generation (defaults to --network)",
        ),
    ] = None,
    backend_type: Annotated[
        str | None,
        typer.Option(help="Backend type: descriptor_wallet | neutrino"),
    ] = None,
    buyout_config: Annotated[
        Path | None,
        typer.Option(
            "--buyout-config",
            help="Explicit buyout TOML file (no location is ever guessed). Requires "
            "--buyout-session; without both, this maker uses wallet inputs only.",
        ),
    ] = None,
    buyout_session: Annotated[
        str | None,
        typer.Option(
            "--buyout-session",
            help="Session id of an accepted, unused buyout this maker offers channel "
            "liquidity from. Requires --buyout-config.",
        ),
    ] = None,
    rpc_url: Annotated[
        str | None, typer.Option(envvar="BITCOIN_RPC_URL", help="Bitcoin full node RPC URL")
    ] = None,
    neutrino_url: Annotated[
        str | None, typer.Option(envvar="NEUTRINO_URL", help="Neutrino REST API URL")
    ] = None,
    min_size: Annotated[int | None, typer.Option(help="Minimum CoinJoin size in sats")] = None,
    cj_fee_relative: Annotated[
        str | None,
        typer.Option(
            help="Relative coinjoin fee (e.g., 0.001 = 0.1%)",
            envvar="CJ_FEE_RELATIVE",
        ),
    ] = None,
    cj_fee_absolute: Annotated[
        int | None,
        typer.Option(
            help="Absolute coinjoin fee in sats. Mutually exclusive with --cj-fee-relative.",
            envvar="CJ_FEE_ABSOLUTE",
        ),
    ] = None,
    tx_fee_contribution: Annotated[
        int | None, typer.Option(help="Tx fee contribution in sats")
    ] = None,
    directory_servers: Annotated[
        str | None,
        typer.Option(
            "--directory",
            "-D",
            envvar="DIRECTORY_SERVERS",
            help="Directory servers (comma-separated host:port)",
        ),
    ] = None,
    tor_socks_host: Annotated[
        str | None, typer.Option(help="Tor SOCKS proxy host (overrides TOR__SOCKS_HOST)")
    ] = None,
    tor_socks_port: Annotated[
        int | None, typer.Option(help="Tor SOCKS proxy port (overrides TOR__SOCKS_PORT)")
    ] = None,
    tor_control_host: Annotated[
        str | None,
        typer.Option(
            help="Tor control port host (overrides TOR__CONTROL_HOST)",
        ),
    ] = None,
    tor_control_port: Annotated[
        int | None, typer.Option(help="Tor control port (overrides TOR__CONTROL_PORT)")
    ] = None,
    tor_cookie_path: Annotated[
        Path | None,
        typer.Option(
            help="Path to Tor cookie auth file (overrides TOR__COOKIE_PATH)",
        ),
    ] = None,
    disable_tor_control: Annotated[
        bool,
        typer.Option(
            "--disable-tor-control",
            help="Disable Tor control port integration",
        ),
    ] = False,
    onion_serving_host: Annotated[
        str | None,
        typer.Option(
            help="Bind address for incoming connections (overrides MAKER__ONION_SERVING_HOST)",
        ),
    ] = None,
    onion_serving_port: Annotated[
        int | None,
        typer.Option(
            help="Port for incoming .onion connections (overrides MAKER__ONION_SERVING_PORT)",
        ),
    ] = None,
    tor_target_host: Annotated[
        str | None,
        typer.Option(
            help="Target hostname for Tor hidden service (overrides TOR__TARGET_HOST)",
        ),
    ] = None,
    fidelity_bond_locktimes: Annotated[
        list[int],
        typer.Option("--fidelity-bond-locktime", "-L", help="Fidelity bond locktimes to scan for"),
    ] = [],  # noqa: B006
    fidelity_bond_index: Annotated[
        int | None,
        typer.Option(
            "--fidelity-bond-index",
            "-I",
            envvar="FIDELITY_BOND_INDEX",
            help="Fidelity bond derivation index",
        ),
    ] = None,
    fidelity_bond: Annotated[
        str | None,
        typer.Option(
            "--fidelity-bond",
            "-B",
            help="Specific fidelity bond to use (format: txid:vout)",
        ),
    ] = None,
    no_fidelity_bond: Annotated[
        bool,
        typer.Option(
            "--no-fidelity-bond",
            help="Disable fidelity bond usage. Skips registry lookup and bond proof generation "
            "even when bonds exist in the registry.",
        ),
    ] = False,
    merge_algorithm: Annotated[
        str | None,
        typer.Option(
            "--merge-algorithm",
            "-M",
            envvar="MERGE_ALGORITHM",
            help="UTXO selection strategy: default, gradual, greedy, random",
        ),
    ] = None,
    mixdepth_selection: Annotated[
        str | None,
        typer.Option(
            "--mixdepth-selection",
            envvar="MIXDEPTH_SELECTION",
            help=(
                "Source mixdepth policy: balanced (privacy compartments) or "
                "concentrated (legacy liquidity heuristic)"
            ),
        ),
    ] = None,
    dual_offers: Annotated[
        bool,
        typer.Option(
            "--dual-offers",
            help=(
                "Create both relative and absolute fee offers simultaneously. "
                "Each offer gets a unique ID (0 for relative, 1 for absolute). "
                "Use with --cj-fee-relative and --cj-fee-absolute to set fees for each."
            ),
        ),
    ] = False,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """
    Start the maker bot.

    Configuration is loaded from ~/.joinmarket-ng/config.toml (or $JOINMARKET_DATA_DIR/config.toml),
    environment variables, and CLI arguments. CLI arguments have the highest priority.
    """
    # Opt-in channel buyout: only an explicit file plus session id enables it,
    # and both are checked before any settings, wallet or buyout code loads.
    if (buyout_config is None) != (buyout_session is None):
        logger.error("--buyout-config and --buyout-session must be given together")
        raise typer.Exit(1)
    if buyout_session is not None and _BUYOUT_SESSION_RE.fullmatch(buyout_session) is None:
        logger.error("--buyout-session must be 64 lowercase hex characters")
        raise typer.Exit(1)

    from jmcore.process_hardening import harden_current_process

    # Disable core dumps and ptrace before loading wallet secrets.
    harden_current_process()

    # Load settings (log_level=None means use settings.logging.level)
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)

    # Ensure config file exists (creates template if not)
    ensure_config_file(settings.get_data_dir())

    # Load mnemonic using unified resolver
    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        resolved_mnemonic = resolved.mnemonic if resolved else ""
        resolved_passphrase = resolved.bip39_passphrase if resolved else ""
        resolved_creation_height = resolved.creation_height if resolved else None
    except (ValueError, FileNotFoundError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    # Build MakerConfig with CLI overrides
    try:
        config = build_maker_config(
            settings=settings,
            mnemonic=resolved_mnemonic,
            passphrase=resolved_passphrase,
            network=network,
            bitcoin_network=bitcoin_network,
            data_dir=data_dir,
            backend_type=backend_type,
            rpc_url=rpc_url,
            neutrino_url=neutrino_url,
            directory_servers=directory_servers,
            tor_socks_host=tor_socks_host,
            tor_socks_port=tor_socks_port,
            tor_control_host=tor_control_host,
            tor_control_port=tor_control_port,
            tor_cookie_path=tor_cookie_path,
            disable_tor_control=disable_tor_control,
            onion_serving_host=onion_serving_host,
            onion_serving_port=onion_serving_port,
            tor_target_host=tor_target_host,
            min_size=min_size,
            cj_fee_relative=cj_fee_relative,
            cj_fee_absolute=cj_fee_absolute,
            tx_fee_contribution=tx_fee_contribution,
            merge_algorithm=merge_algorithm,
            mixdepth_selection=mixdepth_selection,
            fidelity_bond_locktimes=fidelity_bond_locktimes if fidelity_bond_locktimes else None,
            fidelity_bond_index=fidelity_bond_index,
            no_fidelity_bond=no_fidelity_bond,
            # Pass True only when the flag was explicitly given; False means "not given",
            # so we pass None to let build_maker_config fall back to settings.
            dual_offers=True if dual_offers else None,
        )
    except ValueError as e:
        logger.error(str(e))
        raise typer.Exit(1)

    if resolved_creation_height is not None:
        config.creation_height = resolved_creation_height
    if resolved is not None:
        config.mnemonic_file = resolved.mnemonic_file

    # Log configuration source
    logger.info(f"Using network: {config.network.value}")
    logger.info(f"Using backend: {config.backend_type}")
    logger.info("Tor SOCKS proxy configured")
    logger.bind(sensitive=True).info(f"Tor SOCKS: {config.socks_host}:{config.socks_port}")
    logger.info(f"Directory servers: {len(config.directory_servers)} configured")

    wallet = create_wallet_service(config)

    # One maker per CoinJoin pit: the nick state filename is fixed per address type.
    nick_component = get_nick_state_component("maker", config.address_type)

    def _publish_maker_nick(_old_nick: str, new_nick: str) -> None:
        write_nick_state(config.data_dir, nick_component, new_nick)

    buyout_settings: BuyoutSettings | None = None
    if buyout_config is not None:
        try:
            from jmswap.buyout_config import BuyoutConfigError
        except ImportError:
            logger.error("Channel buyout support requires the jmswap package")
            raise typer.Exit(1)
        try:
            buyout_settings = _validated_buyout_settings(
                buyout_config, config=config, wallet_fingerprint=wallet.wallet_fingerprint
            )
        except (BuyoutConfigError, ValueError) as exc:
            logger.error(str(exc))
            raise typer.Exit(1)

    # Store the specific fidelity bond selection if provided
    if fidelity_bond and no_fidelity_bond:
        logger.error("--fidelity-bond and --no-fidelity-bond are mutually exclusive")
        raise typer.Exit(1)

    if fidelity_bond:
        try:
            parts = fidelity_bond.split(":")
            if len(parts) != 2:
                raise ValueError("Invalid format")
            config.selected_fidelity_bond = (parts[0], int(parts[1]))
            logger.bind(sensitive=True).info(f"Using specified fidelity bond: {fidelity_bond}")
        except (ValueError, IndexError):
            logger.error("Invalid fidelity bond format. Use txid:vout")
            logger.bind(sensitive=True).error(
                f"Invalid fidelity bond format: {fidelity_bond}. Use txid:vout"
            )
            raise typer.Exit(1)

    # The bot is built inside the event loop because a prepared buyout only
    # exists there; it is published here so an interrupt can still stop it.
    bot: MakerBot | None = None

    async def shutdown_maker(maker: MakerBot) -> None:
        # Clean up nick state file on shutdown
        remove_nick_state(config.data_dir, nick_component)
        await maker.stop()

    async def run_bot() -> None:
        nonlocal bot
        try:
            async with AsyncExitStack() as stack:
                buyout: ChannelBuyout | None = None
                if buyout_settings is not None and buyout_session is not None:
                    try:
                        buyout = await _open_prepared_buyout(stack, buyout_settings, buyout_session)
                    except Exception as exc:
                        logger.error("The prepared buyout session could not be opened")
                        logger.bind(sensitive=True).error("Buyout session failure: {}", exc)
                        raise typer.Exit(1) from None
                    logger.info(f"Offering channel liquidity from buyout session {buyout_session}")

                try:
                    bot = MakerBot(
                        wallet,
                        wallet.backend,
                        config,
                        nick_change_callback=_publish_maker_nick,
                        buyout=buyout,
                    )
                except ValueError as exc:
                    if buyout is None:
                        raise
                    logger.error("The prepared buyout cannot fund this maker")
                    logger.bind(sensitive=True).error("Buyout binding failure: {}", exc)
                    raise typer.Exit(1) from None
                # Registered before the nick state is written, so every exit
                # path removes it and stops the maker, and registered after the
                # buyout so the maker is stopped before the runtime closes.
                stack.push_async_callback(shutdown_maker, bot)

                try:
                    # Write nick state file for external tracking and
                    # cross-component protection
                    nick = bot.nick
                    data_dir = config.data_dir
                    write_nick_state(data_dir, nick_component, nick)
                    logger.info("Maker nick state written")
                    logger.bind(sensitive=True).info(
                        f"Nick state written to {data_dir}/state/{nick_component}.nick"
                    )

                    # Send startup notification immediately (including nick)
                    notifier = get_notifier(settings, component_name="Maker")
                    await notifier.notify_startup(
                        component="Maker",
                        network=config.network.value,
                        nick=nick,
                    )
                    await bot.start()
                    while True:
                        await asyncio.sleep(1)
                        if (
                            buyout is not None
                            and bot.current_offers
                            and not _buyout_still_offerable(buyout)
                        ):
                            # The advertised channel liquidity no longer exists,
                            # so withdraw it now instead of leaving it up until
                            # the ordinary rescan interval. The observed offer
                            # balance is zero, which keeps the publication lock
                            # and the privacy delay and rebuilds the offers from
                            # wallet state without forcing a rescan. Offers are
                            # cleared by that refresh, so this runs once.
                            await bot._update_offers(expected_balance=0)
                except asyncio.CancelledError:
                    pass
        except BaseExceptionGroup as group:
            # The settlement monitor shares a task group with the maker, so a
            # failure on either side arrives wrapped. A lone maker failure keeps
            # its own CLI handling; anything else ends the maker cleanly.
            failure = _task_group_failure(group)
            if isinstance(
                failure,
                typer.Exit
                | KeyboardInterrupt
                | SystemExit
                | asyncio.CancelledError
                | ExpiredFidelityBondCertificateError,
            ):
                raise failure from None
            logger.error("Maker stopped: the prepared buyout lifetime failed")
            logger.bind(sensitive=True).error("Buyout lifetime failure: {}", failure)
            raise typer.Exit(1) from None

    try:
        run_async(run_bot())
    except ExpiredFidelityBondCertificateError as e:
        logger.error("Fidelity bond certificate expired")
        logger.bind(sensitive=True).error(str(e))
        raise typer.Exit(1)
    except KeyboardInterrupt:
        logger.info("Shutting down maker bot...")
        if bot is not None:
            run_async(bot.stop())


@app.command()
def enroll_ring_nodes(
    component: Annotated[str, typer.Option(help="Ring settings to enroll: maker or taker")],
    expected_node: Annotated[
        list[str], typer.Option(help="Repeat NAME=PUBKEY for every mapped LND identity")
    ],
    acknowledge_prior_use: Annotated[
        bool,
        typer.Option(help="Acknowledge that enrollment cannot prove historical node isolation"),
    ] = False,
    config_file: Annotated[Path | None, typer.Option(help="Configuration file")] = None,
    data_dir: Annotated[Path | None, typer.Option(help="JoinMarket data directory")] = None,
    mnemonic_file: Annotated[Path | None, typer.Option(help="Wallet mnemonic file")] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option(help="Prompt for the wallet's BIP39 passphrase")
    ] = False,
) -> None:
    """Explicitly bind LN identities to this wallet's source mixdepths.

    Does not sync the wallet, open channels, recover journals, or start a maker.
    Every cooperating maker/taker profile must share the binding directory.
    """
    from jmcore.channel_ring import ChannelRingConfig
    from jmcore.process_hardening import harden_current_process
    from jmswap.channel_ring_nodes import (
        ChannelRingNodeError,
        channel_ring_wallet_identity,
        enroll_configured_ring_nodes,
    )
    from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed

    if component not in {"maker", "taker"} or not acknowledge_prior_use:
        raise typer.BadParameter("choose maker or taker and explicitly acknowledge prior node use")
    identities: dict[str, str] = {}
    for item in expected_node:
        name, separator, node_id = item.partition("=")
        if not separator or not name or name in identities:
            raise typer.BadParameter("expected nodes must be unique NAME=PUBKEY pairs")
        identities[name] = node_id
    harden_current_process()
    settings = setup_cli(None, data_dir=data_dir, config_file=config_file)
    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        if resolved is None:
            raise ValueError("node enrollment requires the wallet mnemonic")
        if settings.wallet.address_type != "p2tr":
            raise ValueError("channel-ring enrollment requires a p2tr wallet")
        ring_settings = (
            settings.maker.channel_ring if component == "maker" else settings.taker.channel_ring
        )
        offer_type = (
            settings.maker.offer_type
            if component == "maker"
            else settings.taker.preferred_offer_type.value
        )
        master = HDKey.from_seed(mnemonic_to_seed(resolved.mnemonic, resolved.bip39_passphrase))
        bindings = run_async(
            enroll_configured_ring_nodes(
                ChannelRingConfig.from_settings(ring_settings),
                network=settings.network_config.network.value,
                offer_type=offer_type,
                wallet_identity=channel_ring_wallet_identity(master.get_public_key_bytes()),
                mixdepth_count=settings.wallet.mixdepth_count,
                expected_node_ids=identities,
                acknowledge_prior_use=acknowledge_prior_use,
            )
        )
    except (ValueError, OSError, ChannelRingNodeError) as exc:
        logger.error("Node enrollment failed: {}", exc)
        raise typer.Exit(1) from exc
    typer.echo(f"Enrolled {len(bindings)} node identity binding(s); no channels were changed.")


@app.command()
def ring_records(
    component: Annotated[
        str, typer.Option(help="Ring settings to inspect: maker or taker")
    ] = "maker",
    config_file: Annotated[Path | None, typer.Option(help="Configuration file")] = None,
    data_dir: Annotated[Path | None, typer.Option(help="JoinMarket data directory")] = None,
) -> None:
    """List retained channel-ring records as JSON for operator recovery.

    Read-only: does not create the journal, contact LND or the chain, change
    wallet leases, or print ring secrets. Active records keep their inputs
    locked; ``retirement_action`` is ``blocked`` when no local evidence proves
    that retiring the record is safe.
    """
    import json

    from jmcore.channel_ring import ChannelRingConfig
    from jmcore.channel_ring_store import RingParticipantStore, RingStoreError

    if component not in {"maker", "taker"}:
        raise typer.BadParameter("choose maker or taker")
    settings = setup_cli(None, data_dir=data_dir, config_file=config_file)
    ring_settings = (
        settings.maker.channel_ring if component == "maker" else settings.taker.channel_ring
    )
    try:
        config = ChannelRingConfig.from_settings(ring_settings)
        effective_data_dir = data_dir if data_dir is not None else settings.get_data_dir()
        directory = config.persistence_path(effective_data_dir)
        if not directory.is_dir():
            typer.echo(json.dumps({"directory": str(directory), "records": [], "corrupt": []}))
            return
        report = RingParticipantStore(
            directory,
            max_active_sessions=config.max_active_sessions,
            max_verified_sessions=config.max_verified_sessions,
        ).load_all()
    except (ValueError, OSError, RingStoreError) as exc:
        logger.error("Could not read channel-ring records: {}", exc)
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps(
            {
                "directory": str(directory),
                "records": [record.operator_summary() for record in report.records],
                "corrupt": [
                    {"file": item.path.name, "error": item.error} for item in report.corruptions
                ],
            },
            indent=2,
        )
    )


@app.command()
def generate_address(
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", help="Path to mnemonic file")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool,
        typer.Option(
            "--prompt-bip39-passphrase",
            help="Prompt for BIP39 passphrase interactively",
        ),
    ] = False,
    network: Annotated[
        NetworkType | None,
        typer.Option(case_sensitive=False, help="Protocol network"),
    ] = None,
    bitcoin_network: Annotated[
        NetworkType | None,
        typer.Option(
            case_sensitive=False,
            help="Bitcoin network for address generation (defaults to --network)",
        ),
    ] = None,
    backend_type: Annotated[str | None, typer.Option(help="Backend type")] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
            help="Config file path (decoupled from data dir). Defaults to <data-dir>/config.toml",
        ),
    ] = None,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """Generate a new receive address."""
    # Load settings (log_level=None means use settings.logging.level)
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)

    # Load mnemonic using unified resolver
    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        resolved_mnemonic = resolved.mnemonic if resolved else ""
        resolved_passphrase = resolved.bip39_passphrase if resolved else ""
        resolved_creation_height = resolved.creation_height if resolved else None
    except (ValueError, FileNotFoundError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    # Build config with CLI overrides
    try:
        config = build_maker_config(
            settings=settings,
            mnemonic=resolved_mnemonic,
            passphrase=resolved_passphrase,
            network=network,
            bitcoin_network=bitcoin_network,
            backend_type=backend_type,
        )
    except ValueError as e:
        logger.error(str(e))
        raise typer.Exit(1)

    if resolved_creation_height is not None:
        config.creation_height = resolved_creation_height

    wallet = create_wallet_service(config)
    address = wallet.get_receive_address(0, 0)
    typer.echo(address)


@app.command()
def config_init(
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            "-d",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory for JoinMarket files",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
            help="Config file path (decoupled from data dir). Defaults to <data-dir>/config.toml",
        ),
    ] = None,
) -> None:
    """Initialize the config file with default settings."""
    # Determine data directory
    from jmcore.paths import get_default_data_dir
    from jmcore.settings import reset_settings

    reset_settings()

    if data_dir is None:
        data_dir = get_default_data_dir()

    config_path = ensure_config_file(data_dir, config_file=config_file)
    typer.echo(f"Config file created at: {config_path}")
    typer.echo("\nAll settings are commented out by default.")
    typer.echo("Edit the file to customize your configuration.")
    typer.echo("\nPriority (highest to lowest):")
    typer.echo("  1. CLI arguments")
    typer.echo("  2. Environment variables")
    typer.echo("  3. Config file")
    typer.echo("  4. Built-in defaults")


def main() -> None:  # pragma: no cover
    app()
