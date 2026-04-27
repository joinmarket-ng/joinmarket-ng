"""
Standalone command-line interface for the JoinMarket tumbler.

Mirrors the patterns used by :mod:`taker.cli` and :mod:`maker.cli`:
configuration is loaded from (in priority order) CLI arguments, environment
variables, the config file at ``~/.joinmarket-ng/config.toml`` (or
``$JOINMARKET_DATA_DIR/config.toml``), and built-in defaults.

The CLI is a thin wrapper around :mod:`tumbler.builder`,
:mod:`tumbler.persistence`, and :mod:`tumbler.runner`. Plans are
persisted to ``<data_dir>/schedules/<wallet_name>.yaml`` so the same file
is used whether the tumble runs from the CLI or from jmwalletd.
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Annotated, Any

import typer
from jmcore.cli_common import resolve_mnemonic, setup_cli
from jmcore.models import NetworkType
from jmcore.paths import remove_nick_state, write_nick_state
from jmcore.settings import ensure_config_file
from jmwallet.wallet.service import WalletService
from loguru import logger

from tumbler.builder import PlanBuilder, TumbleParameters
from tumbler.estimator import PlanEstimate, estimate_plan_costs
from tumbler.maker_policy import apply_tumbler_maker_policy
from tumbler.persistence import (
    PlanCorruptError,
    PlanNotFoundError,
    load_plan,
    plan_path,
    save_plan,
)
from tumbler.persistence import (
    delete_plan as delete_plan_on_disk,
)
from tumbler.plan import MIN_DESTINATIONS, Plan, PlanStatus
from tumbler.runner import RunnerContext, TumbleRunner

app = typer.Typer(
    name="jm-tumbler",
    help="JoinMarket tumbler - role-mixed CoinJoin schedules with YAML-persisted state",
)


# ---------------------------------------------------------------------------
# Shared CLI helpers
# ---------------------------------------------------------------------------


def _load_or_error(wallet_name: str, data_dir: Path) -> Plan:
    try:
        return load_plan(wallet_name, data_dir)
    except PlanNotFoundError:
        logger.error(f"No tumbler plan found for wallet {wallet_name!r} under {data_dir}")
        raise typer.Exit(1)
    except PlanCorruptError as exc:
        logger.error(f"Tumbler plan is corrupt: {exc}")
        raise typer.Exit(1)


def _format_duration(seconds: float) -> str:
    """Render a seconds value as ``HhMm`` / ``MmSs`` / ``Ss`` for humans."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    return f"{days}d{hours:02d}h"


def _format_sats(sats: int) -> str:
    """Render sats with thousands separators and a BTC hint for big values."""
    if sats >= 100_000:
        return f"{sats:,} sats ({sats / 1e8:.8f} BTC)"
    return f"{sats:,} sats"


def _summarise_plan(plan: Plan, estimate: PlanEstimate | None = None) -> None:
    typer.echo(f"plan_id:      {plan.plan_id}")
    typer.echo(f"wallet:       {plan.wallet_name}")
    typer.echo(f"status:       {plan.status.value}")
    typer.echo(f"phases:       {len(plan.phases)} (current={plan.current_phase})")
    typer.echo(f"destinations: {', '.join(plan.destinations)}")
    if plan.error:
        typer.echo(f"error:        {plan.error}")
    for phase in plan.phases:
        marker = ">" if phase.index == plan.current_phase else " "
        typer.echo(f" {marker} [{phase.index:02d}] {phase.kind.value:<22} {phase.status.value}")

    if estimate is None:
        return

    typer.echo("")
    typer.echo("Wallet balance")
    typer.echo("--------------")
    typer.echo(f"  total:                  {_format_sats(estimate.total_balance_sats)}")
    for mixdepth, balance in sorted(estimate.mixdepth_balances.items()):
        if balance > 0:
            typer.echo(f"    mixdepth {mixdepth}:           {_format_sats(balance)}")

    typer.echo("")
    typer.echo("Estimated cost & time")
    typer.echo("---------------------")
    typer.echo(f"  taker phases:           {estimate.taker_phase_count}")
    typer.echo(f"  maker phases:           {estimate.maker_phase_count}")
    typer.echo(
        f"  max counterparty fees:  {_format_sats(estimate.total_max_cj_fee_sats)}"
        f"  ({estimate.total_max_cj_fee_pct:.3f}% of balance, upper bound)"
    )
    typer.echo(
        f"  est. miner fees:        {_format_sats(estimate.total_miner_fee_sats)}"
        f"  ({estimate.total_miner_fee_pct:.3f}% of balance,"
        f" @ {estimate.fee_rate_sat_vb:.2f} sat/vB {estimate.fee_rate_source})"
    )
    typer.echo(
        f"  total fee upper bound:  {_format_sats(estimate.total_max_fee_sats)}"
        f"  ({estimate.total_max_fee_pct:.3f}% of balance)"
    )
    typer.echo(
        f"  inter-phase wait:       {_format_duration(estimate.total_wait_seconds)}"
        "  (sum of randomised waits)"
    )
    typer.echo(
        f"  total duration:         min {_format_duration(estimate.total_duration_seconds_min)}"
        f", expected {_format_duration(estimate.total_duration_seconds_expected)}"
        f", max {_format_duration(estimate.total_duration_seconds_max)}"
    )
    typer.echo(
        f"  per-phase confirmations: {estimate.confirmation_block_count} blocks"
        f"  (~{_format_duration(estimate.confirmation_block_count * 600)} on mainnet)"
    )


def _resolve_wallet_name(
    settings: Any,
    wallet_name: str | None,
    mnemonic_file: Path | None,
    prompt_bip39_passphrase: bool,
) -> str:
    """Return ``wallet_name`` if set, else derive it from the default mnemonic.

    Mirrors the default-wallet resolution used by ``jm-wallet info`` so the
    read-only ``status`` and ``delete`` commands stay symmetrical with
    ``plan`` / ``run`` (which already fall back to the mnemonic fingerprint).
    """
    if wallet_name:
        return wallet_name
    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
    except (ValueError, FileNotFoundError) as exc:
        logger.error(str(exc))
        raise typer.Exit(1)
    if resolved is None:
        logger.error("Could not resolve a wallet name; pass --wallet-name or configure a mnemonic.")
        raise typer.Exit(1)
    return _wallet_name_from_mnemonic(
        resolved.mnemonic, resolved.bip39_passphrase or "", settings.network_config.network
    )


async def _collect_balances(wallet: WalletService, mixdepth_count: int) -> dict[int, int]:
    balances: dict[int, int] = {}
    for mixdepth in range(mixdepth_count):
        try:
            balances[mixdepth] = int(await wallet.get_balance(mixdepth))
        except Exception:
            logger.exception(f"failed to read balance for mixdepth {mixdepth}")
            balances[mixdepth] = 0
    return balances


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
@app.command("plan")
def plan_command(
    destinations: Annotated[
        list[str],
        typer.Option("--destination", "-d", help="External destination address (repeatable)"),
    ],
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
    wallet_name: Annotated[
        str | None,
        typer.Option(
            "--wallet-name",
            "-w",
            help="Wallet identifier for the plan file; defaults to the mnemonic fingerprint",
        ),
    ] = None,
    network: Annotated[
        NetworkType | None,
        typer.Option("--network", case_sensitive=False, help="Bitcoin network"),
    ] = None,
    backend_type: Annotated[
        str | None,
        typer.Option(
            "--backend", "-b", help="Backend type: scantxoutset | descriptor_wallet | neutrino"
        ),
    ] = None,
    rpc_url: Annotated[
        str | None,
        typer.Option("--rpc-url", envvar="BITCOIN_RPC_URL", help="Bitcoin full node RPC URL"),
    ] = None,
    neutrino_url: Annotated[
        str | None,
        typer.Option("--neutrino-url", envvar="NEUTRINO_URL", help="Neutrino REST API URL"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing pending plan",
        ),
    ] = False,
    seed: Annotated[
        int | None,
        typer.Option("--seed", help="Seed the plan builder RNG for reproducible schedules"),
    ] = None,
    maker_count_min: Annotated[
        int | None,
        typer.Option(
            help=(
                "Minimum counterparty count per CJ; defaults to settings.taker.counterparty_count"
            ),
        ),
    ] = None,
    maker_count_max: Annotated[
        int | None,
        typer.Option(
            help=(
                "Maximum counterparty count per CJ; defaults to settings.taker.counterparty_count"
            ),
        ),
    ] = None,
    mincjamount_sats: Annotated[int, typer.Option(help="Minimum CJ amount in sats")] = 100_000,
    include_maker_sessions: Annotated[
        bool, typer.Option("--maker-sessions/--no-maker-sessions")
    ] = True,
    allow_few_destinations: Annotated[
        bool,
        typer.Option(
            "--allow-few-destinations",
            help=(
                "Override the recommended minimum of "
                f"{MIN_DESTINATIONS} destinations. Intended for development and "
                "automated testing only: fewer destinations expose users to "
                "pairwise re-aggregation heuristics."
            ),
        ),
    ] = False,
    log_level: Annotated[str | None, typer.Option("--log-level", "-l")] = None,
) -> None:
    """Build a tumbler plan for the given destinations and persist it."""
    if len(destinations) < MIN_DESTINATIONS and not allow_few_destinations:
        logger.error(
            "at least {} destination addresses are recommended (got {}). "
            "Pass --allow-few-destinations to override.",
            MIN_DESTINATIONS,
            len(destinations),
        )
        raise typer.Exit(1)
    settings = setup_cli(log_level)
    ensure_config_file(settings.get_data_dir())
    data_dir = settings.get_data_dir()

    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
    except (ValueError, FileNotFoundError) as exc:
        logger.error(str(exc))
        raise typer.Exit(1)
    if resolved is None:
        logger.error("Could not resolve a mnemonic; supply --mnemonic-file or configure one.")
        raise typer.Exit(1)

    effective_wallet = wallet_name or _wallet_name_from_mnemonic(
        resolved.mnemonic, resolved.bip39_passphrase or "", settings.network_config.network
    )

    existing: Plan | None
    try:
        existing = load_plan(effective_wallet, data_dir)
    except PlanNotFoundError:
        existing = None
    except PlanCorruptError as exc:
        logger.error(f"Tumbler plan is corrupt: {exc}")
        raise typer.Exit(1)

    if existing is not None and existing.status == PlanStatus.RUNNING:
        logger.error(
            f"A plan is already RUNNING for {effective_wallet}; use 'jm-tumbler stop' first."
        )
        raise typer.Exit(1)
    if existing is not None and existing.status == PlanStatus.PENDING and not force:
        logger.error("A pending plan already exists; pass --force to overwrite.")
        raise typer.Exit(1)

    try:
        balances, fee_rate, fee_rate_source = asyncio.run(
            _balances_for_mnemonic(
                settings=settings,
                mnemonic=resolved.mnemonic,
                passphrase=resolved.bip39_passphrase or "",
                network=network,
                backend_type=backend_type,
                rpc_url=rpc_url,
                neutrino_url=neutrino_url,
            )
        )
    except RuntimeError as exc:
        logger.error(str(exc))
        raise typer.Exit(1)

    if not any(v > 0 for v in balances.values()):
        logger.error("Wallet has no confirmed coins to tumble.")
        raise typer.Exit(1)

    try:
        effective_min = (
            maker_count_min if maker_count_min is not None else settings.taker.counterparty_count
        )
        effective_max = (
            maker_count_max if maker_count_max is not None else settings.taker.counterparty_count
        )
        params = TumbleParameters(
            destinations=list(destinations),
            mixdepth_balances=balances,
            maker_count_min=effective_min,
            maker_count_max=effective_max,
            mincjamount_sats=mincjamount_sats,
            include_maker_sessions=include_maker_sessions,
            seed=seed,
        )
        plan = PlanBuilder(wallet_name=effective_wallet, params=params).build()
    except ValueError as exc:
        logger.error(str(exc))
        raise typer.Exit(1)

    path = save_plan(plan, data_dir)
    typer.echo(f"Plan written to {path}")

    estimate = estimate_plan_costs(
        plan,
        mixdepth_balances=balances,
        max_cj_fee_abs_sats=settings.taker.max_cj_fee_abs,
        max_cj_fee_rel=settings.taker.max_cj_fee_rel,
        fee_rate_sat_vb=fee_rate,
        fee_rate_source=fee_rate_source,
    )
    _summarise_plan(plan, estimate=estimate)

    # Echo the relevant taker config so the user knows what bounds were
    # applied -- these are the same knobs that gate every CJ during run.
    typer.echo("")
    typer.echo("Active taker config")
    typer.echo("-------------------")
    typer.echo(f"  max_cj_fee_abs:         {settings.taker.max_cj_fee_abs} sats")
    typer.echo(f"  max_cj_fee_rel:         {settings.taker.max_cj_fee_rel}")
    typer.echo(
        f"  counterparty_count:     {settings.taker.counterparty_count}"
        f" (plan range: {effective_min}-{effective_max})"
    )
    if settings.taker.fee_rate is not None:
        typer.echo(f"  fee_rate:               {settings.taker.fee_rate} sat/vB")
    elif settings.taker.fee_block_target is not None:
        typer.echo(f"  fee_block_target:       {settings.taker.fee_block_target} blocks")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
@app.command("status")
def status_command(
    wallet_name: Annotated[
        str | None,
        typer.Option(
            "--wallet-name",
            "-w",
            help="Wallet identifier; defaults to the mnemonic fingerprint",
        ),
    ] = None,
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
    log_level: Annotated[str | None, typer.Option("--log-level", "-l")] = None,
) -> None:
    """Print the current plan for the given wallet."""
    settings = setup_cli(log_level)
    effective_wallet = _resolve_wallet_name(
        settings, wallet_name, mnemonic_file, prompt_bip39_passphrase
    )
    plan = _load_or_error(effective_wallet, settings.get_data_dir())
    _summarise_plan(plan)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------
@app.command("delete")
def delete_command(
    wallet_name: Annotated[
        str | None,
        typer.Option(
            "--wallet-name",
            "-w",
            help="Wallet identifier; defaults to the mnemonic fingerprint",
        ),
    ] = None,
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
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
    log_level: Annotated[str | None, typer.Option("--log-level", "-l")] = None,
) -> None:
    """Delete the on-disk plan for ``wallet_name``."""
    settings = setup_cli(log_level)
    data_dir = settings.get_data_dir()
    effective_wallet = _resolve_wallet_name(
        settings, wallet_name, mnemonic_file, prompt_bip39_passphrase
    )
    plan = _load_or_error(effective_wallet, data_dir)
    if plan.status == PlanStatus.RUNNING:
        logger.error("Plan is RUNNING; stop it before deleting.")
        raise typer.Exit(1)
    if not yes and not typer.confirm(
        f"Delete tumbler plan for {effective_wallet} (status={plan.status.value})?"
    ):
        typer.echo("Cancelled.")
        return
    if delete_plan_on_disk(effective_wallet, data_dir):
        typer.echo(f"Deleted {plan_path(effective_wallet, data_dir)}")
    else:
        typer.echo("Nothing to delete.")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
@app.command("run")
def run_command(
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", help="Path to mnemonic file")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool,
        typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase interactively"),
    ] = False,
    wallet_name: Annotated[
        str | None,
        typer.Option(
            "--wallet-name", "-w", help="Wallet identifier; defaults to the mnemonic fingerprint"
        ),
    ] = None,
    network: Annotated[NetworkType | None, typer.Option("--network", case_sensitive=False)] = None,
    backend_type: Annotated[
        str | None,
        typer.Option("--backend", "-b"),
    ] = None,
    rpc_url: Annotated[str | None, typer.Option("--rpc-url", envvar="BITCOIN_RPC_URL")] = None,
    neutrino_url: Annotated[
        str | None, typer.Option("--neutrino-url", envvar="NEUTRINO_URL")
    ] = None,
    directory_servers: Annotated[
        str | None,
        typer.Option("--directory", "-D", envvar="DIRECTORY_SERVERS"),
    ] = None,
    tor_socks_host: Annotated[str | None, typer.Option(help="Tor SOCKS host override")] = None,
    tor_socks_port: Annotated[int | None, typer.Option(help="Tor SOCKS port override")] = None,
    fee_rate: Annotated[
        float | None,
        typer.Option(
            "--fee-rate",
            help=(
                "Manual fee rate in sat/vB (mutually exclusive with --block-target). "
                "Required when the backend is neutrino."
            ),
        ),
    ] = None,
    block_target: Annotated[
        int | None,
        typer.Option(
            "--block-target",
            help=(
                "Target blocks for fee estimation (mutually exclusive with --fee-rate). "
                "Not supported with the neutrino backend."
            ),
        ),
    ] = None,
    min_confirmations_between_phases: Annotated[
        int,
        typer.Option(
            "--min-confirmations",
            help="Confirmations required before the next phase starts (0 disables gating)",
        ),
    ] = 5,
    counterparties: Annotated[
        int | None,
        typer.Option(
            "--counterparties",
            min=1,
            max=20,
            help=(
                "Override the counterparty count for every phase at runtime. "
                "Useful when the configured count is unavailable on the chosen network."
            ),
        ),
    ] = None,
    log_level: Annotated[str | None, typer.Option("--log-level", "-l")] = None,
) -> None:
    """Execute the saved plan for a wallet to completion."""
    settings = setup_cli(log_level)
    ensure_config_file(settings.get_data_dir())
    data_dir = settings.get_data_dir()

    if fee_rate is not None and block_target is not None:
        logger.error("--fee-rate and --block-target are mutually exclusive.")
        raise typer.Exit(1)

    effective_backend_type = backend_type or settings.bitcoin.backend_type
    if effective_backend_type == "neutrino" and fee_rate is None:
        logger.error("Neutrino backend cannot estimate fees; pass --fee-rate <sat/vB> to proceed.")
        raise typer.Exit(1)

    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
    except (ValueError, FileNotFoundError) as exc:
        logger.error(str(exc))
        raise typer.Exit(1)
    if resolved is None:
        logger.error("Could not resolve a mnemonic.")
        raise typer.Exit(1)

    effective_wallet = wallet_name or _wallet_name_from_mnemonic(
        resolved.mnemonic, resolved.bip39_passphrase or "", settings.network_config.network
    )
    plan = _load_or_error(effective_wallet, data_dir)
    if plan.status in (PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED):
        logger.error(
            f"Plan is in terminal state {plan.status.value}; create a new plan before running."
        )
        raise typer.Exit(1)
    if plan.status == PlanStatus.RUNNING:
        # A prior process crashed mid-run. Reconcile to FAILED and bail: the
        # user must inspect and delete before re-planning.
        plan.status = PlanStatus.FAILED
        plan.error = plan.error or "previous run crashed"
        save_plan(plan, data_dir)
        logger.error("Plan was RUNNING on disk but no runner is attached; marked FAILED.")
        raise typer.Exit(1)

    try:
        asyncio.run(
            _run_plan(
                settings=settings,
                plan=plan,
                mnemonic=resolved.mnemonic,
                passphrase=resolved.bip39_passphrase or "",
                creation_height=resolved.creation_height,
                data_dir=data_dir,
                network=network,
                backend_type=backend_type,
                rpc_url=rpc_url,
                neutrino_url=neutrino_url,
                directory_servers=directory_servers,
                tor_socks_host=tor_socks_host,
                tor_socks_port=tor_socks_port,
                fee_rate=fee_rate,
                block_target=block_target,
                min_confirmations_between_phases=min_confirmations_between_phases,
                counterparties_override=counterparties,
            )
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        raise typer.Exit(130)


# ---------------------------------------------------------------------------
# config-init
# ---------------------------------------------------------------------------
@app.command("config-init")
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
) -> None:
    """Initialize the config file with default settings."""
    from jmcore.paths import get_default_data_dir

    if data_dir is None:
        data_dir = get_default_data_dir()
    config_path = ensure_config_file(data_dir)
    typer.echo(f"Config file created at: {config_path}")


# ---------------------------------------------------------------------------
# Implementation helpers
# ---------------------------------------------------------------------------


def _wallet_name_from_mnemonic(mnemonic: str, passphrase: str, network: NetworkType) -> str:
    """Derive a stable wallet identifier from the mnemonic fingerprint.

    Mirrors :func:`jmwallet.backends.descriptor_wallet.generate_wallet_name` so
    both the CLI and jmwalletd-backed flows land on the same plan file.
    """
    from jmwallet.backends.descriptor_wallet import (
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )

    fingerprint = get_mnemonic_fingerprint(mnemonic, passphrase)
    return generate_wallet_name(fingerprint, network.value)


async def _balances_for_mnemonic(
    settings: Any,
    mnemonic: str,
    passphrase: str,
    network: NetworkType | None,
    backend_type: str | None,
    rpc_url: str | None,
    neutrino_url: str | None,
) -> tuple[dict[int, int], float | None, str]:
    """Open a read-only wallet, sync, and return balances + a fee-rate estimate.

    Returns ``(balances, fee_rate_sat_vb, fee_rate_source)``. The fee rate
    is resolved from ``settings.taker.fee_rate`` (configured), then
    ``settings.taker.fee_block_target`` via the live backend (estimated),
    then ``None`` (caller falls back to a built-in default).
    """
    from taker.cli import build_taker_config, create_backend

    config = build_taker_config(
        settings=settings,
        mnemonic=mnemonic,
        passphrase=passphrase,
        network=network,
        backend_type=backend_type,
        rpc_url=rpc_url,
        neutrino_url=neutrino_url,
    )
    backend = create_backend(config)
    wallet = WalletService(
        mnemonic=config.mnemonic.get_secret_value(),
        passphrase=config.passphrase.get_secret_value(),
        backend=backend,
        network=(config.bitcoin_network or config.network).value,
        mixdepth_count=config.mixdepth_count,
        data_dir=config.data_dir,
    )
    try:
        await wallet.sync()
    except AttributeError:
        # Older WalletService revisions sync lazily; balances will still work.
        pass
    try:
        balances = await _collect_balances(wallet, config.mixdepth_count)
        fee_rate, fee_source = await _resolve_fee_rate(settings, backend)
        return balances, fee_rate, fee_source
    finally:
        close = getattr(wallet, "close", None)
        if close is not None:
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # pragma: no cover - best effort close
                logger.exception("wallet close failed")


async def _resolve_fee_rate(settings: Any, backend: Any) -> tuple[float | None, str]:
    """Resolve a sat/vB rate from settings.taker, falling back to estimatesmartfee.

    Returns ``(rate, source)`` where ``source`` is one of ``configured``
    (manual ``fee_rate``), ``estimated`` (resolved from ``fee_block_target``
    via the backend), or ``fallback`` (neither available; caller picks a
    built-in default).
    """
    if settings.taker.fee_rate is not None:
        return float(settings.taker.fee_rate), "configured"
    target = settings.taker.fee_block_target
    if target is None:
        return None, "fallback"
    estimate = getattr(backend, "estimate_fee", None)
    if estimate is None:
        return None, "fallback"
    try:
        # Backends return BTC/kvB by convention; convert to sat/vB.
        btc_per_kvb = await estimate(int(target))
        if btc_per_kvb is None or btc_per_kvb <= 0:
            return None, "fallback"
        sat_per_vb = float(btc_per_kvb) * 1e8 / 1000.0
        return sat_per_vb, "estimated"
    except Exception:  # pragma: no cover - best effort: fall through to default
        logger.exception("fee rate estimation failed; falling back to built-in default")
        return None, "fallback"


async def _run_plan(
    settings: Any,
    plan: Plan,
    mnemonic: str,
    passphrase: str,
    creation_height: int | None,
    data_dir: Path,
    network: NetworkType | None,
    backend_type: str | None,
    rpc_url: str | None,
    neutrino_url: str | None,
    directory_servers: str | None,
    tor_socks_host: str | None,
    tor_socks_port: int | None,
    fee_rate: float | None,
    block_target: int | None,
    min_confirmations_between_phases: int,
    counterparties_override: int | None = None,
) -> None:
    """Instantiate backend, wallet, and runner; execute the plan."""
    from maker.bot import MakerBot
    from maker.cli import build_maker_config
    from maker.cli import create_wallet_service as create_maker_wallet
    from pydantic import SecretStr
    from taker.cli import build_taker_config, create_backend
    from taker.taker import Taker

    # Shared wallet across all phases (the runner asks factories to build
    # transient Takers/Makers but reuses this ``WalletService``).
    taker_config = build_taker_config(
        settings=settings,
        mnemonic=mnemonic,
        passphrase=passphrase,
        network=network,
        backend_type=backend_type,
        rpc_url=rpc_url,
        neutrino_url=neutrino_url,
        directory_servers=directory_servers,
        tor_socks_host=tor_socks_host,
        tor_socks_port=tor_socks_port,
        fee_rate=fee_rate,
        block_target=block_target,
    )
    if creation_height is not None:
        taker_config.creation_height = creation_height
    shared_backend = create_backend(taker_config)
    bitcoin_network = taker_config.bitcoin_network or taker_config.network
    wallet = WalletService(
        mnemonic=taker_config.mnemonic.get_secret_value(),
        passphrase=taker_config.passphrase.get_secret_value(),
        backend=shared_backend,
        network=bitcoin_network.value,
        mixdepth_count=taker_config.mixdepth_count,
        data_dir=taker_config.data_dir,
    )
    try:
        await wallet.sync()
    except AttributeError:
        pass

    async def _taker_factory(phase: Any) -> Any:
        backend = create_backend(taker_config)
        effective_counterparties = (
            counterparties_override
            if counterparties_override is not None
            else getattr(phase, "counterparty_count", None)
        )
        # The runner also reads phase.counterparty_count when calling
        # ``do_coinjoin``; keep it consistent with the TakerConfig we build so
        # the override applies uniformly.
        if counterparties_override is not None and hasattr(phase, "counterparty_count"):
            phase.counterparty_count = counterparties_override
        config = build_taker_config(
            settings=settings,
            mnemonic=mnemonic,
            passphrase=passphrase,
            amount=getattr(phase, "amount", 0) or 0,
            destination=getattr(phase, "destination", "INTERNAL") or "INTERNAL",
            mixdepth=getattr(phase, "mixdepth", 0),
            counterparties=effective_counterparties,
            network=network,
            backend_type=backend_type,
            rpc_url=rpc_url,
            neutrino_url=neutrino_url,
            directory_servers=directory_servers,
            tor_socks_host=tor_socks_host,
            tor_socks_port=tor_socks_port,
            fee_rate=fee_rate,
            block_target=block_target,
        )
        if creation_height is not None:
            config.creation_height = creation_height
        return Taker(wallet=wallet, backend=backend, config=config)

    async def _maker_factory(_phase: Any) -> Any:
        backend = create_backend(taker_config)
        config = build_maker_config(
            settings=settings,
            mnemonic=mnemonic,
            passphrase=passphrase,
            network=network,
            backend_type=backend_type,
            rpc_url=rpc_url,
            neutrino_url=neutrino_url,
            directory_servers=directory_servers,
            tor_socks_host=tor_socks_host,
            tor_socks_port=tor_socks_port,
        )
        # Tumbler maker sessions must run as 0-fee sw0absoffer with no
        # fidelity bond; otherwise the offer is unlikely to be picked
        # (bondless + non-zero fee) or, if a bond is reused, the session
        # links every phase under the same identity. See
        # ``apply_tumbler_maker_policy`` for the full rationale.
        apply_tumbler_maker_policy(config)
        _ = SecretStr  # re-export guard for linter (SecretStr is used via config)
        _ = create_maker_wallet  # silence unused-import; reserved for future fork
        return MakerBot(wallet=wallet, backend=backend, config=config)

    async def _get_confirmations(txid: str) -> int | None:
        from tumbler.confirmations import resolve_confirmations

        return await resolve_confirmations(txid, shared_backend, data_dir)

    ctx = RunnerContext(
        wallet_service=wallet,
        wallet_name=plan.wallet_name,
        data_dir=data_dir,
        taker_factory=_taker_factory,
        maker_factory=_maker_factory,
        get_confirmations=_get_confirmations,
        min_confirmations_between_phases=min_confirmations_between_phases,
    )
    runner = TumbleRunner(plan, ctx)

    write_nick_state(data_dir, "tumbler", plan.wallet_name)
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(runner.run())

    def _signal_handler() -> None:
        logger.info("Stop requested; finishing current phase then exiting.")
        runner.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    try:
        final = await task
    finally:
        remove_nick_state(data_dir, "tumbler")

    typer.echo(f"Tumble finished with status: {final.status.value}")
    if final.error:
        typer.echo(f"error: {final.error}")
    if final.status != PlanStatus.COMPLETED:
        raise typer.Exit(1)


def main() -> None:
    """Entry point."""
    app()


if __name__ == "__main__":
    main()
