"""Destructive wallet cleanup helpers used by ``jm-wallet delete``."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from jmcore.bitcoin import get_hrp
from jmcore.cli_common import ResolvedBackendSettings
from jmcore.paths import get_wallet_metadata_path

from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.backends.offline import OfflineBackend
from jmwallet.history import delete_wallet_history_entries
from jmwallet.history_state import load_reconstruction_checkpoint_strict
from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
from jmwallet.wallet.bond_registry import (
    BondRegistry,
    FidelityBondInfo,
    delete_wallet_registry_entries,
    get_legacy_registry_path,
    get_registry_path,
    make_wallet_ownership_predicate,
)
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.sync import NEUTRINO_HISTORICAL_BACKFILL_BATCH_SIZE

_MAX_SUPPORTED_MIXDEPTH_COUNT = 10


@dataclass(frozen=True)
class WalletDeletionResult:
    """Counts and paths removed by a completed wallet deletion."""

    removed_paths: tuple[Path, ...]
    history_entries: int = 0
    bond_entries: int = 0


def _canonical_watch_address(address: str, network: str) -> str:
    """Match the lowercase Bech32 form used by Neutrino wallet sync."""
    if not address or address != address.strip():
        raise ValueError("Invalid wallet watch address")
    lowered = address.lower()
    return lowered if lowered.startswith(f"{get_hrp(network)}1") else address


def _metadata_watch_addresses(data_dir: Path, fingerprint: str, network: str) -> set[str]:
    """Read every persisted address using this wallet's metadata labels.

    Metadata loading in normal wallet operation deliberately tolerates malformed
    lines to preserve interoperability. Deletion must instead fail closed: an
    unreadable own marker could otherwise leave its Neutrino watch behind.
    """
    path = get_wallet_metadata_path(data_dir, fingerprint=fingerprint)
    if not path.exists():
        return set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"Cannot read wallet metadata {path}: {exc}") from exc

    addresses: set[str] = set()
    prefixes = ("jm:used", "jm:reserved", "jm:funded")
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid wallet metadata {path}:{line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Invalid wallet metadata record at {path}:{line_number}")
        label = record.get("label")
        if not isinstance(label, str):
            continue
        matching_prefix = next((prefix for prefix in prefixes if label.startswith(prefix)), None)
        if matching_prefix is None:
            continue
        if label != matching_prefix and not label.startswith(f"{matching_prefix}:"):
            raise ValueError(f"Invalid wallet metadata label at {path}:{line_number}")
        if record.get("type") != "addr":
            raise ValueError(f"Invalid wallet metadata address record at {path}:{line_number}")
        address = record.get("ref")
        if not isinstance(address, str):
            raise ValueError(f"Invalid wallet metadata address at {path}:{line_number}")
        try:
            addresses.add(_canonical_watch_address(address, network))
        except ValueError as exc:
            raise ValueError(f"Invalid wallet metadata address at {path}:{line_number}") from exc
    return addresses


def _load_bond_registry_for_cleanup(path: Path) -> BondRegistry:
    """Load a registry strictly because missing an owned address is unsafe."""
    try:
        return BondRegistry.model_validate_json(path.read_text(), strict=True)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid fidelity-bond registry {path}: {exc}") from exc


def _add_registry_addresses(
    addresses: set[str],
    bonds: Iterable[FidelityBondInfo],
    *,
    network: str,
    owned_only: bool,
    wallet: WalletService,
) -> None:
    """Add matching registry addresses, proving ownership for legacy entries."""
    belongs_to_wallet = make_wallet_ownership_predicate(
        wallet.master_key, wallet.fidelity_bond_root_path
    )
    for bond in bonds:
        if bond.network != network:
            continue
        if owned_only:
            try:
                if not belongs_to_wallet(bond):
                    continue
            except Exception as exc:
                raise ValueError(
                    f"Cannot determine ownership of legacy fidelity bond {bond.address}"
                ) from exc
        address = bond.address
        if not isinstance(address, str):
            raise ValueError("Invalid fidelity-bond registry address")
        addresses.add(_canonical_watch_address(address, network))


def collect_neutrino_watch_addresses(
    *,
    data_dir: Path,
    mnemonic: str,
    bip39_passphrase: str,
    fingerprint: str,
    network: str,
    neutrino_url: str,
    mixdepth_count: int,
    gap_limit: int,
    scan_range: int,
) -> tuple[str, ...]:
    """Derive the complete deterministic Neutrino watch set for wallet deletion.

    The normal regular-branch footprint is intentionally bounded to the
    historical Neutrino backfill size. A matching completed reconstruction
    checkpoint expands only the branches it actually scanned.
    """
    baseline_end = (
        max(
            gap_limit,
            min(scan_range, NEUTRINO_HISTORICAL_BACKFILL_BATCH_SIZE),
        )
        - 1
    )
    if baseline_end < 0:
        raise ValueError("Wallet address coverage settings must be positive")

    checkpoint = load_reconstruction_checkpoint_strict(data_dir, wallet_fingerprint=fingerprint)
    matching_checkpoint_ends: dict[tuple[int, int], int] = {}
    if checkpoint is not None:
        if checkpoint.version != 1:
            raise ValueError("Unsupported history reconstruction checkpoint version")
        for branch, end in checkpoint.regular_branch_ends.items():
            try:
                mixdepth_text, change_text = branch.split(":", 1)
                mixdepth = int(mixdepth_text)
                change = int(change_text)
            except (AttributeError, ValueError) as exc:
                raise ValueError(f"Invalid reconstruction checkpoint branch {branch!r}") from exc
            if (
                mixdepth < 0
                or mixdepth >= _MAX_SUPPORTED_MIXDEPTH_COUNT
                or change not in (0, 1)
                or isinstance(end, bool)
                or end < 0
            ):
                raise ValueError(f"Invalid reconstruction checkpoint branch {branch!r}")
        expected_backend_id = "jmwallet.backends.neutrino.NeutrinoBackend|" + neutrino_url.rstrip(
            "/"
        )
        if (
            checkpoint.wallet_fingerprint == fingerprint
            and checkpoint.network == network
            and checkpoint.backend_id == expected_backend_id
        ):
            for branch, end in checkpoint.regular_branch_ends.items():
                mixdepth_text, change_text = branch.split(":", 1)
                mixdepth = int(mixdepth_text)
                change = int(change_text)
                matching_checkpoint_ends[(mixdepth, change)] = end

    derivation_mixdepth_count = max(
        mixdepth_count,
        max((mixdepth + 1 for mixdepth, _change in matching_checkpoint_ends), default=0),
    )
    wallet = WalletService(
        mnemonic=mnemonic,
        backend=OfflineBackend(),
        network=network,
        mixdepth_count=derivation_mixdepth_count,
        gap_limit=gap_limit,
        scan_range=scan_range,
        passphrase=bip39_passphrase,
    )
    if wallet.wallet_fingerprint != fingerprint:
        raise ValueError("Mnemonic-derived wallet fingerprint does not match deletion fingerprint")

    branch_ends = {
        (mixdepth, change): baseline_end
        for mixdepth in range(derivation_mixdepth_count)
        for change in (0, 1)
    }
    for branch, end in matching_checkpoint_ends.items():
        branch_ends[branch] = max(branch_ends[branch], end)

    addresses = _metadata_watch_addresses(data_dir, fingerprint, network)
    for (mixdepth, change), end in branch_ends.items():
        addresses.update(wallet.get_address(mixdepth, change, index) for index in range(end + 1))

    from jmcore.timenumber import TIMENUMBER_COUNT, timenumber_to_timestamp

    addresses.update(
        wallet.get_fidelity_bond_address(timenumber, timenumber_to_timestamp(timenumber))
        for timenumber in range(TIMENUMBER_COUNT)
    )

    per_wallet_path = get_registry_path(data_dir, fingerprint)
    if per_wallet_path.exists():
        _add_registry_addresses(
            addresses,
            _load_bond_registry_for_cleanup(per_wallet_path).bonds,
            network=network,
            owned_only=False,
            wallet=wallet,
        )
    legacy_path = get_legacy_registry_path(data_dir)
    if legacy_path.exists():
        _add_registry_addresses(
            addresses,
            _load_bond_registry_for_cleanup(legacy_path).bonds,
            network=network,
            owned_only=True,
            wallet=wallet,
        )
    return tuple(sorted(addresses))


async def remove_neutrino_wallet_watches(
    backend_settings: ResolvedBackendSettings, addresses: list[str]
) -> tuple[int, int]:
    """Remove one wallet's persisted Neutrino watched addresses and close the client."""
    from jmwallet.backends.neutrino import NeutrinoBackend

    backend = NeutrinoBackend(
        neutrino_url=backend_settings.neutrino_url,
        network=backend_settings.bitcoin_network,
        add_peers=backend_settings.neutrino_add_peers,
        scan_start_height=backend_settings.scan_start_height,
        tls_cert_path=backend_settings.neutrino_tls_cert,
        auth_token=backend_settings.neutrino_auth_token,
    )
    try:
        return await backend.remove_watch_addresses(addresses)
    finally:
        await backend.close()


def local_wallet_artifact_paths(
    data_dir: Path,
    mnemonic_file: Path,
    fingerprint: str,
) -> tuple[Path, ...]:
    """Return wallet-private files removed on every deletion."""
    metadata_path = get_wallet_metadata_path(data_dir, fingerprint=fingerprint)
    return (
        metadata_path,
        metadata_path.with_suffix(".lock"),
        data_dir / f"address_history_{fingerprint}.jsonl",
        data_dir / "state" / f"history_reconstruction_{fingerprint}.json",
        mnemonic_file.with_name(mnemonic_file.name + ".meta"),
        mnemonic_file,
    )


def core_wallet_path(core_wallet_dir: Path, wallet_name: str) -> Path:
    """Build and validate the host-local path for one generated Core wallet."""
    if Path(wallet_name).name != wallet_name or wallet_name in {"", ".", ".."}:
        raise ValueError("unsafe Bitcoin Core wallet name")
    base = core_wallet_dir.expanduser().resolve(strict=True)
    if not base.is_dir():
        raise ValueError(f"Bitcoin Core wallet directory is not a directory: {base}")
    candidate = base / wallet_name
    if candidate.is_symlink():
        raise ValueError(f"Refusing to delete symlinked Bitcoin Core wallet path: {candidate}")
    return candidate


async def delete_core_descriptor_wallet(
    backend_settings: ResolvedBackendSettings,
    wallet_name: str,
    core_wallet_dir: Path,
) -> Path:
    """Unload a Core wallet, disable startup loading, and remove local files."""
    path = core_wallet_path(core_wallet_dir, wallet_name)
    backend = DescriptorWalletBackend(
        rpc_url=backend_settings.rpc_url,
        rpc_user=backend_settings.rpc_user,
        rpc_password=backend_settings.rpc_password,
        wallet_name=wallet_name,
    )
    try:
        exists_on_node = await backend.wallet_exists()
        if exists_on_node and not path.exists():
            raise ValueError(
                f"Bitcoin Core knows wallet {wallet_name!r}, but {path} does not exist. "
                "Pass Core's actual -walletdir path."
            )
        if not exists_on_node and path.exists():
            raise ValueError(
                f"Bitcoin Core does not report wallet {wallet_name!r}, but {path} exists. "
                "Refusing to delete files that may belong to a different Core instance."
            )
        if exists_on_node:
            await backend.unload_wallet_for_deletion()
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        return path
    finally:
        await backend.close()


def delete_wallet_data(
    *,
    data_dir: Path,
    mnemonic_file: Path,
    mnemonic: str,
    bip39_passphrase: str,
    fingerprint: str,
    network: str,
    delete_history: bool,
    delete_bond_registry: bool,
    core_path: Path | None = None,
) -> WalletDeletionResult:
    """Delete local wallet files after backend cleanup has succeeded."""
    history_entries = delete_wallet_history_entries(fingerprint, data_dir) if delete_history else 0

    bond_entries = 0
    if delete_bond_registry:
        master_key = HDKey.from_seed(mnemonic_to_seed(mnemonic, bip39_passphrase))
        coin_type = 0 if network == "mainnet" else 1
        predicate = make_wallet_ownership_predicate(master_key, f"m/84'/{coin_type}'")
        bond_entries = delete_wallet_registry_entries(data_dir, fingerprint, predicate)

    removed_paths: list[Path] = []
    for path in local_wallet_artifact_paths(data_dir, mnemonic_file, fingerprint):
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_dir() and not path.is_symlink():
            raise ValueError(f"Refusing to delete unexpected directory: {path}")
        path.unlink()
        removed_paths.append(path)

    if core_path is not None:
        removed_paths.insert(0, core_path)

    return WalletDeletionResult(
        removed_paths=tuple(removed_paths),
        history_entries=history_entries,
        bond_entries=bond_entries,
    )
