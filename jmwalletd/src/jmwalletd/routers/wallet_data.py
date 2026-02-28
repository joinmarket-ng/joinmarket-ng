"""Wallet data endpoints: display, UTXOs, addresses, seed, freeze, config, rescan, sign."""

from __future__ import annotations

import calendar
import datetime
from typing import Any

from fastapi import APIRouter, Depends
from loguru import logger

from jmwallet.wallet.bond_registry import create_bond_info, load_registry, save_registry
from jmwallet.wallet.constants import FIDELITY_BOND_BRANCH
from jmwalletd.deps import get_daemon_state, require_auth
from jmwalletd.errors import (
    ActionNotAllowed,
    ConfigNotPresent,
    InvalidRequestFormat,
    YieldGeneratorDataUnreadable,
)
from jmwalletd.models import (
    ConfigGetRequest,
    ConfigGetResponse,
    ConfigSetRequest,
    FreezeRequest,
    GetAddressResponse,
    GetSeedResponse,
    ListUtxosResponse,
    RescanBlockchainResponse,
    RescanInfoResponse,
    SignMessageRequest,
    SignMessageResponse,
    UTXOEntry,
    WalletDisplayAccount,
    WalletDisplayBranch,
    WalletDisplayEntry,
    WalletDisplayResponse,
    WalletInfo,
    YieldGenReportResponse,
)
from jmwalletd.state import DaemonState

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/{walletname}/display
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/display")
async def wallet_display(
    walletname: str,
    _auth: dict[str, Any] = Depends(require_auth),
    state: DaemonState = Depends(get_daemon_state),
) -> WalletDisplayResponse:
    """Return full wallet display with accounts, branches, and entries."""
    ws = state.wallet_service
    await ws.sync()

    accounts: list[WalletDisplayAccount] = []
    total_balance = 0
    total_available = 0

    for mixdepth in range(ws.mixdepth_count):
        balance = await ws.get_balance(mixdepth)
        total_balance += balance

        # Build external and internal branches.
        branches: list[WalletDisplayBranch] = []
        branch_defs = [(0, "external addresses\tm/84'"), (1, "internal addresses\tm/84'")]
        for change, branch_label in branch_defs:
            address_infos = ws.get_address_info_for_mixdepth(mixdepth, change)
            branch_balance = sum(ai.balance for ai in address_infos)

            entries: list[WalletDisplayEntry] = []
            for ai in address_infos:
                entries.append(
                    WalletDisplayEntry(
                        hd_path=ai.path,
                        address=ai.address,
                        amount=f"{ai.balance / 1e8:.8f}",
                        available_balance=f"{ai.balance / 1e8:.8f}",
                        status=ai.status,
                        label="",
                        extradata="",
                    )
                )

            branches.append(
                WalletDisplayBranch(
                    branch=f"{branch_label}/{mixdepth}'/{change}",
                    balance=f"{branch_balance / 1e8:.8f}",
                    available_balance=f"{branch_balance / 1e8:.8f}",
                    entries=entries,
                )
            )

        account_balance = balance
        total_available += account_balance

        accounts.append(
            WalletDisplayAccount(
                account=str(mixdepth),
                account_balance=f"{account_balance / 1e8:.8f}",
                available_balance=f"{account_balance / 1e8:.8f}",
                branches=branches,
            )
        )

    return WalletDisplayResponse(
        walletname=walletname,
        walletinfo=WalletInfo(
            wallet_name="JM wallet",
            total_balance=f"{total_balance / 1e8:.8f}",
            available_balance=f"{total_available / 1e8:.8f}",
            accounts=accounts,
        ),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/{walletname}/utxos
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/utxos")
async def list_utxos(
    walletname: str,
    _auth: dict[str, Any] = Depends(require_auth),
    state: DaemonState = Depends(get_daemon_state),
) -> ListUtxosResponse:
    """List all UTXOs in the wallet."""
    ws = state.wallet_service
    await ws.sync()
    utxo_entries: list[UTXOEntry] = []

    for mixdepth in range(ws.mixdepth_count):
        utxos = ws.utxo_cache.get(mixdepth, [])
        for u in utxos:
            utxo_entries.append(
                UTXOEntry(
                    utxo=u.outpoint,
                    address=u.address,
                    path=u.path,
                    label=u.label or "",
                    value=u.value,
                    tries=0,
                    tries_remaining=3,
                    external=False,
                    mixdepth=u.mixdepth,
                    confirmations=u.confirmations,
                    frozen=u.frozen,
                )
            )

    return ListUtxosResponse(utxos=utxo_entries)


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/{walletname}/address/new/{mixdepth}
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/address/new/{mixdepth}")
async def get_new_address(
    walletname: str,
    mixdepth: str,
    _auth: dict[str, Any] = Depends(require_auth),
    state: DaemonState = Depends(get_daemon_state),
) -> GetAddressResponse:
    """Get a new receive address for the specified mixdepth."""
    try:
        md = int(mixdepth)
    except ValueError as exc:
        raise InvalidRequestFormat(f"Invalid mixdepth: {mixdepth}") from exc

    ws = state.wallet_service
    if md < 0 or md >= ws.mixdepth_count:
        raise InvalidRequestFormat(f"Mixdepth {md} out of range (0-{ws.mixdepth_count - 1})")

    address = ws.get_new_address(md)
    return GetAddressResponse(address=address)


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/{walletname}/address/timelock/new/{lockdate}
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/address/timelock/new/{lockdate}")
async def get_timelock_address(
    walletname: str,
    lockdate: str,
    _auth: dict[str, Any] = Depends(require_auth),
    state: DaemonState = Depends(get_daemon_state),
) -> GetAddressResponse:
    """Get a new timelocked (fidelity bond) address.

    The lockdate should be in YYYY-mm format (e.g., "2025-06").
    """
    try:
        parts = lockdate.split("-")
        if len(parts) != 2:
            msg = "Expected YYYY-mm"
            raise ValueError(msg)
        year, month = int(parts[0]), int(parts[1])
        if month < 1 or month > 12:
            msg = "Month must be 1-12"
            raise ValueError(msg)
    except (ValueError, IndexError) as exc:
        msg = f"Invalid lockdate format: {lockdate}. Expected YYYY-mm."
        raise InvalidRequestFormat(msg) from exc

    ws = state.wallet_service
    # Convert lockdate to locktime (first day of the month as UNIX timestamp).
    dt = datetime.datetime(year, month, 1, tzinfo=datetime.UTC)
    locktime = calendar.timegm(dt.timetuple())

    # Use the next available index for this locktime so callers can generate
    # multiple addresses per expiry month without collision.
    registry = load_registry(state.data_dir)
    existing_indices = {b.index for b in registry.bonds if b.locktime == locktime}
    index = 0
    while index in existing_indices:
        index += 1

    address = ws.get_fidelity_bond_address(index, locktime)

    # Persist the bond to the registry so the maker can pick it up at startup.
    if not registry.get_bond_by_address(address):
        pubkey_hex = (
            ws.get_fidelity_bond_key(index, locktime).get_public_key_bytes(compressed=True).hex()
        )
        witness_script = ws.get_fidelity_bond_script(index, locktime)
        path = f"{ws.root_path}/0'/{FIDELITY_BOND_BRANCH}/{index}"
        bond_info = create_bond_info(
            address=address,
            locktime=locktime,
            index=index,
            path=path,
            pubkey_hex=pubkey_hex,
            witness_script=witness_script,
            network=ws.network,
        )
        registry.add_bond(bond_info)
        save_registry(registry, state.data_dir)
        logger.debug(
            "Registered fidelity bond address {} (index={}, locktime={}) in registry",
            address,
            index,
            locktime,
        )

    return GetAddressResponse(address=address)


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/{walletname}/getseed
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/getseed")
async def get_seed(
    walletname: str,
    _auth: dict[str, Any] = Depends(require_auth),
    state: DaemonState = Depends(get_daemon_state),
) -> GetSeedResponse:
    """Return the wallet's BIP39 mnemonic seed phrase."""
    ws = state.wallet_service
    return GetSeedResponse(seedphrase=ws.mnemonic)


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/freeze
# ---------------------------------------------------------------------------
@router.post("/wallet/{walletname}/freeze")
async def freeze_utxo(
    walletname: str,
    body: FreezeRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    state: DaemonState = Depends(get_daemon_state),
) -> dict[str, str]:
    """Freeze or unfreeze a UTXO."""
    if state.taker_running:
        raise ActionNotAllowed("Cannot freeze/unfreeze UTXOs while a coinjoin is in progress.")

    # Validate utxo format: "txid:vout"
    utxo_str = body.utxo_string
    parts = utxo_str.split(":")
    if len(parts) != 2:
        raise InvalidRequestFormat(f"Invalid UTXO format: {utxo_str}. Expected txid:vout.")

    try:
        int(parts[1])
    except ValueError as exc:
        raise InvalidRequestFormat(f"Invalid vout in UTXO: {utxo_str}") from exc

    ws = state.wallet_service
    if body.freeze:
        ws.freeze_utxo(utxo_str)
    else:
        ws.unfreeze_utxo(utxo_str)

    return {}


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/configget
# ---------------------------------------------------------------------------

# Translation table for reference JoinMarket ``[POLICY]`` field names that
# differ in our Pydantic settings or live in a different sub-model.
_POLICY_FIELD_MAP: dict[str, tuple[str, str]] = {
    # reference_field -> (our_settings_attr, our_field_name)
    "tx_fees": ("wallet", "default_fee_block_target"),
    "tx_fees_factor": ("taker", "tx_fee_factor"),
    "max_cj_fee_abs": ("taker", "max_cj_fee_abs"),
    "max_cj_fee_rel": ("taker", "max_cj_fee_rel"),
    "minimum_makers": ("taker", "minimum_makers"),
    "gaplimit": ("wallet", "gap_limit"),
}

# Sensible defaults for fields the reference has but we don't model.
_POLICY_DEFAULTS: dict[str, str] = {
    "max_sweep_fee_change": "0.8",
}


@router.post("/wallet/{walletname}/configget")
async def config_get(
    walletname: str,
    body: ConfigGetRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    state: DaemonState = Depends(get_daemon_state),
) -> ConfigGetResponse:
    """Read a config variable (in-memory override takes priority)."""
    section = body.section.upper()
    field_name = body.field.lower()

    # Check in-memory overrides first.
    if section in state.config_overrides and field_name in state.config_overrides[section]:
        return ConfigGetResponse(configvalue=state.config_overrides[section][field_name])

    # Fall back to the settings system.
    from jmcore.settings import get_settings

    try:
        settings = get_settings()

        # Special handling for POLICY fields that JAM requests.
        if section == "POLICY":
            if field_name in _POLICY_DEFAULTS:
                return ConfigGetResponse(configvalue=_POLICY_DEFAULTS[field_name])

            if field_name in _POLICY_FIELD_MAP:
                attr, real_field = _POLICY_FIELD_MAP[field_name]
                subsettings = getattr(settings, attr)
                value = getattr(subsettings, real_field)
                return ConfigGetResponse(configvalue=str(value))

        value = _get_setting_value(settings, section, field_name)
        return ConfigGetResponse(configvalue=str(value))
    except (AttributeError, KeyError) as exc:
        raise ConfigNotPresent(f"Config not found: [{section}] {field_name}") from exc


def _get_setting_value(settings: Any, section: str, field: str) -> Any:
    """Look up a config value from the settings hierarchy.

    Maps the reference implementation's config section names to our
    Pydantic settings structure.
    """
    # Map reference section names to our settings attributes.
    section_map: dict[str, str] = {
        "POLICY": "wallet",
        "BLOCKCHAIN": "bitcoin",
        "TIMEOUT": "network",
        "LOGGING": "logging",
        "DAEMON": "network",
    }

    attr_name = section_map.get(section, section.lower())
    subsettings = getattr(settings, attr_name, None)
    if subsettings is None:
        msg = f"Unknown section: {section}"
        raise KeyError(msg)

    value = getattr(subsettings, field, None)
    if value is None:
        msg = f"Unknown field: {field}"
        raise KeyError(msg)

    return value


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/configset
# ---------------------------------------------------------------------------
@router.post("/wallet/{walletname}/configset")
async def config_set(
    walletname: str,
    body: ConfigSetRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    state: DaemonState = Depends(get_daemon_state),
) -> dict[str, str]:
    """Set a config variable (in-memory only, not persisted)."""
    section = body.section.upper()
    field_name = body.field.lower()

    if section not in state.config_overrides:
        state.config_overrides[section] = {}

    state.config_overrides[section][field_name] = body.value
    logger.info("Config override set: [{}] {} = {}", section, field_name, body.value)
    return {}


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/{walletname}/rescanblockchain/{blockheight}
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/rescanblockchain/{blockheight}")
async def rescan_blockchain(
    walletname: str,
    blockheight: int,
    _auth: dict[str, Any] = Depends(require_auth),
    state: DaemonState = Depends(get_daemon_state),
) -> RescanBlockchainResponse:
    """Trigger a blockchain rescan from the given block height."""
    import asyncio

    ws = state.wallet_service
    backend = ws.backend

    # rescan_blockchain is only available on DescriptorWalletBackend.
    if not hasattr(backend, "rescan_blockchain"):
        raise ActionNotAllowed("Rescan not supported by the current backend.")

    state.rescanning = True
    state.rescan_progress = 0.0

    async def _do_rescan() -> None:
        try:
            await backend.rescan_blockchain(blockheight)
        except Exception:
            logger.exception("Rescan failed")
        finally:
            state.rescanning = False

    asyncio.create_task(_do_rescan())
    return RescanBlockchainResponse(walletname=walletname)


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/{walletname}/getrescaninfo
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/getrescaninfo")
async def get_rescan_info(
    walletname: str,
    _auth: dict[str, Any] = Depends(require_auth),
    state: DaemonState = Depends(get_daemon_state),
) -> RescanInfoResponse:
    """Get rescan progress information."""
    if state.rescanning:
        return RescanInfoResponse(rescanning=True, progress=state.rescan_progress)
    return RescanInfoResponse(rescanning=False)


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/signmessage
# ---------------------------------------------------------------------------
@router.post("/wallet/{walletname}/signmessage")
async def sign_message(
    walletname: str,
    body: SignMessageRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    state: DaemonState = Depends(get_daemon_state),
) -> SignMessageResponse:
    """Sign a message with a wallet key at the given HD path."""
    ws = state.wallet_service

    try:
        # Parse the HD path to derive the address, then get the key.
        # The hd_path format is like "m/84'/0'/0'/0/5".
        # We look up the address from the path components.
        parts = body.hd_path.replace("'", "").split("/")
        if len(parts) < 5:
            raise InvalidRequestFormat(f"Invalid HD path: {body.hd_path}")

        # Extract mixdepth, change, index from the path.
        mixdepth = int(parts[3])
        change = int(parts[4]) if len(parts) > 4 else 0
        index = int(parts[5]) if len(parts) > 5 else 0

        address = ws.get_address(mixdepth, change, index)
        key = ws.get_key_for_address(address)
        if key is None:
            raise InvalidRequestFormat(f"Cannot derive key for path: {body.hd_path}")

        # Sign the message using Bitcoin message format (via coincurve).
        from jmcore.crypto import bitcoin_message_hash

        msg_hash = bitcoin_message_hash(body.message)

        from coincurve import PrivateKey

        privkey = PrivateKey(key.private_key)
        sig = privkey.sign_recoverable(msg_hash, hasher=None)
        import base64

        signature = base64.b64encode(sig).decode()

        return SignMessageResponse(
            signature=signature,
            message=body.message,
            address=address,
        )
    except (InvalidRequestFormat, ValueError) as exc:
        raise InvalidRequestFormat(f"Signing failed: {exc}") from exc
    except Exception as exc:
        raise InvalidRequestFormat(f"Signing failed: {exc}") from exc


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/yieldgen/report
# ---------------------------------------------------------------------------
@router.get("/wallet/yieldgen/report")
async def yieldgen_report(
    state: DaemonState = Depends(get_daemon_state),
) -> YieldGenReportResponse:
    """Return the yield generator CSV report data."""
    report_path = state.data_dir / "yigen-statement.csv"

    if not report_path.exists():
        raise YieldGeneratorDataUnreadable()

    try:
        lines = report_path.read_text().strip().split("\n")
        return YieldGenReportResponse(yigen_data=lines)
    except Exception as exc:
        raise YieldGeneratorDataUnreadable(str(exc)) from exc
