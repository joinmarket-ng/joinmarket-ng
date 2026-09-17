"""Shared settings-to-``MakerConfig`` mapping for daemon-launched makers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import SecretStr

from jmcore.config import build_tor_control_config
from jmcore.models import OfferType
from jmcore.settings import JoinMarketSettings
from maker.config import MergeAlgorithm, OfferConfig
from maker.mixdepth_selection import MixdepthSelectionPolicy

if TYPE_CHECKING:
    from maker.config import MakerConfig


def _parse_offer_type(value: OfferType | str) -> OfferType:
    try:
        return OfferType(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid offer_type in config: {value}. Must be sw0reloffer or sw0absoffer"
        ) from exc


def _parse_merge_algorithm(value: str) -> MergeAlgorithm:
    try:
        return MergeAlgorithm(value.lower())
    except ValueError as exc:
        raise ValueError(
            f"Invalid merge algorithm: {value}. Must be default, gradual, greedy, or random"
        ) from exc


def _parse_mixdepth_selection_policy(value: str) -> MixdepthSelectionPolicy:
    try:
        return MixdepthSelectionPolicy(value.lower())
    except ValueError as exc:
        raise ValueError(
            f"Invalid mixdepth selection policy: {value}. Must be balanced or concentrated"
        ) from exc


def build_daemon_maker_config(
    settings: JoinMarketSettings,
    mnemonic: str,
    data_dir: Path,
    *,
    offer_type: OfferType | str | None = None,
    min_size: int | None = None,
    cj_fee_relative: str | None = None,
    cj_fee_absolute: int | None = None,
    tx_fee_contribution: int | None = None,
) -> MakerConfig:
    """Build a daemon ``MakerConfig`` from settings and optional REST offer overrides.

    MakerBot receives the daemon's already-open wallet and blockchain backend,
    so backend construction settings are intentionally not reproduced here.
    """
    maker_settings = settings.maker
    has_offer_overrides = any(
        value is not None
        for value in (
            offer_type,
            min_size,
            cj_fee_relative,
            cj_fee_absolute,
            tx_fee_contribution,
        )
    )
    effective_offer_type = _parse_offer_type(
        offer_type if offer_type is not None else maker_settings.offer_type
    )
    effective_min_size = min_size if min_size is not None else maker_settings.min_size
    effective_cj_fee_relative = (
        cj_fee_relative if cj_fee_relative is not None else maker_settings.cj_fee_relative
    )
    effective_cj_fee_absolute = (
        cj_fee_absolute if cj_fee_absolute is not None else maker_settings.cj_fee_absolute
    )
    effective_tx_fee_contribution = (
        tx_fee_contribution
        if tx_fee_contribution is not None
        else maker_settings.tx_fee_contribution
    )
    offer_configs = (
        []
        if has_offer_overrides or not maker_settings.dual_offers
        else [
            OfferConfig(
                offer_type=OfferType.SW0_RELATIVE,
                min_size=effective_min_size,
                cj_fee_relative=effective_cj_fee_relative,
                cj_fee_absolute=effective_cj_fee_absolute,
                tx_fee_contribution=effective_tx_fee_contribution,
                cjfee_factor=maker_settings.cjfee_factor,
                txfee_contribution_factor=maker_settings.txfee_contribution_factor,
                size_factor=maker_settings.size_factor,
            ),
            OfferConfig(
                offer_type=OfferType.SW0_ABSOLUTE,
                min_size=effective_min_size,
                cj_fee_relative=effective_cj_fee_relative,
                cj_fee_absolute=effective_cj_fee_absolute,
                tx_fee_contribution=effective_tx_fee_contribution,
                cjfee_factor=maker_settings.cjfee_factor,
                txfee_contribution_factor=maker_settings.txfee_contribution_factor,
                size_factor=maker_settings.size_factor,
            ),
        ]
    )

    # Keep construction lazy so jmwalletd retains its existing optional-maker
    # dependency behavior and does not retain a temporary test patch.
    from maker.config import MakerConfig

    return MakerConfig(
        mnemonic=SecretStr(mnemonic),
        network=settings.network_config.network,
        bitcoin_network=settings.network_config.bitcoin_network or settings.network_config.network,
        data_dir=data_dir,
        directory_servers=settings.get_directory_servers(),
        allow_clearnet_connections=settings.network_config.allow_clearnet_connections,
        nick_auth_mode=settings.network_config.nick_auth_mode,
        nick_auth_directory_ids=settings.network_config.nick_auth_directory_ids,
        socks_host=settings.tor.socks_host,
        socks_port=settings.tor.socks_port,
        stream_isolation=settings.tor.stream_isolation,
        connection_timeout=settings.tor.connection_timeout,
        max_fee_rate_sat_vb=settings.wallet.max_fee_rate_sat_vb,
        tor_control=build_tor_control_config(settings.tor),
        onion_host=maker_settings.onion_host,
        onion_serving_host=maker_settings.onion_serving_host,
        onion_serving_port=maker_settings.onion_serving_port,
        tor_target_host=settings.tor.target_host,
        min_size=effective_min_size,
        min_fee_rate_sat_vb=maker_settings.min_fee_rate_sat_vb,
        min_fee_block_target=maker_settings.min_fee_block_target,
        offer_type=effective_offer_type,
        cj_fee_relative=effective_cj_fee_relative,
        cj_fee_absolute=effective_cj_fee_absolute,
        tx_fee_contribution=effective_tx_fee_contribution,
        cjfee_factor=maker_settings.cjfee_factor,
        txfee_contribution_factor=maker_settings.txfee_contribution_factor,
        size_factor=maker_settings.size_factor,
        min_confirmations=maker_settings.min_confirmations,
        session_timeout_sec=maker_settings.session_timeout_sec,
        pre_sign_timeout_sec=maker_settings.pre_sign_timeout_sec,
        identity_renewal_min_sec=maker_settings.identity_renewal_min_sec,
        identity_renewal_max_sec=maker_settings.identity_renewal_max_sec,
        identity_grace_sec=maker_settings.identity_grace_sec,
        identity_rotation_quiet_min_sec=maker_settings.identity_rotation_quiet_min_sec,
        identity_rotation_quiet_max_sec=maker_settings.identity_rotation_quiet_max_sec,
        pending_tx_timeout_min=maker_settings.pending_tx_timeout_min,
        pending_tx_abandon_hours=maker_settings.pending_tx_abandon_hours,
        rescan_interval_sec=maker_settings.rescan_interval_sec,
        message_rate_limit=maker_settings.message_rate_limit,
        message_burst_limit=maker_settings.message_burst_limit,
        offer_reannounce_delay_max=maker_settings.offer_reannounce_delay_max,
        merge_algorithm=_parse_merge_algorithm(maker_settings.merge_algorithm),
        mixdepth_selection_policy=_parse_mixdepth_selection_policy(
            maker_settings.mixdepth_selection_policy
        ),
        offer_configs=offer_configs,
        allow_mixdepth_zero_merge=maker_settings.allow_mixdepth_zero_merge,
        directory_reconnect_interval=maker_settings.directory_reconnect_interval,
        directory_reconnect_max_retries=maker_settings.directory_reconnect_max_retries,
        directory_startup_timeout=maker_settings.directory_startup_timeout,
        orderbook_rate_limit=maker_settings.orderbook_rate_limit,
        orderbook_rate_interval=maker_settings.orderbook_rate_interval,
        orderbook_violation_ban_threshold=maker_settings.orderbook_violation_ban_threshold,
        orderbook_violation_warning_threshold=maker_settings.orderbook_violation_warning_threshold,
        orderbook_violation_severe_threshold=maker_settings.orderbook_violation_severe_threshold,
        orderbook_ban_duration=maker_settings.orderbook_ban_duration,
    )
