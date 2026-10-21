"""Guided setup configuration, kept separate from wallet/node creation."""

from __future__ import annotations

import copy
import hashlib
import os
import shlex
import sys
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer
from click import Choice
from jmcore.config_file import ConfigFileError, ConfigScalar, update_config_values
from jmcore.constants import BITCOIN_RPC_PORTS
from jmcore.models import NetworkType
from jmcore.paths import get_default_data_dir
from jmcore.secure_files import read_private_file, read_sensitive_file
from jmcore.settings import JoinMarketSettings, validate_config_document
from pydantic import SecretStr, ValidationError

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


class LightningMode(StrEnum):
    OFF = "off"
    MANAGED = "managed"
    MANUAL = "manual"


class WalletAction(StrEnum):
    EXISTING = "existing"
    GENERATE = "generate"
    IMPORT = "import"


@dataclass(frozen=True)
class _SelectedWallet:
    path: Path
    mnemonic: SecretStr
    newly_created: bool
    config_target: Path
    config_digest: str


def _config_path(data_dir: Path | None, config_file: Path | None) -> Path:
    if config_file is not None:
        return config_file.expanduser().absolute()
    if data_dir is not None:
        return data_dir.expanduser().absolute() / "config.toml"
    override = os.environ.get("JOINMARKET_CONFIG_FILE")
    return (
        Path(override).expanduser().absolute()
        if override
        else get_default_data_dir() / "config.toml"
    )


def _contents(path: Path) -> bytes | None:
    try:
        return read_sensitive_file(path)
    except FileNotFoundError:
        return None


def _endpoint(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit("//" + value)
        if (
            not parsed.hostname
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        return parsed.hostname, parsed.port
    except ValueError:
        raise typer.BadParameter("Tor endpoints must be HOST:PORT or [IPv6]:PORT") from None


@app.command()
def setup(
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Application data directory")
    ] = None,
    config_file: Annotated[
        Path | None, typer.Option("--config-file", help="Configuration file")
    ] = None,
    network: Annotated[
        NetworkType | None, typer.Option("--network", help="JoinMarket network")
    ] = None,
    bitcoin_network: Annotated[
        NetworkType | None,
        typer.Option("--bitcoin-network", help="Explicit blockchain-network override"),
    ] = None,
    taproot: Annotated[
        bool | None,
        typer.Option("--taproot/--no-taproot", help="Optional global Taproot pit switch"),
    ] = None,
    lightning: Annotated[
        LightningMode | None, typer.Option("--lightning", help="off, managed, or manual Lightning")
    ] = None,
    rpc_url: Annotated[
        str | None, typer.Option("--rpc-url", help="Bitcoin Core RPC endpoint")
    ] = None,
    rpc_cookie_file: Annotated[
        Path | None,
        typer.Option(
            "--rpc-cookie-file", help="Bitcoin Core cookie path (not read during configuration)"
        ),
    ] = None,
    tor_socks: Annotated[
        str | None, typer.Option("--tor-socks", help="Tor SOCKS HOST:PORT")
    ] = None,
    tor_control: Annotated[
        str | None, typer.Option("--tor-control", help="Tor control HOST:PORT")
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Accept configuration defaults; never implies fresh-wallet history"
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate and preview without writing")
    ] = False,
    configure_only: Annotated[
        bool,
        typer.Option("--configure-only", help="Configure only; do not create wallets or nodes"),
    ] = False,
    wallet_action: Annotated[
        WalletAction | None,
        typer.Option("--wallet-action", help="Explicit existing, generate, or import selection"),
    ] = None,
    wallet_file: Annotated[
        Path | None, typer.Option("--wallet-file", help="Selected mnemonic file or new destination")
    ] = None,
    wallet_password_file: Annotated[
        Path | None,
        typer.Option("--wallet-password-file", help="Private file holding the encryption password"),
    ] = None,
    import_mnemonic_file: Annotated[
        Path | None,
        typer.Option(
            "--import-mnemonic-file", help="Private plaintext BIP39 source for explicit import"
        ),
    ] = None,
    resume_wallet_publication: Annotated[
        bool,
        typer.Option(
            "--resume-wallet-publication", help="Resume a matching setup journal; never regenerate"
        ),
    ] = False,
    confirm_import_recovery: Annotated[
        bool,
        typer.Option(
            "--confirm-import-recovery",
            help="Allow missing import metadata to be published as recovery pending",
        ),
    ] = False,
    show_generated_seed: Annotated[
        bool,
        typer.Option(
            "--show-generated-seed",
            help="Explicitly disclose a completed generated seed, including to redirected output",
        ),
    ] = False,
    create_managed_nodes: Annotated[
        bool | None,
        typer.Option(
            "--create-managed-nodes/--no-create-managed-nodes",
            help="Explicit fresh managed identities, not restoration or service startup",
        ),
    ] = None,
    install_lnd: Annotated[
        bool,
        typer.Option(
            "--install-lnd",
            help="Authorize downloading the pinned LND release through Tor when needed",
        ),
    ] = False,
    applications_quiesced: Annotated[
        bool,
        typer.Option(
            "--applications-quiesced",
            help="Affirm consumers and automatic restarts are excluded during node initialization",
        ),
    ] = False,
    bip39_passphrase_file: Annotated[
        Path | None,
        typer.Option(
            "--bip39-passphrase-file",
            help="Private optional BIP39 passphrase input for node identities",
        ),
    ] = None,
    no_bip39_passphrase: Annotated[
        bool,
        typer.Option(
            "--no-bip39-passphrase",
            help="Explicitly use no BIP39 passphrase for managed-node identities",
        ),
    ] = False,
) -> None:
    """Configure the standard setup without assuming wallet freshness.

    Configure-only and dry-run never access wallet secrets. Wallet creation or
    import requires an explicit choice; this stage never starts Lightning nodes.
    """
    path = _config_path(data_dir, config_file)
    try:
        target = path.resolve(strict=False)
        original = _contents(path)
        document = tomllib.loads(original.decode("utf-8")) if original is not None else {}
        defaults = validate_config_document(document)
    except (OSError, RuntimeError, ValueError, ValidationError):
        typer.echo("Existing configuration cannot be validated; no changes written.", err=True)
        raise typer.Exit(1) from None

    selected_network = network or defaults.network_config.network
    if network is None and not yes:
        selected_network = NetworkType(
            typer.prompt(
                "Network (mainnet/testnet/signet/regtest)",
                default=selected_network.value,
                type=Choice([item.value for item in NetworkType]),
            )
        )
    selected_taproot = taproot if taproot is not None else defaults.wallet.address_type == "p2tr"
    if taproot is None and not yes:
        selected_taproot = typer.confirm(
            "Enable the optional Taproot pit?", default=selected_taproot
        )
    selected_lightning = lightning or LightningMode(defaults.lightning.mode)
    if lightning is None and not selected_taproot:
        selected_lightning = LightningMode.OFF
    if lightning is None and not yes and selected_taproot:
        selected_lightning = LightningMode(
            typer.prompt(
                "Lightning (off/managed/manual)",
                default=selected_lightning.value,
                type=Choice([item.value for item in LightningMode]),
            )
        )

    updates: dict[str, dict[str, ConfigScalar]] = {
        "network_config": {"network": selected_network.value},
        "wallet": {"address_type": "p2tr" if selected_taproot else "p2wpkh"},
        "lightning": {"mode": selected_lightning.value},
    }
    if bitcoin_network is not None:
        updates["network_config"]["bitcoin_network"] = bitcoin_network.value
    chain = bitcoin_network or defaults.network_config.bitcoin_network or selected_network
    selected_url = rpc_url or defaults.bitcoin.rpc_url
    if rpc_url is None and "rpc_url" not in document.get("bitcoin", {}):
        selected_url = f"http://127.0.0.1:{BITCOIN_RPC_PORTS[chain.value]}"
    if rpc_url is None and not yes and defaults.bitcoin.backend_type != "neutrino":
        # Existing endpoints may contain inline authentication. Never display
        # that default or echo a replacement containing credentials.
        selected_url = typer.prompt(
            "Bitcoin Core RPC URL (hidden; Enter keeps current)",
            default=selected_url,
            show_default=False,
            hide_input=True,
        )
    updates["bitcoin"] = {"rpc_url": selected_url}
    selected_cookie = rpc_cookie_file
    if (
        selected_cookie is None
        and not yes
        and selected_taproot
        and selected_lightning is LightningMode.MANAGED
        and defaults.bitcoin.backend_type == "descriptor_wallet"
        and not defaults.bitcoin.rpc_cookie_file
        and not (defaults.bitcoin.rpc_user and defaults.bitcoin.rpc_password.get_secret_value())
    ):
        selected_cookie = Path(
            typer.prompt("Bitcoin Core RPC cookie file (path only; not read now)")
        )
    if selected_cookie is not None:
        updates["bitcoin"].update(
            rpc_cookie_file=str(selected_cookie.expanduser().absolute()),
            rpc_user="",
            rpc_password="",
        )
    if tor_socks is not None:
        host, port = _endpoint(tor_socks)
        updates.setdefault("tor", {}).update(socks_host=host, socks_port=port)
    if tor_control is not None:
        host, port = _endpoint(tor_control)
        updates.setdefault("tor", {}).update(
            control_enabled=True, control_host=host, control_port=port
        )

    candidate = copy.deepcopy(document)
    for section, values in updates.items():
        candidate.setdefault(section, {}).update(values)
    try:
        planned = validate_config_document(candidate)
        if planned.lightning.mode == "managed" and (
            planned.bitcoin.backend_type != "descriptor_wallet" or not planned.tor.control_enabled
        ):
            raise ValueError("managed nodes require Core and Tor control")
        typer.echo(f"Wallet pit: {planned.wallet.address_type}")
        typer.echo(f"Lightning: {planned.lightning.mode}")
        typer.echo(f"Network: {planned.network_config.network.value}; blockchain: {chain.value}")
        if dry_run:
            typer.echo("Preview only; no files written.")
            return
        if not yes and not typer.confirm("Save this configuration?", default=False):
            typer.echo("No changes written.")
            return
        # Do not apply decisions based on a config edited during prompting.
        if path.resolve(strict=False) != target or _contents(path) != original:
            raise ConfigFileError("configuration changed while choosing setup options")
        update_config_values(path, updates, validate=validate_config_document)
    except (ConfigFileError, OSError, RuntimeError, ValueError, ValidationError):
        typer.echo(
            "Configuration could not be safely applied; "
            "rerun setup or review the selected settings.",
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(f"Configuration saved: {path}")
    if configure_only:
        typer.echo("Configuration only; wallet and node state unchanged.")
        return
    if wallet_action is None:
        if yes:
            if create_managed_nodes or install_lnd:
                typer.echo(
                    "Node initialization requires explicit --wallet-action selection; "
                    "no nodes created.",
                    err=True,
                )
                raise typer.Exit(1)
            typer.echo(
                "Configuration stage complete; no wallet action selected. "
                "Wallet and nodes unchanged."
            )
            return
        wallet_action = WalletAction(
            typer.prompt(
                "Wallet action (existing/generate/import)",
                default=WalletAction.EXISTING.value,
                type=Choice([item.value for item in WalletAction]),
            )
        )
    selected_wallet = _wallet_stage(
        path,
        planned,
        wallet_action,
        wallet_file=wallet_file,
        password_file=wallet_password_file,
        import_file=import_mnemonic_file,
        resume=resume_wallet_publication,
        confirm_import_recovery=confirm_import_recovery,
        show_seed=show_generated_seed,
        interactive=not yes,
        data_dir=data_dir,
    )
    create = create_managed_nodes
    if create is None and planned.lightning.mode == "managed" and not yes:
        create = typer.confirm(
            "Create fresh managed node identities (not restore lost nodes)?", default=False
        )
    if not create:
        if install_lnd:
            typer.echo(
                "--install-lnd requires --create-managed-nodes here; "
                "use jm-lightning install-lnd separately for installation only.",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo("Wallet stage complete; Lightning provisioning was not performed.")
        return
    _managed_node_stage(
        path,
        selected_wallet,
        data_dir=data_dir,
        install_lnd=install_lnd,
        consumers_excluded=applications_quiesced,
        passphrase_file=bip39_passphrase_file,
        no_passphrase=no_bip39_passphrase,
        interactive=not yes,
    )


def _wallet_password(path: Path | None, *, interactive: bool, confirm: bool) -> SecretStr:
    if path is not None:
        value = read_private_file(path.expanduser()).decode("utf-8").rstrip("\r\n")
    elif interactive:
        value = typer.prompt("Wallet encryption password", hide_input=True, show_default=False)
        if confirm and value != typer.prompt("Confirm wallet encryption password", hide_input=True):
            raise ValueError("password confirmation differs")
    else:
        raise ValueError("provide a wallet password file")
    if not value:
        raise ValueError("wallet password must not be empty")
    return SecretStr(value)


def _wallet_stage(
    config_path: Path,
    settings: JoinMarketSettings,
    action: WalletAction,
    *,
    wallet_file: Path | None,
    password_file: Path | None,
    import_file: Path | None,
    resume: bool,
    confirm_import_recovery: bool,
    show_seed: bool,
    interactive: bool,
    data_dir: Path | None,
) -> _SelectedWallet:
    from jmwallet.cli.mnemonic import decrypt_mnemonic, validate_mnemonic
    from jmwallet.setup_wallet import SetupWalletError, publish_setup_wallet

    # Configured relative paths retain ordinary wallet-loader semantics (CWD),
    # not an invented config-relative location. Environment seed overrides are
    # deliberately not used to select the wallet in this explicit setup stage.
    selected = wallet_file or (
        Path(settings.wallet.mnemonic_file)
        if settings.wallet.mnemonic_file
        else (data_dir.expanduser() if data_dir is not None else settings.get_data_dir())
        / "wallets"
        / "default.mnemonic"
    )
    # Only CLI paths expand ~, just as ordinary explicit CLI path selection.
    selected = (selected.expanduser() if wallet_file is not None else selected).absolute()
    published = False
    config_commit_started = False
    generated = False
    newly_created = False
    phrase: SecretStr | None = None
    try:
        target = config_path.resolve(strict=False)
        original = _contents(config_path)
        if original is None:
            raise ValueError("configuration disappeared")
        candidate = tomllib.loads(original.decode("utf-8"))
        if validate_config_document(candidate).model_dump() != settings.model_dump():
            raise ConfigFileError("configuration changed between setup stages")
        if action is WalletAction.EXISTING and (
            resume or import_file is not None or confirm_import_recovery
        ):
            raise ValueError("existing wallet selection cannot resume or import")
        if action is not WalletAction.IMPORT and (
            import_file is not None or confirm_import_recovery
        ):
            raise ValueError("import options require explicit import selection")
        if confirm_import_recovery and not resume:
            raise ValueError("recovery confirmation only applies to an interrupted import")
        if resume and import_file is not None:
            raise ValueError("resume uses the published wallet, not a new import source")

        configured = settings.wallet.mnemonic_file
        different_wallet = action is not WalletAction.EXISTING or (
            configured is None or selected != Path(configured).absolute()
        )
        if different_wallet and (
            settings.wallet.mnemonic_password is not None
            or settings.wallet.bip39_passphrase is not None
        ):
            typer.echo(
                "Wallet selection blocked: explicitly resolve configured mnemonic_password and "
                "bip39_passphrase settings before switching wallets; no wallet was created.",
                err=True,
            )
            raise typer.Exit(1)

        candidate.setdefault("wallet", {})["mnemonic_file"] = str(selected)
        validate_config_document(candidate)

        if action is WalletAction.EXISTING:
            contents = read_sensitive_file(selected)
            try:
                plaintext = contents.decode("utf-8").strip()
            except UnicodeDecodeError:
                plaintext = ""
            if validate_mnemonic(plaintext):
                phrase = SecretStr(plaintext)
                typer.echo("WARNING: Existing wallet is plaintext; it was not rewritten.")
            else:
                password = (
                    _wallet_password(password_file, interactive=interactive, confirm=False)
                    if password_file is not None or settings.wallet.mnemonic_password is None
                    else settings.wallet.mnemonic_password
                )
                phrase = SecretStr(decrypt_mnemonic(contents, password.get_secret_value()))
                if not validate_mnemonic(phrase.get_secret_value()):
                    raise ValueError("invalid mnemonic")
        else:
            password = _wallet_password(password_file, interactive=interactive, confirm=not resume)
            imported = None
            if action is WalletAction.IMPORT and not resume:
                if import_file is not None:
                    words = read_private_file(import_file.expanduser()).decode("utf-8")
                elif interactive:
                    words = typer.prompt("BIP39 words to import (hidden)", hide_input=True)
                else:
                    raise ValueError("explicit import requires a source file")
                imported = SecretStr(" ".join(words.split()))
            result = publish_setup_wallet(
                selected,
                password,
                operation="generate" if action is WalletAction.GENERATE else "import",
                mnemonic=imported,
                resume=resume,
                confirm_import_recovery=confirm_import_recovery,
            )
            published = True
            if not result.complete:
                typer.echo(
                    "Wallet publication exists but setup metadata is incomplete. "
                    "Retain files and journal; no wallet selection or seed disclosure performed.",
                    err=True,
                )
                raise typer.Exit(1)
            phrase = result.mnemonic
            generated = result.origin == "generated"
            newly_created = result.newly_created

        if config_path.resolve(strict=False) != target or _contents(config_path) != original:
            raise ConfigFileError("configuration changed during wallet setup")
        config_commit_started = True
        update_config_values(
            config_path,
            {"wallet": {"mnemonic_file": str(selected)}},
            validate=validate_config_document,
        )
        committed = _contents(config_path)
        if (
            config_path.resolve(strict=False) != target
            or committed is None
            or (
                validate_config_document(tomllib.loads(committed.decode("utf-8"))).model_dump()
                != validate_config_document(candidate).model_dump()
            )
        ):
            raise ConfigFileError("configuration changed after wallet selection")
    except (OSError, RuntimeError, ValueError, ConfigFileError, SetupWalletError):
        if config_commit_started:
            message = (
                "Wallet verified; configuration update could not be confirmed. "
                "Inspect configuration; wallet contents are preserved."
            )
        elif published:
            message = (
                "Wallet saved; configuration selection was not updated. Retain wallet and journal; "
                "inspect or explicitly resume, never regenerate."
            )
        elif action is not WalletAction.EXISTING:
            message = (
                "Wallet publication may exist; retain files and journal. Configuration selection "
                "was not updated; inspect or explicitly resume, never regenerate."
            )
        else:
            message = (
                "Existing wallet could not be verified; wallet metadata and configuration "
                "selection unchanged."
            )
        typer.echo(message, err=True)
        raise typer.Exit(1) from None
    typer.echo(
        "Wallet verified and selected. No recovery scan, identity registration, "
        "or node creation performed."
    )
    if generated:
        disclose = show_seed
        if not disclose and interactive and sys.stdin.isatty() and sys.stdout.isatty():
            disclose = typer.confirm(
                "Display seed for private backup? Stop recordings and screen sharing first",
                default=False,
            )
        if disclose:
            assert phrase is not None
            typer.echo("PRIVATE BACKUP: never share or include these words in recordings.")
            typer.echo(phrase.get_secret_value())
        else:
            typer.echo(
                "Seed not displayed. Encrypted wallet preserved; "
                "private seed backup remains pending."
            )
    assert phrase is not None
    return _SelectedWallet(
        selected, phrase, newly_created, target, hashlib.sha256(committed).hexdigest()
    )


def _managed_node_stage(
    config_path: Path,
    wallet: _SelectedWallet,
    *,
    data_dir: Path | None,
    install_lnd: bool,
    consumers_excluded: bool,
    passphrase_file: Path | None,
    no_passphrase: bool,
    interactive: bool,
) -> None:
    try:
        from jmwallet.setup_lightning import SetupLightningError, initialize_managed_nodes
    except ModuleNotFoundError as exc:
        if exc.name is None or not (
            exc.name in {"jmwallet.setup_lightning", "jmswap"} or exc.name.startswith("jmswap.")
        ):
            raise
        typer.echo(
            "Managed setup requires the jm-lightning component in this Python installation. "
            "Wallet preserved; no node initialization performed.",
            err=True,
        )
        raise typer.Exit(1) from None

    try:
        document = _contents(config_path)
        if (
            document is None
            or config_path.resolve(strict=False) != wallet.config_target
            or (hashlib.sha256(document).hexdigest() != wallet.config_digest)
        ):
            raise SetupLightningError(
                "Configuration changed after wallet selection; inspect before initialization"
            )
        settings = validate_config_document(tomllib.loads(document.decode("utf-8")))
        if settings.lightning.mode != "managed" or settings.wallet.address_type != "p2tr":
            raise SetupLightningError(
                "Node creation requires explicit managed Lightning and Taproot"
            )
        if not consumers_excluded and interactive:
            consumers_excluded = typer.confirm(
                "Have you excluded node consumers and automatic restarts "
                "until initialization finishes? "
                "Keep existing settlement monitors running",
                default=False,
            )
        if not consumers_excluded:
            raise SetupLightningError(
                "Affirm --applications-quiesced; "
                "initialization does not stop or drain other applications"
            )
        if passphrase_file is not None and no_passphrase:
            raise SetupLightningError(
                "Choose a BIP39 passphrase file or explicit no-passphrase, not both"
            )
        if passphrase_file is not None:
            passphrase = SecretStr(
                read_private_file(passphrase_file.expanduser()).decode("utf-8").rstrip("\r\n")
            )
        elif no_passphrase:
            if (
                settings.wallet.bip39_passphrase is not None
                and settings.wallet.bip39_passphrase.get_secret_value()
            ):
                raise SetupLightningError(
                    "Resolve the configured BIP39 passphrase before selecting no passphrase"
                )
            passphrase = SecretStr("")
        elif settings.wallet.bip39_passphrase is not None:
            passphrase = settings.wallet.bip39_passphrase
        elif interactive:
            passphrase = SecretStr(
                typer.prompt(
                    "BIP39 passphrase for node identities (hidden; empty means none)",
                    default="",
                    show_default=False,
                    hide_input=True,
                )
            )
        else:
            raise SetupLightningError(
                "Select --bip39-passphrase-file or --no-bip39-passphrase explicitly"
            )
        if (
            settings.wallet.bip39_passphrase is not None
            and passphrase != settings.wallet.bip39_passphrase
        ):
            raise SetupLightningError(
                "Resolve the configured BIP39 passphrase before choosing a different value"
            )
        if (
            "BIP39_PASSPHRASE" in os.environ
            and os.environ["BIP39_PASSPHRASE"] != passphrase.get_secret_value()
        ):
            raise SetupLightningError(
                "Resolve the conflicting BIP39_PASSPHRASE environment override "
                "before initialization"
            )
        if "EXPECTED_FINGERPRINT" in os.environ:
            from jmcore.wallet_metadata import WalletIdentity

            from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

            expected = WalletIdentity.validate_fingerprint(os.environ["EXPECTED_FINGERPRINT"])
            if (
                get_mnemonic_fingerprint(
                    wallet.mnemonic.get_secret_value(), passphrase.get_secret_value()
                )
                != expected
            ):
                raise SetupLightningError(
                    "Selected wallet does not match EXPECTED_FINGERPRINT; "
                    "resolve staged credentials"
                )
        if (
            _contents(config_path) != document
            or config_path.resolve(strict=False) != wallet.config_target
        ):
            raise SetupLightningError(
                "Configuration changed while choosing node options; inspect it"
            )
        actual_data_dir = (
            (data_dir if data_dir is not None else settings.get_data_dir())
            .expanduser()
            .resolve(strict=False)
        )
        typer.echo("Checking prerequisites for explicit fresh-node initialization.")
        registry = initialize_managed_nodes(
            settings,
            wallet.mnemonic,
            passphrase,
            wallet_file=wallet.path,
            data_dir=actual_data_dir,
            consumers_excluded=consumers_excluded,
            install_lnd=install_lnd,
        )
        if (
            _contents(config_path) != document
            or config_path.resolve(strict=False) != wallet.config_target
        ):
            raise SetupLightningError(
                "Nodes were initialized but configuration changed; "
                "inspect before starting consumers"
            )
    except (SetupLightningError, OSError, RuntimeError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, SetupLightningError)
            else ("Node initialization could not be verified; inspect prerequisites.")
        )
        typer.echo(message, err=True)
        typer.echo(
            "Wallet remains selected; preserve any partial node state for inspection or recovery.",
            err=True,
        )
        typer.echo(
            "Keep new-node consumers and automatic restarts excluded until recovery completes; "
            "retain existing settlement monitors.",
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(f"Initialized {len(registry.nodes)} managed nodes; they are not running.")
    command = [
        str(Path(sys.executable).with_name("jm-lightning")),
        "run",
        "--data-dir",
        str(actual_data_dir.expanduser().absolute()),
        "--config-file",
        str(config_path),
        "--mnemonic-file",
        str(wallet.path),
    ]
    if passphrase.get_secret_value():
        command.append("--prompt-bip39-passphrase")
    typer.echo("Start the persistent supervisor separately (keep it running):")
    typer.echo(shlex.join(command))
    typer.echo(
        "LND supervision does not replace application settlement monitors. "
        "No markets were activated."
    )


def main(args: list[str] | None = None) -> None:
    app(args=args, prog_name="jm-ng setup")


if __name__ == "__main__":
    main()
