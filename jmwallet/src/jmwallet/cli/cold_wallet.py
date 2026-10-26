"""
Cold wallet workflow: create-bond-address, generate-hot-keypair,
prepare-certificate-message, import-certificate, spend-bond
+ crypto verification helpers.
"""

from __future__ import annotations

import base64
import json
import math
from datetime import datetime
from pathlib import Path
from secrets import token_bytes
from typing import Annotated

import typer
from bitcointx.core.key import CKey, CPubKey
from jmcore.cli_common import resolve_backend_settings, setup_cli
from jmcore.constants import SECP256K1_N
from jmcore.secure_files import atomic_write_private, ensure_sensitive_directory
from loguru import logger

from jmwallet.cli import app

_WALLET_FINGERPRINT_HELP = (
    "8-char hex master key fingerprint of the JoinMarket wallet that will operate "
    "this bond. Run 'jm-wallet info --mnemonic-file <wallet>' on the hot wallet "
    "to look it up. Required because each wallet has its own bond registry "
    "(fidelity_bonds_<fp>.json) under the shared data directory."
)


def _generate_private_key() -> CKey:
    """Generate a valid compressed secp256k1 private key."""
    while True:
        try:
            return CKey.from_secret_bytes(token_bytes(32))
        except ValueError:
            continue


def _resolve_wallet_fingerprint(wallet_fingerprint: str | None) -> str:
    """Validate and return the wallet fingerprint, or exit with an error."""
    if not wallet_fingerprint:
        logger.error("--wallet-fingerprint is required to select the per-wallet bond registry.")
        logger.info(
            "Run 'jm-wallet info --mnemonic-file <wallet>' on the JoinMarket wallet "
            "that will operate this bond to look up its 8-char hex master fingerprint, "
            "then pass it via --wallet-fingerprint <fp>."
        )
        raise typer.Exit(1)
    fp = wallet_fingerprint.strip().lower()
    if len(fp) != 8:
        logger.error("--wallet-fingerprint must be exactly 8 hex chars")
        logger.bind(sensitive=True).error(
            f"--wallet-fingerprint must be exactly 8 hex chars, got {len(fp)}: {fp!r}"
        )
        raise typer.Exit(1)
    try:
        bytes.fromhex(fp)
    except ValueError:
        logger.error("--wallet-fingerprint must be valid hex")
        logger.bind(sensitive=True).error(f"--wallet-fingerprint must be valid hex, got {fp!r}")
        raise typer.Exit(1)
    return fp


@app.command("create-bond-address", no_args_is_help=True)
def create_bond_address(
    pubkey: Annotated[str, typer.Argument(help="Public key (hex, 33 bytes compressed)")],
    locktime: Annotated[
        int, typer.Option("--locktime", "-L", help="Locktime as Unix timestamp")
    ] = 0,
    locktime_date: Annotated[
        str | None,
        typer.Option(
            "--locktime-date", "-d", help="Locktime as date (YYYY-MM, must be 1st of month)"
        ),
    ] = None,
    network: Annotated[str, typer.Option("--network", "-n")] = "mainnet",
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    no_save: Annotated[
        bool,
        typer.Option("--no-save", help="Do not save the bond to the registry"),
    ] = False,
    allow_expired: Annotated[
        bool,
        typer.Option(
            "--allow-expired",
            help="Allow a past/current locktime for recovery or testing only",
        ),
    ] = False,
    wallet_fingerprint: Annotated[
        str | None,
        typer.Option("--wallet-fingerprint", help=_WALLET_FINGERPRINT_HELP),
    ] = None,
    log_level: Annotated[str, typer.Option("--log-level", "-l")] = "INFO",
) -> None:
    """
    Create a fidelity bond address from a public key (cold wallet workflow).

    This command creates a timelocked P2WSH bond address from a public key WITHOUT
    requiring your mnemonic or private keys. Use this for true cold storage security.

    Use a standard receive or change key supported by your external signer and
    record its derivation path and master fingerprint for redemption. Test the
    complete synthetic spend workflow before funding the bond.

    Your hardware wallet never needs to be connected to this online tool.
    See docs/fidelity-bond-operations.md for the maintained workflow.
    """
    setup_cli(log_level, data_dir=data_dir)

    # Strip wpkh() wrapper if present (Sparrow copies pubkey as "wpkh(03abcd...)")
    pubkey = pubkey.strip()
    if pubkey.startswith("wpkh(") and pubkey.endswith(")"):
        pubkey = pubkey[5:-1]
        logger.info("Stripped wpkh() wrapper from public key")

    # Validate pubkey
    try:
        pubkey_bytes = bytes.fromhex(pubkey)
        if len(pubkey_bytes) != 33:
            raise ValueError("Public key must be 33 bytes (compressed)")
        # Verify it's a valid compressed pubkey (starts with 02 or 03)
        if pubkey_bytes[0] not in (0x02, 0x03):
            raise ValueError("Invalid compressed public key format")
        if not CPubKey(pubkey_bytes).is_fullyvalid():
            raise ValueError("Invalid compressed public key")
    except ValueError as e:
        logger.error(f"Invalid public key: {e}")
        raise typer.Exit(1)

    # Parse locktime
    from jmcore.timenumber import is_valid_locktime, parse_locktime_date

    if locktime_date:
        try:
            locktime = parse_locktime_date(locktime_date)
        except ValueError as e:
            logger.error(f"Invalid locktime date: {e}")
            logger.info("Use format: YYYY-MM or YYYY-MM-DD (must be 1st of month)")
            logger.info("Valid range: 2020-01 to 2099-12")
            raise typer.Exit(1)

    if locktime <= 0:
        logger.error("Locktime is required. Use --locktime or --locktime-date")
        raise typer.Exit(1)

    # Validate locktime is a valid timenumber (1st of month, midnight UTC)
    if not is_valid_locktime(locktime):
        from jmcore.timenumber import get_nearest_valid_locktime

        suggested = get_nearest_valid_locktime(locktime, round_up=True)
        suggested_dt = datetime.fromtimestamp(suggested)
        logger.warning(
            f"Locktime {locktime} is not a valid fidelity bond locktime "
            f"(must be 1st of month at midnight UTC)"
        )
        logger.info(f"Suggested locktime: {suggested} ({suggested_dt.strftime('%Y-%m-%d')})")
        logger.info("Use --locktime-date YYYY-MM for correct format")
        raise typer.Exit(1)

    # Normal creation must not produce an immediately spendable bond. Recovery
    # and deterministic test vectors can opt in explicitly.
    if locktime <= datetime.now().timestamp():
        if not allow_expired:
            logger.error("Locktime must be in the future for normal bond creation")
            logger.info("Use --allow-expired only for recovery or testing")
            raise typer.Exit(1)
        logger.warning(
            "Using a past/current locktime in explicit recovery/testing mode; "
            "the bond is immediately spendable"
        )

    from jmcore.btc_script import disassemble_script, mk_freeze_script
    from jmcore.paths import get_default_data_dir

    from jmwallet.wallet.address import script_to_p2wsh_address
    from jmwallet.wallet.bond_registry import (
        create_bond_info,
        get_registry_path,
        load_registry,
        save_registry,
    )

    # Resolve wallet fingerprint up front so we fail fast even when --no-save is set,
    # since cold-wallet bonds are external (path="external") and the fingerprint
    # cannot be derived later from any key material.
    resolved_fingerprint = _resolve_wallet_fingerprint(wallet_fingerprint)

    # Create the witness script from the public key
    witness_script = mk_freeze_script(pubkey, locktime)
    address = script_to_p2wsh_address(witness_script, network)

    locktime_dt = datetime.fromtimestamp(locktime)
    disassembled = disassemble_script(witness_script)

    # Resolve data directory
    resolved_data_dir = data_dir if data_dir else get_default_data_dir()

    # Save to registry unless --no-save
    saved = False
    existing = False
    if not no_save:
        registry = load_registry(resolved_data_dir, resolved_fingerprint)
        existing_bond = registry.get_bond_by_address(address)
        if existing_bond:
            existing = True
            logger.info(f"Bond already exists in registry (created: {existing_bond.created_at})")
        else:
            # For bonds created from pubkey, we don't have the derivation path or index
            # So we use placeholder values
            bond_info = create_bond_info(
                address=address,
                locktime=locktime,
                index=-1,  # Unknown index for pubkey-based bonds
                path="external",  # Path is unknown when created from pubkey
                pubkey_hex=pubkey,
                witness_script=witness_script,
                network=network,
            )
            registry.add_bond(bond_info)
            save_registry(registry, resolved_data_dir, resolved_fingerprint)
            saved = True

    # Compute the underlying P2WPKH address for the pubkey (for user confirmation)
    from jmwallet.wallet.address import pubkey_to_p2wpkh_address

    p2wpkh_address = pubkey_to_p2wpkh_address(bytes.fromhex(pubkey), network)

    print("\n" + "=" * 80)
    print("FIDELITY BOND ADDRESS (created from public key)")
    print("=" * 80)
    print(f"\nBond Address (P2WSH):  {address}")
    print(f"Signing Address:       {p2wpkh_address}")
    print("  (Use this address in Sparrow to sign messages)")
    print(f"Locktime:              {locktime} ({locktime_dt.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"Network:               {network}")
    print(f"Public Key:            {pubkey}")
    print()
    print("-" * 80)
    print("WITNESS SCRIPT (redeemScript)")
    print("-" * 80)
    print(f"Hex:          {witness_script.hex()}")
    print(f"Disassembled: {disassembled}")
    print("-" * 80)
    if saved:
        print(f"\nSaved to registry: {get_registry_path(resolved_data_dir, resolved_fingerprint)}")
    elif existing:
        print("\nBond already in registry (not updated)")
    elif no_save:
        print("\nNot saved to registry (--no-save)")
    print("\n" + "=" * 80)
    print("HOW TO GET PUBLIC KEY FROM SPARROW WALLET:")
    print("=" * 80)
    print("  1. Open Sparrow Wallet and connect your hardware wallet")
    print("  2. Go to Addresses tab")
    print("  3. Choose any address from the Deposit (m/84'/0'/0'/0/x) or")
    print("     Change (m/84'/0'/0'/1/x) account - use index 0 for simplicity")
    print("  4. Right-click the address and select 'Copy Public Key'")
    print("     If Sparrow copies it as 'wpkh(03abcd...)', that's fine -")
    print("     this command strips the wpkh() wrapper automatically")
    print("  5. Note the DERIVATION PATH: double-click the address (or click the")
    print("     receive arrow) to see it (e.g., m/84'/0'/0'/0/0)")
    print("  6. Note the MASTER FINGERPRINT: go to Settings (bottom-left) ->")
    print("     Keystores section (e.g., aabbccdd)")
    print("  -> You will need both when running 'spend-bond' later")
    print()
    print("NOTE: The /2 fidelity bond derivation path is NOT available in Sparrow.")
    print("      Using /0 (deposit) or /1 (change) addresses works fine.")
    print()
    print("IMPORTANT:")
    print("  - Funds sent to the Bond Address are LOCKED until the locktime!")
    print("  - Remember which address you used for the bond's public key")
    print("  - Your private keys never leave the hardware wallet")
    print("=" * 80 + "\n")


@app.command("import-bond-registration", no_args_is_help=True)
def import_bond_registration(
    registration: Annotated[
        str | None,
        typer.Argument(help="Canonical SeedSigner BIP46 registration JSON"),
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option("--file", help="File containing canonical SeedSigner BIP46 registration JSON"),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    wallet_fingerprint: Annotated[
        str | None,
        typer.Option("--wallet-fingerprint", help=_WALLET_FINGERPRINT_HELP),
    ] = None,
    log_level: Annotated[str, typer.Option("--log-level", "-l")] = "INFO",
) -> None:
    """Import a verified SeedSigner BIP46 fidelity-bond registration payload."""
    setup_cli(log_level, data_dir=data_dir)

    if (registration is None) == (file is None):
        logger.error("Provide exactly one inline registration JSON argument or --file")
        raise typer.Exit(1)

    if file is not None:
        try:
            registration = file.read_bytes().decode("ascii").strip()
        except (OSError, UnicodeDecodeError) as exc:
            logger.error(f"Cannot read ASCII registration file: {exc}")
            raise typer.Exit(1) from exc

    assert registration is not None
    from jmwallet.wallet.bond_registration import (
        BondRegistrationError,
        parse_bip46_registration_payload,
    )

    try:
        imported = parse_bip46_registration_payload(registration)
    except BondRegistrationError as exc:
        logger.error(f"Invalid SeedSigner BIP46 registration: {exc}")
        raise typer.Exit(1) from exc

    from jmcore.paths import get_default_data_dir

    from jmwallet.wallet.bond_registry import (
        create_bond_info,
        get_registry_path,
        load_registry,
        save_registry,
    )

    resolved_data_dir = data_dir if data_dir else get_default_data_dir()
    resolved_fingerprint = _resolve_wallet_fingerprint(wallet_fingerprint)
    try:
        registry = load_registry(
            resolved_data_dir,
            resolved_fingerprint,
            allow_legacy_fallback=False,
            fail_closed=True,
        )
    except ValueError as exc:
        logger.error(f"Cannot import into bond registry: {exc}")
        raise typer.Exit(1) from exc

    imported_bond = create_bond_info(
        address=imported.address,
        locktime=imported.locktime,
        index=-1,
        path=imported.derivation_path,
        pubkey_hex=imported.pubkey,
        witness_script=imported.witness_script,
        network=imported.network,
        signer_master_fingerprint=imported.master_fingerprint,
    )
    existing_bond = registry.get_bond_by_address(imported.address)
    if existing_bond is None:
        registry.add_bond(imported_bond)
        result = "Imported"
    else:
        # Preserve observed UTXOs, certificate material, and creation time.
        existing_bond.address = imported_bond.address
        existing_bond.locktime = imported_bond.locktime
        existing_bond.locktime_human = imported_bond.locktime_human
        existing_bond.index = imported_bond.index
        existing_bond.path = imported_bond.path
        existing_bond.pubkey = imported_bond.pubkey
        existing_bond.witness_script_hex = imported_bond.witness_script_hex
        existing_bond.network = imported_bond.network
        existing_bond.signer_master_fingerprint = imported_bond.signer_master_fingerprint
        result = "Updated existing"

    save_registry(registry, resolved_data_dir, resolved_fingerprint)
    print(f"{result} SeedSigner BIP46 bond registration: {imported.address}")
    print(f"Registry: {get_registry_path(resolved_data_dir, resolved_fingerprint)}")


@app.command("generate-hot-keypair")
def generate_hot_keypair(
    bond_address: Annotated[
        str | None,
        typer.Option(
            "--bond-address",
            help="Bond address to associate keypair with (saves to registry)",
        ),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    wallet_fingerprint: Annotated[
        str | None,
        typer.Option("--wallet-fingerprint", help=_WALLET_FINGERPRINT_HELP),
    ] = None,
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
) -> None:
    """
    Generate a hot wallet keypair for fidelity bond certificates.

    This generates a random keypair that will be used for signing nick messages
    in the fidelity bond proof. The private key stays in the hot wallet, while
    the public key is used to create a certificate signed by the cold wallet.

    The certificate chain is:
      UTXO keypair (cold) -> signs -> certificate (hot) -> signs -> nick proofs

    If --bond-address is provided, the keypair is saved to the bond registry
    and will be automatically used when importing the certificate.

    SECURITY:
    - The hot wallet private key should be stored securely
    - If compromised, an attacker can impersonate your bond through cert expiry
    - But they CANNOT spend your bond funds (those remain in cold storage)
    """
    setup_cli(log_level, data_dir=data_dir)

    from jmcore.paths import get_default_data_dir

    # Generate a random private key
    privkey = _generate_private_key()
    pubkey = bytes(privkey.pub)

    resolved_data_dir = data_dir if data_dir else get_default_data_dir()

    # Optionally save to registry
    saved_to_registry = False
    saved_key_file: Path | None = None
    if bond_address:
        from jmwallet.wallet.bond_registry import load_registry, save_registry

        resolved_fingerprint = _resolve_wallet_fingerprint(wallet_fingerprint)
        registry = load_registry(resolved_data_dir, resolved_fingerprint)
        bond = registry.get_bond_by_address(bond_address)

        if bond:
            bond.cert_pubkey = pubkey.hex()
            bond.cert_privkey = privkey.secret_bytes.hex()
            save_registry(registry, resolved_data_dir, resolved_fingerprint)
            saved_to_registry = True
            logger.info("Saved hot keypair to bond registry")
            logger.bind(sensitive=True).info(
                f"Saved hot keypair to bond registry for {bond_address}"
            )
        else:
            logger.warning("Bond not found in registry")
            logger.bind(sensitive=True).warning(f"Bond not found for address: {bond_address}")
            logger.info("Private key will be written to a local key file")

    if not saved_to_registry:
        ensure_sensitive_directory(resolved_data_dir)
        key_file_name = f"hot_certificate_key_{pubkey.hex()[:16]}.json"
        saved_key_file = resolved_data_dir / key_file_name
        key_content = (
            json.dumps(
                {
                    "cert_pubkey": pubkey.hex(),
                    "cert_privkey": privkey.secret_bytes.hex(),
                },
                indent=2,
            )
            + "\n"
        )
        atomic_write_private(saved_key_file, key_content.encode("utf-8"))
        logger.info("Wrote hot keypair with mode 0600")
        logger.bind(sensitive=True).info(f"Wrote hot keypair to {saved_key_file} with mode 0600")

    print("\n" + "=" * 80)
    print("HOT WALLET KEYPAIR FOR FIDELITY BOND CERTIFICATE")
    print("=" * 80)
    print(f"Public Key (hex):  {pubkey.hex()}")
    if saved_to_registry:
        print(f"\nSaved to bond registry for: {bond_address}")
        print("  (The keypair will be used automatically with import-certificate)")
    elif saved_key_file is not None:
        print(f"\nPrivate key saved to: {saved_key_file}")
        print("  (File permissions set to 0600)")
    print("\n" + "=" * 80)
    print("NEXT STEPS:")
    print("  1. Use the public key with 'prepare-certificate-message'")
    print("  2. Sign the certificate message with your hardware wallet (Sparrow)")
    print("  3. Import the certificate with 'import-certificate'")
    if not saved_to_registry and saved_key_file is not None:
        print("\nNOTE: Keep the key file secure; you will need it for import-certificate.")
    print("\nSECURITY:")
    print("  - This is the HOT wallet key - it will be used to sign nick proofs")
    print("  - If this key is compromised, attacker can impersonate your bond")
    print("  - But your BOND FUNDS remain safe in cold storage!")
    print("=" * 80 + "\n")


@app.command("prepare-certificate-message", no_args_is_help=True)
def prepare_certificate_message(
    bond_address: Annotated[str, typer.Argument(help="Bond P2WSH address")],
    cert_pubkey: Annotated[
        str | None,
        typer.Option("--cert-pubkey", help="Certificate public key (hex)"),
    ] = None,
    validity_periods: Annotated[
        int,
        typer.Option(
            "--validity-periods",
            help="Certificate validity in 2016-block periods from now (1=~2wk, 52=~2yr)",
        ),
    ] = 52,  # ~2 years validity
    data_dir_opt: Annotated[
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
    network: Annotated[str | None, typer.Option("--network", "-n", help="Bitcoin network")] = None,
    backend_type: Annotated[
        str | None,
        typer.Option("--backend", "-b", help="Backend: descriptor_wallet | neutrino"),
    ] = None,
    rpc_url: Annotated[str | None, typer.Option("--rpc-url", envvar="BITCOIN_RPC_URL")] = None,
    neutrino_url: Annotated[
        str | None, typer.Option("--neutrino-url", envvar="NEUTRINO_URL")
    ] = None,
    current_block: Annotated[
        int | None,
        typer.Option(
            "--current-block",
            help=(
                "Current block height override for offline/air-gapped workflows. "
                "Skips all network block-height lookups."
            ),
        ),
    ] = None,
    wallet_fingerprint: Annotated[
        str | None,
        typer.Option("--wallet-fingerprint", help=_WALLET_FINGERPRINT_HELP),
    ] = None,
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
) -> None:
    """
    Prepare certificate message for signing with hardware wallet (cold wallet support).

    This generates the message that needs to be signed by the bond UTXO's private key.
    The message can then be signed using a hardware wallet via tools like Sparrow Wallet.

    IMPORTANT: This command does NOT require your mnemonic or private keys.
    It only prepares the message that you will sign with your hardware wallet.

    If --cert-pubkey is not provided and the bond already has a hot keypair saved
    in the registry (from generate-hot-keypair --bond-address), it will be used.

    The certificate message format for Sparrow is plain ASCII text:
      "fidelity-bond-cert|<cert_pubkey_hex>|<cert_expiry>"

    Where cert_expiry is the ABSOLUTE period number (current_period + validity_periods).
    The value is signed for reference compatibility. Reference and JoinMarket NG
    takers accept the certificate at its boundary block and reject it afterward.
    """
    settings = setup_cli(log_level, data_dir=data_dir_opt, config_file=config_file)

    if validity_periods < 1:
        logger.error("--validity-periods must be at least 1")
        raise typer.Exit(1)

    from jmcore.paths import get_default_data_dir

    from jmwallet.wallet.bond_registry import load_registry

    # Resolve data directory
    data_dir = data_dir_opt if data_dir_opt else get_default_data_dir()
    resolved_fingerprint = _resolve_wallet_fingerprint(wallet_fingerprint)
    registry = load_registry(data_dir, resolved_fingerprint)
    bond = registry.get_bond_by_address(bond_address)

    if not bond:
        logger.error("Bond not found in registry")
        logger.bind(sensitive=True).error(f"Bond not found for address: {bond_address}")
        logger.info("Make sure you have created the bond with 'create-bond-address' first")
        raise typer.Exit(1)

    # Get cert_pubkey from argument or registry
    if not cert_pubkey:
        if bond.cert_pubkey:
            cert_pubkey = bond.cert_pubkey
            logger.info("Using certificate pubkey from bond registry")
        else:
            logger.error("--cert-pubkey is required")
            logger.info(
                "Run 'generate-hot-keypair --bond-address <addr>' first, or provide --cert-pubkey"
            )
            raise typer.Exit(1)

    # Validate cert_pubkey
    try:
        cert_pubkey_bytes = bytes.fromhex(cert_pubkey)
        if len(cert_pubkey_bytes) != 33:
            raise ValueError("Certificate pubkey must be 33 bytes (compressed)")
        if cert_pubkey_bytes[0] not in (0x02, 0x03):
            raise ValueError("Invalid compressed public key format")
        if not CPubKey(cert_pubkey_bytes).is_fullyvalid():
            raise ValueError("Invalid compressed public key")
    except ValueError as e:
        logger.error(f"Invalid certificate pubkey: {e}")
        raise typer.Exit(1)

    # Fetch current block height.
    # Priority: explicit --current-block > configured node backend > --mempool-api.
    import asyncio

    current_block_height: int
    if current_block is not None:
        if current_block < 0:
            logger.error("--current-block must be >= 0")
            raise typer.Exit(1)
        current_block_height = current_block
        logger.info(f"Current block height: {current_block_height} (from --current-block)")
    else:
        backend_settings = resolve_backend_settings(
            settings,
            network=network,
            backend_type=backend_type,
            rpc_url=rpc_url,
            neutrino_url=neutrino_url,
            data_dir=data_dir_opt,
        )

        node_available = bool(backend_settings.rpc_url or backend_settings.neutrino_url)

        if node_available:
            from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
            from jmwallet.backends.neutrino import NeutrinoBackend

            try:
                if backend_settings.backend_type == "neutrino":
                    node_backend: DescriptorWalletBackend | NeutrinoBackend = NeutrinoBackend(
                        neutrino_url=backend_settings.neutrino_url,
                        network=backend_settings.network,
                        tls_cert_path=backend_settings.neutrino_tls_cert,
                        auth_token=backend_settings.neutrino_auth_token,
                    )
                else:
                    node_backend = DescriptorWalletBackend(
                        rpc_url=backend_settings.rpc_url,
                        rpc_user=backend_settings.rpc_user,
                        rpc_password=backend_settings.rpc_password,
                    )
                current_block_height = asyncio.run(node_backend.get_block_height())
                logger.info(f"Current block height: {current_block_height} (from node)")
            except Exception as e:
                logger.error(f"Failed to fetch block height from Bitcoin node: {e}")
                raise typer.Exit(1)
        else:
            logger.error("No block height source available.")
            logger.info("Provide --current-block for offline mode or configure a Bitcoin node.")
            raise typer.Exit(1)

    # Calculate cert_expiry as ABSOLUTE period number
    # Reference: yieldgenerator.py line 139
    # cert_expiry = ((blocks + BLOCK_COUNT_SAFETY) // RETARGET_INTERVAL) + CERT_MAX_VALIDITY_TIME
    retarget_interval = 2016
    block_count_safety = 2
    current_period = (current_block_height + block_count_safety) // retarget_interval
    cert_expiry = current_period + validity_periods

    # Validate cert_expiry fits in 2 bytes (uint16)
    if cert_expiry > 65535:
        logger.error(f"cert_expiry {cert_expiry} exceeds maximum 65535")
        raise typer.Exit(1)

    # Calculate expiry details for display
    expiry_block = cert_expiry * retarget_interval
    blocks_until_expiry = expiry_block - current_block_height
    weeks_until_expiry = blocks_until_expiry // 2016 * 2

    # Create ASCII certificate message (hex pubkey - compatible with Sparrow text input)
    # This format allows users to paste directly into Sparrow's message field
    cert_msg_ascii = f"fidelity-bond-cert|{cert_pubkey}|{cert_expiry}"

    # Save message to file for easier signing workflows
    data_dir.mkdir(parents=True, exist_ok=True)
    message_file = data_dir / "certificate_message.txt"
    message_file.write_text(cert_msg_ascii)

    # Get the signing address (P2WPKH address for the bond's pubkey)
    from jmwallet.wallet.address import pubkey_to_p2wpkh_address

    bond_pubkey = bytes.fromhex(bond.pubkey)
    # Determine network from bond
    signing_address = pubkey_to_p2wpkh_address(bond_pubkey, bond.network)

    print("\n" + "=" * 80)
    print("FIDELITY BOND CERTIFICATE MESSAGE")
    print("=" * 80)
    print(f"\nBond Address (P2WSH):  {bond_address}")
    print(f"Signing Address:       {signing_address}")
    print("  (Select this address in Sparrow to sign)")
    print(f"Certificate Pubkey:    {cert_pubkey}")
    print(f"\nCurrent Block:         {current_block_height} (period {current_period})")
    print(f"Cert Expiry:           period {cert_expiry} (block {expiry_block})")
    print(f"Validity:              ~{weeks_until_expiry} weeks ({blocks_until_expiry} blocks)")
    print("\n" + "-" * 80)
    print("MESSAGE TO SIGN (copy this EXACTLY into Sparrow):")
    print("-" * 80)
    print(cert_msg_ascii)
    print("-" * 80)
    print(f"\nMessage saved to: {message_file}")
    print("\n" + "=" * 80)
    print("HOW TO SIGN THIS MESSAGE:")
    print("=" * 80)
    print()
    print("Sparrow Wallet with Hardware Wallet:")
    print("  1. Open Sparrow Wallet and connect your hardware wallet")
    print("  2. Go to Tools -> Sign/Verify Message")
    print(f"  3. Select the Signing Address shown above: {signing_address}")
    print("  4. Copy the entire message above (fidelity-bond-cert|...) and")
    print("     paste it into the 'Message' field in Sparrow")
    print("  5. Select 'Standard (Electrum)' format (NOT BIP322)")
    print("  6. Click 'Sign Message' - hardware wallet will prompt for confirmation")
    print("  7. Copy the resulting base64 signature")
    print()
    print("After signing, import with:")
    print("  jm-wallet import-certificate <bond_address> \\")
    print("    --cert-signature '<base64_signature>' \\")
    print(f"    --cert-expiry {cert_expiry}")
    print("=" * 80 + "\n")


def _verify_recoverable_signature(
    sig_bytes: bytes, cert_pubkey_hex: str, cert_expiry: int, expected_pubkey: bytes
) -> bool:
    """
    Verify a 65-byte recoverable signature (Sparrow/Electrum format).

    Electrum format: 1 byte header + 32 bytes R + 32 bytes S
    Header encodes recovery ID and address type:
      27-30: P2PKH uncompressed
      31-34: P2PKH compressed
      35-38: P2SH-P2WPKH (nested segwit, compressed)
      39-42: P2WPKH (native segwit, compressed)

    Compact recovery format: 1-byte header + 32 bytes R + 32 bytes S

    Returns True if the recovered pubkey matches expected_pubkey.
    """
    from jmcore.crypto import bitcoin_message_hash_bytes

    if len(sig_bytes) != 65:
        return False

    r_value = int.from_bytes(sig_bytes[1:33], "big")
    s_value = int.from_bytes(sig_bytes[33:65], "big")
    if not 1 <= r_value < SECP256K1_N or not 1 <= s_value <= SECP256K1_N // 2:
        return False

    header = sig_bytes[0]

    # Determine recovery ID from header byte.
    # Electrum/Sparrow encode both recovery ID and address type in the header:
    #   27-30: P2PKH uncompressed
    #   31-34: P2PKH compressed
    #   35-38: P2SH-P2WPKH (nested segwit, compressed)
    #   39-42: P2WPKH (native segwit, compressed)
    # Trezor and Sparrow use the segwit ranges when signing with bc1q addresses.
    if 39 <= header <= 42:
        recovery_id = header - 39
        compressed = True
    elif 35 <= header <= 38:
        recovery_id = header - 35
        compressed = True
    elif 31 <= header <= 34:
        recovery_id = header - 31
        compressed = True
    elif 27 <= header <= 30:
        recovery_id = header - 27
        compressed = False
    else:
        logger.warning(f"Unknown signature header byte: {header}")
        return False

    if not CPubKey(expected_pubkey).is_fullyvalid():
        return False

    canonical_header = (31 if compressed else 27) + recovery_id
    compact_sig = bytes([canonical_header]) + sig_bytes[1:]

    def recovers_expected_pubkey(msg_hash: bytes) -> bool:
        recovered_pubkey = CPubKey.recover_compact(msg_hash, compact_sig)
        return (
            recovered_pubkey is not None
            and recovered_pubkey.is_fullyvalid()
            and bytes(recovered_pubkey) == expected_pubkey
        )

    # Try ASCII message format (what Sparrow signed with our new CLI)
    ascii_msg = f"fidelity-bond-cert|{cert_pubkey_hex}|{cert_expiry}".encode()
    msg_hash = bitcoin_message_hash_bytes(ascii_msg)

    try:
        if recovers_expected_pubkey(msg_hash):
            logger.debug("Signature verified with ASCII message format")
            return True
    except Exception as e:
        logger.debug(f"Recovery failed with ASCII format: {e}")

    # Try binary format (raw pubkey bytes) as fallback
    cert_pubkey_bytes = bytes.fromhex(cert_pubkey_hex)
    binary_msg = (
        b"fidelity-bond-cert|" + cert_pubkey_bytes + b"|" + str(cert_expiry).encode("ascii")
    )
    msg_hash = bitcoin_message_hash_bytes(binary_msg)

    try:
        if recovers_expected_pubkey(msg_hash):
            logger.debug("Signature verified with binary message format")
            return True
    except Exception as e:
        logger.debug(f"Recovery failed with binary format: {e}")

    # Try hex-as-text format (user pasted hex into Sparrow's message field)
    # This handles the case where user pasted the old CLI's hex output
    hex_msg = (
        b"fidelity-bond-cert|" + cert_pubkey_bytes + b"|" + str(cert_expiry).encode("ascii")
    ).hex()
    hex_as_text_msg = hex_msg.encode("utf-8")
    msg_hash = bitcoin_message_hash_bytes(hex_as_text_msg)

    try:
        if recovers_expected_pubkey(msg_hash):
            logger.debug("Signature verified with hex-as-text message format")
            return True
    except Exception as e:
        logger.debug(f"Recovery failed with hex-as-text format: {e}")

    return False


def _verify_der_signature(
    sig_bytes: bytes, cert_pubkey_hex: str, cert_expiry: int, expected_pubkey: bytes
) -> bool:
    """
    Verify a DER-encoded signature.

    Tries both ASCII and binary message formats.
    """
    from jmcore.crypto import bitcoin_message_hash_bytes, verify_raw_ecdsa

    cert_pubkey_bytes = bytes.fromhex(cert_pubkey_hex)

    # Try ASCII format first
    ascii_msg = f"fidelity-bond-cert|{cert_pubkey_hex}|{cert_expiry}".encode()
    msg_hash = bitcoin_message_hash_bytes(ascii_msg)

    if verify_raw_ecdsa(msg_hash, sig_bytes, expected_pubkey):
        logger.debug("DER signature verified with ASCII format")
        return True

    # Try binary format
    binary_msg = (
        b"fidelity-bond-cert|" + cert_pubkey_bytes + b"|" + str(cert_expiry).encode("ascii")
    )
    msg_hash = bitcoin_message_hash_bytes(binary_msg)

    if verify_raw_ecdsa(msg_hash, sig_bytes, expected_pubkey):
        logger.debug("DER signature verified with binary format")
        return True

    return False


def _recoverable_to_der(sig_bytes: bytes) -> bytes:
    """
    Convert a 65-byte recoverable signature to DER format.

    Format in: 1 byte header + 32 bytes R + 32 bytes S
    Format out: DER-encoded signature
    """
    if len(sig_bytes) != 65:
        return sig_bytes

    r = sig_bytes[1:33]
    s = sig_bytes[33:65]

    def encode_int(val: bytes) -> bytes:
        # Remove leading zeros but keep one if MSB is set
        val = val.lstrip(b"\x00") or b"\x00"
        if val[0] & 0x80:
            val = b"\x00" + val
        return bytes([len(val)]) + val

    r_enc = encode_int(r)
    s_enc = encode_int(s)

    sig_body = b"\x02" + r_enc + b"\x02" + s_enc
    return b"\x30" + bytes([len(sig_body)]) + sig_body


@app.command("import-certificate", no_args_is_help=True)
def import_certificate(
    address: Annotated[str, typer.Argument(help="Bond address")],
    cert_pubkey: Annotated[
        str | None, typer.Option("--cert-pubkey", help="Certificate pubkey (hex)")
    ] = None,
    cert_signature: Annotated[
        str, typer.Option("--cert-signature", help="Certificate signature (base64)")
    ] = "",
    cert_expiry: Annotated[
        int,
        typer.Option(
            "--cert-expiry",
            help="Certificate expiry as ABSOLUTE period number (from prepare-certificate-message)",
        ),
    ] = 0,  # 0 means "must be provided"
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
    skip_verification: Annotated[
        bool,
        typer.Option("--skip-verification", help="Skip signature verification (not recommended)"),
    ] = False,
    network: Annotated[str | None, typer.Option("--network", "-n", help="Bitcoin network")] = None,
    backend_type: Annotated[
        str | None,
        typer.Option("--backend", "-b", help="Backend: descriptor_wallet | neutrino"),
    ] = None,
    rpc_url: Annotated[str | None, typer.Option("--rpc-url", envvar="BITCOIN_RPC_URL")] = None,
    neutrino_url: Annotated[
        str | None, typer.Option("--neutrino-url", envvar="NEUTRINO_URL")
    ] = None,
    current_block: Annotated[
        int | None,
        typer.Option(
            "--current-block",
            help=(
                "Current block height override for offline/air-gapped workflows. "
                "Skips all network block-height lookups."
            ),
        ),
    ] = None,
    wallet_fingerprint: Annotated[
        str | None,
        typer.Option("--wallet-fingerprint", help=_WALLET_FINGERPRINT_HELP),
    ] = None,
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
) -> None:
    """
    Import a certificate signature for a fidelity bond (cold wallet support).

    This imports a certificate generated with 'prepare-certificate-message' into the
    bond registry, allowing the hot wallet to use it for making offers.

    IMPORTANT: The --cert-expiry value must match EXACTLY what was used in
    prepare-certificate-message. This is an ABSOLUTE period number, not a duration.

    If --cert-pubkey is not provided, it will be loaded from the bond registry.
    The certificate private key is loaded from the bond registry, or requested via
    an interactive hidden prompt if unavailable there.

    The signature should be the base64 output from Sparrow's message signing tool,
    using the 'Standard (Electrum)' format.
    """
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)

    from jmcore.paths import get_default_data_dir

    from jmwallet.wallet.bond_registry import get_registry_path, load_registry, save_registry

    # Load registry first to get bond info
    resolved_data_dir = data_dir if data_dir else get_default_data_dir()
    resolved_fingerprint = _resolve_wallet_fingerprint(wallet_fingerprint)
    registry = load_registry(resolved_data_dir, resolved_fingerprint)

    # Find bond by address
    bond = registry.get_bond_by_address(address)
    if not bond:
        logger.error("Bond not found in registry")
        logger.bind(sensitive=True).error(f"Bond not found for address: {address}")
        logger.info("Make sure you have created the bond with 'create-bond-address' first")
        raise typer.Exit(1)

    # Get cert_pubkey from argument or registry
    if not cert_pubkey:
        if bond.cert_pubkey:
            cert_pubkey = bond.cert_pubkey
            logger.info("Using certificate pubkey from bond registry")
        else:
            logger.error("--cert-pubkey is required")
            logger.info("Run 'generate-hot-keypair --bond-address <addr>' first")
            raise typer.Exit(1)

    cert_privkey = bond.cert_privkey
    if cert_privkey:
        logger.info("Using certificate privkey from bond registry")
    else:
        cert_privkey = typer.prompt("Certificate private key (hex)", hide_input=True).strip()
        if not cert_privkey:
            logger.error("Certificate private key is required")
            logger.info("Run 'generate-hot-keypair --bond-address <addr>' first")
            raise typer.Exit(1)

    if not cert_signature:
        logger.error("--cert-signature is required")
        raise typer.Exit(1)

    # The wire field is an unsigned 16-bit absolute period.
    if not 1 <= cert_expiry <= 65535:
        logger.error("--cert-expiry must be between 1 and 65535")
        logger.info("Use the same absolute period shown by 'prepare-certificate-message'")
        raise typer.Exit(1)

    # Fetch current block height to validate cert_expiry is in the future.
    # Priority: explicit --current-block > configured node backend > --mempool-api.
    import asyncio

    current_block_height: int | None = None
    if current_block is not None:
        if current_block < 0:
            logger.error("--current-block must be >= 0")
            raise typer.Exit(1)
        current_block_height = current_block
        logger.info(f"Current block height: {current_block_height} (from --current-block)")
    else:
        backend_settings = resolve_backend_settings(
            settings,
            network=network,
            backend_type=backend_type,
            rpc_url=rpc_url,
            neutrino_url=neutrino_url,
            data_dir=data_dir,
        )

        node_available = bool(backend_settings.rpc_url or backend_settings.neutrino_url)

        if node_available:
            from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
            from jmwallet.backends.neutrino import NeutrinoBackend

            try:
                if backend_settings.backend_type == "neutrino":
                    node_backend: DescriptorWalletBackend | NeutrinoBackend = NeutrinoBackend(
                        neutrino_url=backend_settings.neutrino_url,
                        network=backend_settings.network,
                        tls_cert_path=backend_settings.neutrino_tls_cert,
                        auth_token=backend_settings.neutrino_auth_token,
                    )
                else:
                    node_backend = DescriptorWalletBackend(
                        rpc_url=backend_settings.rpc_url,
                        rpc_user=backend_settings.rpc_user,
                        rpc_password=backend_settings.rpc_password,
                    )
                current_block_height = asyncio.run(node_backend.get_block_height())
                logger.debug(f"Current block height: {current_block_height} (from node)")
            except Exception as e:
                logger.warning(f"Failed to fetch block height from Bitcoin node: {e}")

        if current_block_height is None:
            logger.error("Cannot determine current block height for certificate expiry validation")
            logger.info("Provide --current-block for offline mode or configure a Bitcoin node.")
            raise typer.Exit(1)

    # Validate cert_expiry is in the future
    retarget_interval = 2016
    assert current_block_height is not None
    expiry_block = cert_expiry * retarget_interval
    if current_block_height > expiry_block:
        logger.error("Certificate has ALREADY EXPIRED!")
        logger.error(f"  Current block: {current_block_height}")
        logger.error(f"  Cert expiry:   period {cert_expiry} (block {expiry_block})")
        logger.info("Run 'prepare-certificate-message' again with current block height")
        logger.info("and re-sign the new message with your hardware wallet.")
        raise typer.Exit(1)

    blocks_remaining = expiry_block - current_block_height
    weeks_remaining = blocks_remaining // retarget_interval * 2
    logger.info(f"Certificate valid for ~{weeks_remaining} weeks ({blocks_remaining} blocks)")

    # Validate inputs
    try:
        cert_pubkey_bytes = bytes.fromhex(cert_pubkey)
        if len(cert_pubkey_bytes) != 33:
            raise ValueError("Certificate pubkey must be 33 bytes")
        if cert_pubkey_bytes[0] not in (0x02, 0x03):
            raise ValueError("Invalid compressed public key format")
        if not CPubKey(cert_pubkey_bytes).is_fullyvalid():
            raise ValueError("Invalid compressed public key")

        cert_privkey_bytes = bytes.fromhex(cert_privkey)
        if len(cert_privkey_bytes) != 32:
            raise ValueError("Certificate privkey must be 32 bytes")

        # Decode signature from base64 (Sparrow output)
        try:
            cert_sig_bytes = base64.b64decode(cert_signature)
        except Exception:
            # Try hex format as fallback
            try:
                cert_sig_bytes = bytes.fromhex(cert_signature)
            except Exception:
                raise ValueError("Signature must be base64 (from Sparrow) or hex encoded")

        # Verify that privkey matches pubkey
        privkey = CKey.from_secret_bytes(cert_privkey_bytes)
        derived_pubkey = bytes(privkey.pub)
        if derived_pubkey != cert_pubkey_bytes:
            raise ValueError("Certificate privkey does not match cert_pubkey!")

    except ValueError as e:
        logger.error(f"Invalid input: {e}")
        raise typer.Exit(1)

    # Get the bond's utxo pubkey
    utxo_pubkey = bytes.fromhex(bond.pubkey)

    # Verify certificate signature (unless skipped)
    if not skip_verification:
        # The signature from Sparrow is a 65-byte recoverable signature:
        # 1 byte header (recovery ID + 27 for compressed) + 32 bytes R + 32 bytes S
        if len(cert_sig_bytes) == 65:
            logger.info("Detected 65-byte recoverable signature (Sparrow/Electrum format)")
            verified = _verify_recoverable_signature(
                cert_sig_bytes, cert_pubkey, cert_expiry, utxo_pubkey
            )
        else:
            # Try DER format
            logger.info(f"Signature is {len(cert_sig_bytes)} bytes, trying DER format")
            verified = _verify_der_signature(cert_sig_bytes, cert_pubkey, cert_expiry, utxo_pubkey)

        if not verified:
            logger.error("Certificate signature verification failed!")
            logger.error("The signature does not match the bond's public key.")
            logger.info("Make sure you:")
            logger.info("  1. Selected the correct signing address in Sparrow")
            logger.info("  2. Copied the message EXACTLY as shown by prepare-certificate-message")
            logger.info("  3. Used 'Standard (Electrum)' format in Sparrow")
            raise typer.Exit(1)

        logger.info("Certificate signature verified successfully")
    else:
        logger.warning("Skipping signature verification - use at your own risk!")

    # Convert recoverable signature to DER format for storage
    # The maker code expects DER signatures
    if len(cert_sig_bytes) == 65:
        der_sig = _recoverable_to_der(cert_sig_bytes)
    else:
        der_sig = cert_sig_bytes

    # Update bond with certificate
    bond.cert_pubkey = cert_pubkey
    bond.cert_privkey = cert_privkey
    bond.cert_signature = der_sig.hex()  # Store as hex DER
    bond.cert_expiry = cert_expiry

    save_registry(registry, resolved_data_dir, resolved_fingerprint)

    # Calculate expiry info for display
    expiry_block = cert_expiry * retarget_interval
    blocks_remaining = expiry_block - current_block_height
    weeks_remaining = blocks_remaining // retarget_interval * 2
    expiry_info = f"~{weeks_remaining} weeks remaining"

    print("\n" + "=" * 80)
    print("CERTIFICATE IMPORTED SUCCESSFULLY")
    print("=" * 80)
    print(f"\nBond Address:          {address}")
    print(f"Certificate Pubkey:    {cert_pubkey}")
    print(f"Certificate Expiry:    period {cert_expiry} (block {expiry_block}, {expiry_info})")
    print(f"\nRegistry updated: {get_registry_path(resolved_data_dir, resolved_fingerprint)}")
    print("\n" + "=" * 80)
    print("NEXT STEPS:")
    print("  The maker bot will automatically use this certificate when creating")
    print("  fidelity bond proofs. Your cold wallet private key is never needed!")
    print("=" * 80 + "\n")


@app.command("spend-bond", no_args_is_help=True)
def spend_bond(
    bond_address: Annotated[str, typer.Argument(help="Bond P2WSH address to spend")],
    destination: Annotated[str, typer.Argument(help="Destination address for the funds")],
    fee_rate: Annotated[
        float,
        typer.Option("--fee-rate", "-f", help="Fee rate in sat/vB"),
    ] = 1.0,
    master_fingerprint: Annotated[
        str | None,
        typer.Option(
            "--master-fingerprint",
            "-m",
            help=(
                "Master key fingerprint (4 bytes hex, e.g. 'aabbccdd'). "
                "Found in Sparrow: Settings -> Keystore -> Master fingerprint. "
                "Enables Sparrow and HWI to identify the signing key."
            ),
        ),
    ] = None,
    derivation_path: Annotated[
        str | None,
        typer.Option(
            "--derivation-path",
            "-p",
            help=(
                "BIP32 derivation path of the key used for the bond "
                "(e.g. \"m/84'/0'/0'/0/0\"). "
                "This is the path of the address whose pubkey was used in "
                "'create-bond-address'. Check Sparrow: Addresses tab -> "
                "right-click the address -> Copy -> Derivation Path."
            ),
        ),
    ] = None,
    output_file: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Save PSBT to file (default: stdout only)"),
    ] = None,
    test_unfunded: Annotated[
        bool,
        typer.Option(
            "--test-unfunded",
            help=(
                "Allow generating a test PSBT even when the bond is unfunded, "
                "using a synthetic UTXO for signer compatibility testing."
            ),
        ),
    ] = False,
    test_utxo_value: Annotated[
        int,
        typer.Option(
            "--test-utxo-value",
            help=("Synthetic UTXO value in sats when using --test-unfunded (default: 100000)."),
        ),
    ] = 100_000,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    wallet_fingerprint: Annotated[
        str | None,
        typer.Option("--wallet-fingerprint", help=_WALLET_FINGERPRINT_HELP),
    ] = None,
    log_level: Annotated[str, typer.Option("--log-level", "-l")] = "INFO",
) -> None:
    """
    Generate a PSBT to spend a cold storage fidelity bond after locktime expires.

    This creates a Partially Signed Bitcoin Transaction (PSBT) that can be signed
    using HWI (hardware wallet) or the mnemonic signing script (software wallet).

    The PSBT includes the witness script (CLTV timelock) needed to spend the bond.

    HOT WALLETS: If the bond was created with 'generate-bond-address' (key derived
    from this wallet's seed), you do NOT need a PSBT. After it expires, spend it
    directly with: jm-wallet send <destination> --select-utxos

    REQUIREMENTS:
    - The bond must exist in the registry (created with 'create-bond-address')
    - The bond must be funded (use 'jm-wallet sync-bonds'
      to update UTXO info), unless using --test-unfunded for a dry-run signer test
    - The locktime must have expired (or be close enough for your use case)

    SIGNING:

    Hardware-wallet support for custom CLTV P2WSH scripts varies by device,
    firmware, and Bitcoin app. Test the exact signer with --test-unfunded before
    funding. HWI signing uses scripts/sign_bond_psbt.py; QR signers can return a
    signed PSBT; scripts/sign_bond_mnemonic.py is the software fallback.

    Option A - Mnemonic signing (works with any device):
    1. Run: python scripts/sign_bond_mnemonic.py <psbt_base64>
    2. Enter your BIP39 mnemonic when prompted (hidden input)
    3. Broadcast: bitcoin-cli sendrawtransaction <signed_hex>

    Option B - HWI signing (Jade; BitBox01 expected; Ledger legacy app only):
    1. Install HWI >= 3.1.0. Jade must not use cbor2 5.8.0; see the guide.
    2. Connect and unlock your hardware wallet
    3. Run: python scripts/sign_bond_psbt.py --chain <main|test|signet|regtest> <psbt_base64>
       BitBox01: add --device-type digitalbitbox --device-password

    See docs/fidelity-bond-operations.md for signer testing, backups, and the
    complete redemption workflow.

    NOTE: Sparrow Wallet also cannot sign CLTV timelock scripts.
    """
    setup_cli(log_level, data_dir=data_dir)

    from jmcore.bitcoin import (
        BIP32Derivation,
        PSBTInput,
        TxInput,
        TxOutput,
        address_to_scriptpubkey,
        create_psbt,
        estimate_vsize,
        format_amount,
        get_address_type,
        parse_derivation_path,
        psbt_to_base64,
        script_to_p2wsh_scriptpubkey,
    )
    from jmcore.paths import get_default_data_dir

    from jmwallet.wallet.bond_registry import load_registry

    # Resolve data directory
    resolved_data_dir = data_dir if data_dir else get_default_data_dir()
    resolved_fingerprint = _resolve_wallet_fingerprint(wallet_fingerprint)
    registry = load_registry(resolved_data_dir, resolved_fingerprint)

    # Find bond in registry
    bond = registry.get_bond_by_address(bond_address)
    if not bond:
        logger.error("Bond not found in registry")
        logger.bind(sensitive=True).error(f"Bond not found for address: {bond_address}")
        logger.info("Make sure you have created the bond with 'create-bond-address' first")
        logger.info("Use 'jm-wallet list-bonds' to see all bonds")
        raise typer.Exit(1)

    if master_fingerprint is None and derivation_path is None and bond.signer_master_fingerprint:
        master_fingerprint = bond.signer_master_fingerprint
        derivation_path = bond.path
        logger.info("Using BIP32 derivation metadata from imported bond registration")

    # Resolve bond UTXO source (real UTXO or synthetic dry-run UTXO)
    is_test_unfunded_mode = False
    if bond.is_funded:
        assert bond.txid is not None
        assert bond.vout is not None
        assert bond.value is not None
        txid = bond.txid
        vout = bond.vout
        input_value = bond.value
    else:
        if not test_unfunded:
            logger.error("Bond is not funded (no UTXO info)")
            logger.info(
                "Use 'jm-wallet sync-bonds' to update UTXO info "
                "from the blockchain, or manually fund the bond address first"
            )
            logger.info(
                "If you want to test signer compatibility before funding, rerun with "
                "--test-unfunded"
            )
            raise typer.Exit(1)

        if test_utxo_value <= 0:
            logger.error("--test-utxo-value must be positive")
            raise typer.Exit(1)

        # Deterministic synthetic outpoint for dry-run signer testing.
        txid = "11" * 32
        vout = 0
        input_value = test_utxo_value
        is_test_unfunded_mode = True
        logger.warning(
            "Generating TEST PSBT for unfunded bond using synthetic UTXO metadata. "
            "This PSBT is for signing-tool validation only and CANNOT be broadcast."
        )

    # Warn if locktime hasn't expired yet
    import time

    current_time = int(time.time())
    if bond.locktime > current_time:
        remaining_days = (bond.locktime - current_time) / 86400
        logger.warning(
            f"Bond locktime has NOT expired yet! "
            f"Expires in {remaining_days:.1f} days "
            f"({datetime.fromtimestamp(bond.locktime).strftime('%Y-%m-%d')})"
        )
        logger.warning(
            "The PSBT will be created anyway, but the transaction CANNOT be "
            "broadcast until the locktime has passed."
        )

    # Validate fee rate
    if fee_rate <= 0:
        logger.error("Fee rate must be positive")
        raise typer.Exit(1)

    # Validate destination address
    try:
        dest_scriptpubkey = address_to_scriptpubkey(destination)
    except ValueError as e:
        logger.error("Invalid destination address")
        logger.bind(sensitive=True).error(f"Invalid destination address: {e}")
        raise typer.Exit(1)

    # Estimate transaction size for fee calculation
    # P2WSH input -> single output (no change since we sweep the whole bond)
    try:
        dest_type = get_address_type(destination)
    except ValueError:
        logger.warning("Could not determine destination address type, assuming p2wpkh")
        logger.bind(sensitive=True).warning(
            f"Could not determine address type for {destination}, assuming p2wpkh"
        )
        dest_type = "p2wpkh"

    estimated_vsize = estimate_vsize(["p2wsh"], [dest_type])
    estimated_fee = math.ceil(estimated_vsize * fee_rate)

    send_amount = input_value - estimated_fee
    if send_amount <= 0:
        logger.error("Bond value is too small to cover the fee")
        logger.bind(sensitive=True).error(
            f"Bond value ({format_amount(input_value)}) is too small to cover "
            f"the fee ({format_amount(estimated_fee)} at {fee_rate:.1f} sat/vB)"
        )
        raise typer.Exit(1)

    if send_amount < 546:
        logger.error("Output amount is below the dust threshold")
        logger.bind(sensitive=True).error(
            f"Output amount ({format_amount(send_amount)}) is below dust threshold (546 sats)"
        )
        raise typer.Exit(1)

    # Reconstruct the witness script and P2WSH scriptPubKey
    witness_script = bytes.fromhex(bond.witness_script_hex)
    p2wsh_scriptpubkey = script_to_p2wsh_scriptpubkey(witness_script)

    # Build BIP32 derivation info if provided (helps signers identify the key)
    bip32_derivations: list[BIP32Derivation] | None = None
    if master_fingerprint or derivation_path:
        if not master_fingerprint or not derivation_path:
            logger.error(
                "--master-fingerprint and --derivation-path must both be provided. "
                "In Sparrow: Settings -> Keystore for the fingerprint, "
                "Addresses tab -> right-click -> Copy -> Derivation Path for the path."
            )
            raise typer.Exit(1)

        # Validate and parse master fingerprint (4 bytes hex)
        fingerprint_clean = master_fingerprint.strip().lower()
        try:
            fp_bytes = bytes.fromhex(fingerprint_clean)
        except ValueError:
            logger.error("Invalid master fingerprint hex")
            logger.bind(sensitive=True).error(
                f"Invalid master fingerprint hex: {master_fingerprint!r}"
            )
            raise typer.Exit(1)
        if len(fp_bytes) != 4:
            logger.error(
                f"Master fingerprint must be exactly 4 bytes (8 hex chars), "
                f"got {len(fp_bytes)} bytes"
            )
            raise typer.Exit(1)

        # Parse derivation path
        try:
            path_indices = parse_derivation_path(derivation_path)
        except ValueError as e:
            logger.error("Invalid derivation path")
            logger.bind(sensitive=True).error(f"Invalid derivation path: {e}")
            raise typer.Exit(1)

        pubkey_bytes = bytes.fromhex(bond.pubkey)
        bip32_derivations = [
            BIP32Derivation(
                pubkey=pubkey_bytes,
                fingerprint=fp_bytes,
                path=path_indices,
            )
        ]
        logger.info("BIP32 derivation included")
        logger.bind(sensitive=True).info(
            f"BIP32 derivation included: fingerprint={fingerprint_clean}, path={derivation_path}"
        )

    # Build the unsigned transaction components
    tx_input = TxInput.from_hex(
        txid=txid,
        vout=vout,
        # Sequence must be < 0xFFFFFFFF to enable nLockTime checking
        sequence=0xFFFFFFFE,
        value=input_value,
        scriptpubkey=p2wsh_scriptpubkey.hex(),
    )
    tx_output = TxOutput(value=send_amount, script=dest_scriptpubkey)

    # Create PSBT input metadata
    psbt_input = PSBTInput(
        witness_utxo_value=input_value,
        witness_utxo_script=p2wsh_scriptpubkey,
        witness_script=witness_script,
        sighash_type=1,  # SIGHASH_ALL
        bip32_derivations=bip32_derivations,
    )

    # Create the PSBT
    psbt_bytes = create_psbt(
        version=2,
        inputs=[tx_input],
        outputs=[tx_output],
        locktime=bond.locktime,
        psbt_inputs=[psbt_input],
    )

    psbt_base64 = psbt_to_base64(psbt_bytes)

    # Save to file if requested
    if output_file:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(psbt_base64)
        logger.info("PSBT saved")
        logger.bind(sensitive=True).info(f"PSBT saved to: {output_file}")

    # Display results
    locktime_dt = datetime.fromtimestamp(bond.locktime)

    print("\n" + "=" * 80)
    print("SPEND BOND PSBT")
    print("=" * 80)
    if is_test_unfunded_mode:
        print("\nMODE:             TEST-UNFUNDED (synthetic UTXO, not broadcastable)")
    print(f"\nBond Address:     {bond_address}")
    print(f"Bond UTXO:        {txid}:{vout}")
    print(f"Bond Value:       {format_amount(input_value)}")
    print(f"Locktime:         {bond.locktime} ({locktime_dt.strftime('%Y-%m-%d')})")
    print(f"\nDestination:      {destination}")
    print(f"Send Amount:      {format_amount(send_amount)}")
    print(f"Fee:              {format_amount(estimated_fee)} ({fee_rate:.1f} sat/vB)")
    print(f"Estimated vsize:  {estimated_vsize} vB")
    print("\n" + "-" * 80)
    print("PSBT (base64):")
    print("-" * 80)
    print(psbt_base64)
    print("-" * 80)
    if output_file:
        print(f"\nSaved to: {output_file}")

    print("\n" + "=" * 80)
    print("HOW TO SIGN AND BROADCAST:")
    print("=" * 80)
    print()
    if is_test_unfunded_mode:
        print("WARNING: TEST-UNFUNDED mode uses a synthetic input and cannot be broadcast.")
        print("  Use this only to validate your signing workflow before funding the bond.")
        print()
    print("NOTE: Most hardware wallets (Trezor, Coldcard, BitBox02, KeepKey)")
    print("  CANNOT sign CLTV timelock P2WSH scripts. Classic Jade signing is")
    print("  verified via HWI; BitBox01 is expected but untested. Ledger only with")
    print("  the legacy Bitcoin app (2.0.x and earlier); the current app (2.1+)")
    print("  has been reported to reject bond PSBTs. Specter DIY signs via QR.")
    print()
    print("Option A - Mnemonic signing (works with any device):")
    if bip32_derivations:
        print("  1. Run: python scripts/sign_bond_mnemonic.py <psbt_base64>")
    else:
        print("  1. Run: python scripts/sign_bond_mnemonic.py <psbt_base64> \\")
        print("       --derivation-path \"m/84'/0'/0'/0/0\"")
    print("  2. Enter your BIP39 mnemonic when prompted (hidden input)")
    print("  3. Broadcast: bitcoin-cli sendrawtransaction <signed_hex>")
    print()
    if bip32_derivations:
        print("Option B - HWI signing (Jade; BitBox01 expected; Ledger legacy app only):")
        print("  1. Install HWI >= 3.1.0. Jade must not use cbor2 5.8.0;")
        print("     see docs/fidelity-bond-operations.md for the working version matrix.")
        print("  2. Connect and unlock your hardware wallet")
        print(
            "  3. Run: python scripts/sign_bond_psbt.py "
            "--chain <main|test|signet|regtest> <psbt_base64>"
        )
        print("     BitBox01: add --device-type digitalbitbox --device-password")
        print()
    if not bip32_derivations:
        print("TIP: Re-run with --master-fingerprint and --derivation-path to")
        print("  embed BIP32 key origin info (needed for HWI, optional for mnemonic).")
        print()
        print("  Example:")
        print("    jm-wallet spend-bond <bond_addr> <dest_addr> \\")
        print("      --master-fingerprint aabbccdd \\")
        print("      --derivation-path \"m/84'/0'/0'/0/0\"")
        print()
    print("See docs/fidelity-bond-operations.md for signer testing, backups,")
    print("  and the complete redemption workflow.")
    print()
    print("NOTE: Sparrow Wallet also cannot sign CLTV timelock scripts.")
    print()
    if bond.locktime > current_time:
        print("WARNING: The locktime has NOT expired yet!")
        print("  You can sign the PSBT now, but broadcasting will fail until")
        print(f"  {locktime_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print()
    print("=" * 80 + "\n")
