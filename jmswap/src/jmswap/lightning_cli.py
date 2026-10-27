"""``jm-lightning``: JoinMarket-managed LND nodes, one per mixdepth."""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path
from typing import Annotated

import typer
from jmcore.cli_common import resolve_mnemonic, setup_cli
from jmcore.crypto import mnemonic_to_seed
from loguru import logger

from jmswap.lightning_context import LightningContext, lightning_context
from jmswap.lnd_release import LND_VERSION, LndReleaseError, download_and_install, is_installed
from jmswap.managed_nodes import ManagedNodeError
from jmswap.managed_status import NodeStatus, registry_status

app = typer.Typer(
    name="jm-lightning",
    help="Run JoinMarket-managed Lightning nodes (one LND per mixdepth).",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

MnemonicFile = Annotated[
    Path | None,
    typer.Option("--mnemonic-file", "-f", help="Path to mnemonic file", envvar="MNEMONIC_FILE"),
]
PromptPassphrase = Annotated[
    bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for the BIP39 passphrase")
]
DataDir = Annotated[
    Path | None,
    typer.Option("--data-dir", envvar="JOINMARKET_DATA_DIR", help="Data directory"),
]
ConfigFile = Annotated[
    Path | None,
    typer.Option("--config-file", envvar="JOINMARKET_CONFIG_FILE", help="Config file path"),
]
LogLevel = Annotated[str | None, typer.Option("--log-level", "-l", help="Log level")]


def _fail(message: str) -> typer.Exit:
    logger.error(message)
    return typer.Exit(1)


def _context(
    mnemonic_file: Path | None,
    prompt_bip39_passphrase: bool,
    data_dir: Path | None,
    config_file: Path | None,
    log_level: str | None,
    *,
    require_managed: bool = True,
) -> LightningContext:
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)
    if require_managed and settings.lightning.mode != "managed":
        raise _fail('Set [lightning] mode = "managed" (and wallet.address_type = "p2tr") first')
    try:
        resolved = resolve_mnemonic(
            settings, mnemonic_file=mnemonic_file, prompt_bip39_passphrase=prompt_bip39_passphrase
        )
    except (FileNotFoundError, ValueError) as exc:
        raise _fail(str(exc)) from None
    if resolved is None:
        raise _fail("No wallet mnemonic found")
    seed = mnemonic_to_seed(resolved.mnemonic, resolved.bip39_passphrase)
    try:
        return lightning_context(settings, seed)
    except ManagedNodeError as exc:
        raise _fail(str(exc)) from None


@app.command("install-lnd")
def install_lnd(
    data_dir: DataDir = None,
    config_file: ConfigFile = None,
    proxy: Annotated[
        str | None, typer.Option("--proxy", help="Proxy URL for the download, e.g. socks5://...")
    ] = None,
    log_level: LogLevel = None,
) -> None:
    """Download the pinned LND release and verify it against pinned digests."""
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)
    try:
        binary = download_and_install(Path(settings.get_data_dir()), proxy=proxy)
    except (LndReleaseError, OSError) as exc:
        raise _fail(f"LND installation failed: {exc}") from None
    typer.echo(f"LND {LND_VERSION} verified and installed at {binary}")


@app.command()
def init(
    mnemonic_file: MnemonicFile = None,
    prompt_bip39_passphrase: PromptPassphrase = False,
    data_dir: DataDir = None,
    config_file: ConfigFile = None,
    log_level: LogLevel = None,
) -> None:
    """Create one fresh managed node per mixdepth, or finish an interrupted creation."""
    ctx = _context(mnemonic_file, prompt_bip39_passphrase, data_dir, config_file, log_level)
    try:
        ctx.env.validate_for_launch()
    except ManagedNodeError as exc:
        raise _fail(str(exc)) from None
    if not is_installed(ctx.env.lnd_binary) and not ctx.env.lnd_binary.is_file():
        raise _fail("lnd is not installed; run `jm-lightning install-lnd` first")
    from jmswap.managed_provision import allocate_missing, create_nodes, resume

    assert ctx.keys is not None
    try:
        with ctx.store.exclusive_processes():
            if ctx.store.load() is None:
                registry = asyncio.run(
                    create_nodes(
                        ctx.store,
                        ctx.keys,
                        ctx.env,
                        wallet_identity=ctx.wallet_identity,
                        mixdepths=ctx.mixdepths,
                    )
                )
            else:
                added = allocate_missing(ctx.store, ctx.mixdepths)
                if added:
                    logger.info("Adding managed nodes for mixdepths {}", added)
                registry = asyncio.run(resume(ctx.store, ctx.keys, ctx.env))
    except ManagedNodeError as exc:
        raise _fail(str(exc)) from None
    typer.echo(f"Managed nodes ready (epoch {registry.epoch}, keep it with your backups):")
    for node in registry.runnable():
        typer.echo(f"  mixdepth {node.mixdepth}: {node.identity_pubkey}")
    typer.echo("Start them with `jm-lightning run`.")


@app.command()
def run(
    mnemonic_file: MnemonicFile = None,
    prompt_bip39_passphrase: PromptPassphrase = False,
    data_dir: DataDir = None,
    config_file: ConfigFile = None,
    log_level: LogLevel = None,
) -> None:
    """Run every managed node until interrupted, keeping channel backups current."""
    ctx = _context(mnemonic_file, prompt_bip39_passphrase, data_dir, config_file, log_level)
    from jmswap.managed_supervisor import Supervisor

    assert ctx.keys is not None
    try:
        ctx.env.validate_for_launch()
    except ManagedNodeError as exc:
        raise _fail(str(exc)) from None
    supervisor = Supervisor(ctx.store, ctx.keys, ctx.env)

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await supervisor.run(stop)

    try:
        asyncio.run(main())
    except ManagedNodeError as exc:
        raise _fail(str(exc)) from None
    logger.info("All managed nodes stopped")


def _format_status(rows: list[NodeStatus], reserve_min: int) -> list[str]:
    lines = [
        f"{'MD':<3}{'GEN':<4}{'STATE':<12}{'NODE':<14}{'RESERVE':>10}"
        f"{'CHANNELS':>10}{'OUTBOUND':>12}{'INBOUND':>12}  NOTE"
    ]
    for row in sorted(rows, key=lambda r: (r.mixdepth, r.generation)):
        node = (row.identity_pubkey or "-")[:12]
        note = row.error or ("" if row.synced else "syncing")
        if row.running and not row.reserve_ok and row.state == "active":
            note = (note + f" reserve below {reserve_min}").strip()
        channels = f"{row.channels}" + (f"+{row.pending_channels}p" if row.pending_channels else "")
        lines.append(
            f"{row.mixdepth:<3}{row.generation:<4}{row.state.value:<12}{node:<14}"
            f"{row.confirmed_sats:>10}{channels:>10}{row.outbound_sats:>12}"
            f"{row.inbound_sats:>12}  {note}"
        )
    return lines


@app.command()
def status(
    mnemonic_file: MnemonicFile = None,
    prompt_bip39_passphrase: PromptPassphrase = False,
    data_dir: DataDir = None,
    config_file: ConfigFile = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
    log_level: LogLevel = None,
) -> None:
    """Show each mixdepth's node: identity, reserve, channels, and liquidity."""
    ctx = _context(mnemonic_file, prompt_bip39_passphrase, data_dir, config_file, log_level)
    try:
        registry = ctx.store.load()
    except ManagedNodeError as exc:
        raise _fail(str(exc)) from None
    if registry is None:
        raise _fail("No managed nodes yet; run `jm-lightning init`")
    reserve_min = int(ctx.settings.lightning.reserve_min_sats)
    rows = asyncio.run(registry_status(registry, ctx.paths, ctx.env, reserve_min_sats=reserve_min))
    if as_json:
        typer.echo(
            json.dumps(
                {"epoch": registry.epoch, "nodes": [row.model_dump(mode="json") for row in rows]},
                indent=2,
            )
        )
        return
    typer.echo(f"Managed Lightning nodes ({ctx.network}, epoch {registry.epoch})")
    for line in _format_status(rows, reserve_min):
        typer.echo(line)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
