"""Offline PSBT inspection and signing command."""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Annotated

import typer
from jmcore.bitcoin import scriptpubkey_to_address
from jmcore.cli_common import resolve_mnemonic, setup_cli
from jmcore.models import NetworkType
from loguru import logger

from jmwallet.backends.offline import OfflineBackend
from jmwallet.cli import app
from jmwallet.wallet.psbt import PSBT_MAGIC, PSBTError
from jmwallet.wallet.psbt_signer import PSBTSigningPlan
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import TransactionSigningError
from jmwallet.wallet.spend import ExcessiveFeeRateError, enforce_fee_rate_cap

MAX_PSBT_SIZE = 10 * 1024 * 1024


def _decode_psbt_text(text: str) -> bytes:
    compact = "".join(text.split())
    if not compact:
        raise ValueError("PSBT input is empty")
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid base64 PSBT: {exc}") from exc
    return raw


def _load_psbt(psbt_base64: str | None, input_file: Path | None) -> bytes:
    if psbt_base64 is not None and input_file is not None:
        raise ValueError("Provide either a base64 PSBT argument or --input, not both")
    if psbt_base64 is None and input_file is None:
        raise ValueError("Provide a base64 PSBT argument or --input")

    if input_file is not None:
        try:
            file_data = input_file.read_bytes()
        except OSError as exc:
            raise ValueError(f"Could not read PSBT file {input_file}: {exc}") from exc
        if len(file_data) > MAX_PSBT_SIZE:
            raise ValueError(f"PSBT exceeds the {MAX_PSBT_SIZE:,}-byte size limit")
        if file_data.startswith(PSBT_MAGIC):
            raw = file_data
        else:
            try:
                raw = _decode_psbt_text(file_data.decode("ascii"))
            except UnicodeDecodeError as exc:
                raise ValueError("PSBT file is neither binary PSBT nor ASCII base64") from exc
    else:
        assert psbt_base64 is not None
        raw = _decode_psbt_text(psbt_base64)

    if len(raw) > MAX_PSBT_SIZE:
        raise ValueError(f"PSBT exceeds the {MAX_PSBT_SIZE:,}-byte size limit")
    return raw


def _display_plan(plan: PSBTSigningPlan, network: str) -> None:
    transaction = plan.psbt.transaction
    typer.echo("\nPSBT REVIEW")
    typer.echo("=" * 80)
    typer.echo(f"Version:           {transaction.version}")
    typer.echo(f"Locktime:          {transaction.locktime}")
    typer.echo(f"Inputs:            {len(transaction.inputs)}")
    for input_plan, tx_input in zip(plan.inputs, transaction.inputs, strict=True):
        if input_plan.wallet_input_type == "fidelity-bond":
            ownership = "WALLET FIDELITY BOND"
        elif input_plan.wallet_input_type == "regular":
            ownership = "WALLET REGULAR"
        else:
            ownership = "FOREIGN OR UNSUPPORTED"
        state = (
            "finalized"
            if input_plan.finalized
            else "already signed"
            if input_plan.already_signed
            else ""
        )
        state_suffix = f", {state}" if state else ""
        typer.echo(
            f"  [{input_plan.index}] {tx_input.txid}:{tx_input.vout}  "
            f"{input_plan.witness_utxo.value:,} sats  [{ownership}{state_suffix}]"
        )

    typer.echo(f"Outputs:           {len(transaction.outputs)}")
    for index, output in enumerate(transaction.outputs):
        try:
            destination = scriptpubkey_to_address(output.script, network)
        except ValueError:
            destination = f"script:{output.script.hex()}"
        typer.echo(f"  [{index}] {output.value:,} sats  {destination}")

    typer.echo(f"Fee:               {plan.fee:,} sats")
    if plan.fee_rate_is_upper_bound:
        typer.echo(f"Minimum vsize:     {plan.estimated_vsize:,} vB")
        typer.echo(f"Fee rate upper bound:{plan.estimated_fee_rate:9.2f} sat/vB")
        typer.echo("Foreign P2WSH witness sizes are unknown; the fee cap uses this upper bound.")
    else:
        typer.echo(f"Estimated vsize:   {plan.estimated_vsize:,} vB")
        typer.echo(f"Estimated fee rate:{plan.estimated_fee_rate:9.2f} sat/vB")
    typer.echo(f"Wallet inputs:     {plan.owned_count}")
    typer.echo(f"Inputs to sign:    {plan.signable_count}")
    typer.echo("=" * 80)


def _write_result(signed_psbt: bytes, output_file: Path | None) -> None:
    encoded = base64.b64encode(signed_psbt).decode("ascii")
    if output_file is not None:
        try:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text(encoded + "\n")
        except OSError as exc:
            raise ValueError(f"Could not write signed PSBT to {output_file}: {exc}") from exc
        typer.echo(f"Signed PSBT saved to: {output_file}")
        return
    typer.echo("\nSigned PSBT (base64):")
    typer.echo(encoded)


@app.command("sign-psbt", no_args_is_help=True)
def sign_psbt(
    psbt_base64: Annotated[str | None, typer.Argument(help="Base64-encoded PSBT v0 or v2")] = None,
    input_file: Annotated[
        Path | None,
        typer.Option("--input", "-i", help="Read a binary or base64 PSBT from a file"),
    ] = None,
    output_file: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the signed base64 PSBT to a file"),
    ] = None,
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", envvar="MNEMONIC_FILE")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase")
    ] = False,
    network: Annotated[str | None, typer.Option("--network", "-n", help="Bitcoin network")] = None,
    scan_range: Annotated[
        int | None,
        typer.Option(
            "--scan-range",
            min=0,
            max=1_000_000,
            help="Fallback addresses per branch to derive when PSBT key origins are absent",
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Sign without the confirmation prompt")
    ] = False,
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
            help="Config file path (default: <data-dir>/config.toml)",
        ),
    ] = None,
    log_level: Annotated[str | None, typer.Option("--log-level", "-l", help="Log level")] = None,
) -> None:
    """Inspect and partially sign wallet-owned native SegWit PSBT inputs offline.

    Supports regular P2WPKH wallet inputs and canonical JoinMarket fidelity bond
    P2WSH inputs, while leaving unrelated P2WSH inputs unsigned. Every input must
    include witness_utxo data so the complete fee can be reviewed. Accepts PSBT
    v0 and v2. The command never connects to a backend or broadcasts.
    """
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)
    try:
        raw_psbt = _load_psbt(psbt_base64, input_file)
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        if resolved is None:
            raise ValueError("No mnemonic provided")

        resolved_network = NetworkType(network or settings.network_config.network.value).value
        resolved_scan_range = settings.wallet.scan_range if scan_range is None else scan_range
        wallet = WalletService(
            mnemonic=resolved.mnemonic,
            backend=OfflineBackend(),
            network=resolved_network,
            mixdepth_count=settings.wallet.mixdepth_count,
            scan_range=resolved_scan_range,
            passphrase=resolved.bip39_passphrase,
        )
        plan = wallet.prepare_psbt_signing(raw_psbt, resolved_scan_range)
        if plan.owned_count == 0:
            raise ValueError("The PSBT contains no inputs owned by this wallet")
        if plan.fee > 0:
            enforce_fee_rate_cap(
                plan.estimated_fee_rate,
                settings.wallet.max_fee_rate_sat_vb,
                source="PSBT upper bound" if plan.fee_rate_is_upper_bound else "PSBT estimated",
            )
        else:
            logger.warning("PSBT pays a zero fee and may not be relayable")

        _display_plan(plan, resolved_network)
        if plan.signable_count > 0 and not yes:
            if not typer.confirm("Sign the wallet-owned inputs shown above?", default=False):
                typer.echo("Signing cancelled")
                raise typer.Exit(1)

        result = wallet.sign_psbt(plan)
        _write_result(result.psbt, output_file)
        typer.echo(
            f"Signed {len(result.signed_indices)} input(s); "
            f"{len(result.already_signed_indices)} already had valid wallet signatures."
        )
    except (
        ExcessiveFeeRateError,
        FileNotFoundError,
        PSBTError,
        TransactionSigningError,
        ValueError,
    ) as exc:
        logger.error(str(exc))
        raise typer.Exit(1) from exc


# Compatibility with joinmarket-clientserver's historical command spelling.
app.command("signpsbt", hidden=True)(sign_psbt)
