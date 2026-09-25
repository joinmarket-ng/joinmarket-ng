"""Reusable direct-send (non-CoinJoin) transaction building, signing, and broadcasting.

This module contains the core spending logic extracted from the CLI so that both
the CLI and the ``jmwalletd`` HTTP daemon can share it without duplication.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING

from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    create_p2tr_scriptpubkey,
    get_txid,
    scriptpubkey_to_address,
    serialize_transaction,
)
from jmcore.btc_script import mk_freeze_script
from jmcore.randomness import secure_random
from jmcore.transaction_policy import (
    MAX_LOCKTIME,
    NON_RBF_LOCKTIME_SEQUENCE,
    RBF_SEQUENCE,
    compute_tx_locktime,
)
from loguru import logger

from jmwallet.wallet.address import pubkey_to_p2wpkh_script
from jmwallet.wallet.coin_selection import estimate_direct_send_fee
from jmwallet.wallet.constants import FIDELITY_BOND_BRANCH
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.signing import deserialize_transaction

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend
    from jmwallet.wallet.coin_selection import DirectSendSelection
    from jmwallet.wallet.service import WalletService


DUST_THRESHOLD = 546

# Default safety cap on fee rate (sat/vB) used by direct-send transactions.
# This is the fallback when callers don't override via the
# ``max_fee_rate_sat_vb`` parameter (typically wired from
# ``WalletSettings.max_fee_rate_sat_vb``).  It protects against:
#
# * Backends that report wildly inflated fee estimates (RPC bug, hijacked
#   fee oracle, hostile rogue node).
# * UI / scripting bugs that pass a fee rate in the wrong unit (BTC/kvB
#   instead of sat/vB), or with a misplaced decimal point.
# * Malicious upper-layer code attempting to grief a wallet by burning the
#   entire balance to fees.
#
# Above this cap a transaction is refused with :class:`ExcessiveFeeRateError`
# rather than silently broadcasting.
DEFAULT_MAX_FEE_RATE_SAT_VB: float = 1_000.0


class ExcessiveFeeRateError(ValueError):
    """Raised when a resolved fee rate exceeds the configured safety cap.

    Subclasses :class:`ValueError` so existing ``except ValueError`` handlers
    in the CLI and HTTP layers continue to behave correctly (refuse the
    transaction with a user-visible error) without needing to know about the
    new exception type.
    """


def enforce_fee_rate_cap(fee_rate: float, max_fee_rate_sat_vb: float, *, source: str) -> None:
    """Reject *fee_rate* if it exceeds the configured cap.

    Parameters
    ----------
    fee_rate:
        The candidate fee rate in sat/vB.
    max_fee_rate_sat_vb:
        The safety cap.  Must be positive.
    source:
        Human-readable description of where the rate came from
        (``"manual"``, ``"backend estimate"``, ...).  Included verbatim in
        the error message to make misconfiguration easy to debug.

    Raises
    ------
    ExcessiveFeeRateError
        If ``fee_rate`` exceeds ``max_fee_rate_sat_vb``.
    """
    if not math.isfinite(fee_rate) or fee_rate <= 0:
        msg = f"{source} fee rate must be a finite positive number, got {fee_rate!r}"
        raise ExcessiveFeeRateError(msg)
    if fee_rate > max_fee_rate_sat_vb:
        msg = (
            f"{source} fee rate {fee_rate:.2f} sat/vB exceeds safety cap "
            f"{max_fee_rate_sat_vb:.2f} sat/vB. "
            "Raise the cap explicitly (settings.wallet.max_fee_rate_sat_vb) "
            "only if you really intend to pay this much."
        )
        raise ExcessiveFeeRateError(msg)


@dataclass
class DirectSendResult:
    """Result returned by :func:`direct_send`."""

    txid: str
    tx_hex: str
    fee: int
    fee_rate: float
    send_amount: int
    change_amount: int
    num_inputs: int
    num_outputs: int
    inputs: list[dict[str, object]] = field(default_factory=list)
    outputs: list[dict[str, object]] = field(default_factory=list)
    version: int = 2
    locktime: int = 0


@dataclass
class SignedDirectTx:
    """Intermediate result from :func:`prepare_direct_send`."""

    txid: str
    tx_hex: str
    fee: int
    fee_rate: float
    send_amount: int
    change_amount: int
    num_inputs: int
    num_outputs: int
    destination: str
    change_address: str = ""
    selected_utxos: list[tuple[str, int]] = field(default_factory=list)
    source_addresses: list[str] = field(default_factory=list)
    inputs: list[dict[str, object]] = field(default_factory=list)
    outputs: list[dict[str, object]] = field(default_factory=list)
    version: int = 2
    locktime: int = 0


@dataclass(frozen=True)
class DirectTxOutput:
    """Validated output data used by the shared direct-send signer."""

    value_sats: int
    script_pubkey: bytes
    address: str


@dataclass
class BuiltDirectTx:
    """Signed direct transaction plus its final wire-order metadata."""

    raw: bytes
    inputs: list[UTXOInfo]
    outputs: list[DirectTxOutput]
    sequence: int
    locktime: int
    version: int = 2


def resolve_broadcast_txid(
    tx_hex: str,
    backend_txid: str | None,
    *,
    local_txid: str | None = None,
) -> str:
    """Return the txid committed by signed bytes and report backend disagreement."""
    authoritative_txid = local_txid or get_txid(tx_hex)
    if (
        isinstance(backend_txid, str)
        and backend_txid
        and backend_txid.lower() != authoritative_txid
    ):
        logger.bind(sensitive=True).warning(
            "Backend returned txid {} for transaction {}", backend_txid, authoritative_txid
        )
    return authoritative_txid


# Map of wallet networks to python-bitcointx chain parameter names. Used so
# CCoinAddress can both verify the bech32/base58 checksum AND reject
# addresses from a different network than the wallet is configured for.
_NETWORK_CHAIN_PARAMS: dict[str, str] = {
    "mainnet": "bitcoin",
    "testnet": "bitcoin/testnet",
    "signet": "bitcoin/signet",
    "regtest": "bitcoin/regtest",
}


def _decode_bech32_scriptpubkey(address: str, *, network: str | None = None) -> bytes:
    """Decode a Bitcoin address into its scriptPubKey bytes.

    Delegates to ``python-bitcointx``'s :class:`CCoinAddress`, which
    verifies the BIP173/BIP350 checksum (bech32 / bech32m), rejects
    wrong-network addresses under the active :class:`ChainParams`, and
    supports every standard address type (P2WPKH, P2WSH, P2TR, P2PKH,
    P2SH).

    Args:
        address: Destination Bitcoin address as a string.
        network: Wallet network. When provided, address parsing happens
            inside :class:`bitcointx.ChainParams` for the matching chain
            so a mainnet address is rejected on testnet (and vice versa).

    Raises:
        ValueError: If the address is malformed, has a bad checksum, or
            does not belong to the requested network.
    """
    # Imported lazily to keep test-import cost low and to keep the
    # bitcointx dependency optional for callers that never touch
    # direct-send.
    from bitcointx import ChainParams
    from bitcointx.wallet import CCoinAddress, CCoinAddressError

    chain = _NETWORK_CHAIN_PARAMS.get(network) if network is not None else None
    if network is not None and chain is None:
        msg = f"Unsupported network for address decoding: {network!r}"
        raise ValueError(msg)

    try:
        if chain is not None:
            with ChainParams(chain):
                return bytes(CCoinAddress(address).to_scriptPubKey())
        return bytes(CCoinAddress(address).to_scriptPubKey())
    except CCoinAddressError as exc:
        msg = f"Invalid destination address {address!r} (bad checksum, format, or wrong network)"
        raise ValueError(msg) from exc


def select_spendable_utxos(
    utxos: list[UTXOInfo],
    *,
    include_frozen: bool = False,
    include_fidelity_bonds: bool = False,
    locktime_cutoff: int | None = None,
) -> list[UTXOInfo]:
    """Filter UTXOs to only those safe for auto-spending.

    Frozen UTXOs and all fidelity bonds are excluded by default. Setting
    ``include_fidelity_bonds`` admits only bonds whose locktime is strictly
    below ``locktime_cutoff``. The cutoff should be chain median-time-past for
    transaction construction; it defaults to the host time for display-only
    callers.
    """
    cutoff = int(time.time()) if locktime_cutoff is None else locktime_cutoff
    result = []
    for u in utxos:
        if not include_frozen and u.frozen:
            continue
        if u.is_fidelity_bond:
            if not include_fidelity_bonds:
                continue
            if u.locktime is None or u.locktime >= cutoff:
                continue
        result.append(u)
    return result


def parse_outpoint(raw: str) -> tuple[str, int]:
    """Parse a ``txid:vout`` outpoint string into ``(txid, vout)``.

    The txid is lowercased so callers can compare it against
    :attr:`UTXOInfo.txid` without worrying about the case the client used.

    Raises:
        ValueError: If the string is not a well-formed outpoint.
    """
    text = raw.strip()
    txid, separator, vout_text = text.rpartition(":")
    if not separator:
        msg = f"Invalid input UTXO {raw!r}: expected format 'txid:vout'"
        raise ValueError(msg)
    if len(txid) != 64 or any(c not in "0123456789abcdefABCDEF" for c in txid):
        msg = f"Invalid input UTXO {raw!r}: {txid!r} is not a 64-character hex txid"
        raise ValueError(msg)
    if not (vout_text.isascii() and vout_text.isdigit()):
        msg = f"Invalid input UTXO {raw!r}: vout {vout_text!r} is not a non-negative integer"
        raise ValueError(msg)
    return txid.lower(), int(vout_text)


def _find_owning_mixdepth(wallet: WalletService, txid: str, vout: int) -> int | None:
    """Look for an outpoint in already-synced mixdepths other than the requested one.

    Reads only :attr:`WalletService.utxo_cache` so an error path never triggers
    a fresh sync. Returns ``None`` when the outpoint is not in the cache, which
    just means we cannot say more than "not found".
    """
    for other_mixdepth, cached in wallet.utxo_cache.items():
        for utxo in cached:
            if utxo.txid == txid and utxo.vout == vout:
                return other_mixdepth
    return None


def _ensure_direct_send_outpoints_unlocked(
    wallet: WalletService, outpoints: list[tuple[str, int]]
) -> None:
    """Reject inputs currently reserved by an in-flight CoinJoin."""
    locked_outpoints = {(txid.lower(), vout) for txid, vout in wallet.get_locked_input_outpoints()}
    for txid, vout in outpoints:
        if (txid.lower(), vout) in locked_outpoints:
            msg = f"Input UTXO {txid}:{vout} is locked by another in-flight CoinJoin"
            raise ValueError(msg)


async def resolve_input_utxos(
    *,
    wallet: WalletService,
    backend: BlockchainBackend,
    mixdepth: int,
    input_utxos: list[str],
    allow_fidelity_bonds: bool = True,
    allow_conflicts: bool = False,
) -> tuple[list[UTXOInfo], int | None]:
    """Resolve explicit ``txid:vout`` strings into spendable :class:`UTXOInfo`.

    Every listed outpoint must exist in *mixdepth*, be unfrozen, and be
    signable by this wallet. When ``allow_conflicts`` is true, an absent named
    outpoint may instead be reconstructed only when the backend proves that a
    current mempool transaction spends it. When ``allow_fidelity_bonds`` is
    true, fidelity bonds are admitted only when their timelock has already
    expired against chain median-time-past, since the caller selected them
    deliberately. There is no fallback to automatic selection: anything
    unusable raises :class:`ValueError` naming the reason.

    Returns ``(utxos, locktime_cutoff)`` with the UTXOs in the order given.
    ``locktime_cutoff`` is the median-time-past that was fetched to validate
    fidelity bonds, or *None* when no bond was selected.
    """
    if not input_utxos:
        msg = "input_utxos must not be empty; omit it to use automatic coin selection"
        raise ValueError(msg)

    outpoints: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for raw in input_utxos:
        outpoint = parse_outpoint(raw)
        if outpoint in seen:
            msg = f"Duplicate input UTXO {outpoint[0]}:{outpoint[1]}"
            raise ValueError(msg)
        seen.add(outpoint)
        outpoints.append(outpoint)

    _ensure_direct_send_outpoints_unlocked(wallet, outpoints)
    available = {(u.txid, u.vout): u for u in await wallet.get_utxos(mixdepth)}

    utxos: list[UTXOInfo] = []
    reconstructed_conflicts = 0
    for txid, vout in outpoints:
        utxo = available.get((txid, vout))
        if utxo is None:
            if not allow_conflicts:
                owner = _find_owning_mixdepth(wallet, txid, vout)
                if owner is not None:
                    msg = (
                        f"Input UTXO {txid}:{vout} is in mixdepth {owner}, "
                        f"not the requested mixdepth {mixdepth}"
                    )
                else:
                    msg = f"Input UTXO {txid}:{vout} not found in mixdepth {mixdepth}"
                raise ValueError(msg)
            utxo = await _reconstruct_conflicted_input(wallet, backend, mixdepth, txid, vout)
            reconstructed_conflicts += 1
        if utxo.frozen:
            msg = f"Input UTXO {txid}:{vout} is frozen; unfreeze it before spending"
            raise ValueError(msg)
        if utxo.is_fidelity_bond and not allow_fidelity_bonds:
            msg = f"Input UTXO {txid}:{vout} is a fidelity bond; CoinJoin inputs cannot be bonds"
            raise ValueError(msg)
        utxos.append(utxo)

    if allow_conflicts and reconstructed_conflicts == 0:
        msg = (
            "--allow-conflicts requires at least one named input currently spent by a "
            "mempool transaction"
        )
        raise ValueError(msg)

    # Fidelity bonds need chain time to check expiry, so only pay for the
    # median-time-past round trip when one was actually selected.
    locktime_cutoff: int | None = None
    if any(u.is_fidelity_bond for u in utxos):
        locktime_cutoff = await backend.get_median_time_past()
        for utxo in utxos:
            if not utxo.is_fidelity_bond:
                continue
            if utxo.locktime is None or utxo.locktime >= locktime_cutoff:
                msg = (
                    f"Input UTXO {utxo.txid}:{utxo.vout} is a fidelity bond whose "
                    f"timelock {utxo.locktime} has not passed chain time {locktime_cutoff}"
                )
                raise ValueError(msg)
            if not _is_signable_fidelity_bond(wallet, utxo):
                msg = (
                    f"Input UTXO {utxo.txid}:{utxo.vout} is a fidelity bond this wallet cannot sign"
                )
                raise ValueError(msg)

    return utxos, locktime_cutoff


async def _reconstruct_conflicted_input(
    wallet: WalletService,
    backend: BlockchainBackend,
    mixdepth: int,
    txid: str,
    vout: int,
) -> UTXOInfo:
    """Recreate a wallet input proven spent by a current mempool transaction."""
    try:
        spender = await backend.get_mempool_spender(txid, vout)
    except NotImplementedError as exc:
        raise ValueError("Backend does not support authoritative mempool conflict lookup") from exc
    except Exception as exc:
        raise ValueError(
            f"Could not verify mempool conflict for input UTXO {txid}:{vout}: {exc}"
        ) from exc
    if spender.blockhash is not None:
        msg = f"Input UTXO {txid}:{vout} was spent in confirmed block {spender.blockhash}"
        raise ValueError(msg)
    if not spender.spending_txid:
        msg = f"Input UTXO {txid}:{vout} has no current mempool spender"
        raise ValueError(msg)
    logger.warning(
        f"Input UTXO {txid}:{vout} is currently spent by mempool transaction "
        f"{spender.spending_txid}; preparing an explicit conflict replacement"
    )

    try:
        parent = await backend.get_wallet_transaction(txid)
    except NotImplementedError as exc:
        raise ValueError(
            "Backend does not support wallet transaction lookup for conflict inputs"
        ) from exc
    if parent is None or parent.confirmations <= 0 or not parent.raw:
        msg = f"Input UTXO {txid}:{vout} parent transaction is not confirmed and wallet-accessible"
        raise ValueError(msg)
    try:
        parent_bytes = bytes.fromhex(parent.raw)
        if get_txid(parent.raw) != txid:
            raise ValueError("parent txid mismatch")
        parent_tx = deserialize_transaction(parent_bytes)
    except Exception as exc:
        raise ValueError(f"Input UTXO {txid}:{vout} parent transaction is invalid") from exc
    if vout >= len(parent_tx.outputs):
        msg = f"Input UTXO {txid}:{vout} parent output index is invalid"
        raise ValueError(msg)

    output = parent_tx.outputs[vout]
    scriptpubkey = output.script.hex()
    try:
        address = scriptpubkey_to_address(output.script, wallet.network)
    except ValueError as exc:
        raise ValueError(f"Input UTXO {txid}:{vout} has an unknown wallet script") from exc

    path_info = wallet.address_cache.get(address) or wallet.address_cache.get(address.lower())
    if path_info is None:
        path_info = wallet._find_address_path(address)
    if path_info is None:
        msg = f"Input UTXO {txid}:{vout} script is not owned by this wallet"
        raise ValueError(msg)
    owner_mixdepth, branch, index = path_info
    if owner_mixdepth != mixdepth:
        msg = (
            f"Input UTXO {txid}:{vout} is in mixdepth {owner_mixdepth}, "
            f"not the requested mixdepth {mixdepth}"
        )
        raise ValueError(msg)
    outpoint = f"{txid}:{vout}"
    if wallet.is_utxo_frozen(outpoint):
        msg = f"Input UTXO {outpoint} is frozen; unfreeze it before spending"
        raise ValueError(msg)

    locktime: int | None = None
    if branch == FIDELITY_BOND_BRANCH:
        locktime = wallet.get_locktime_for_address(address)
        if locktime is None:
            msg = f"Input UTXO {outpoint} fidelity bond metadata is unavailable"
            raise ValueError(msg)
        path = f"{wallet.get_fidelity_bond_path(index)}:{locktime}"
    elif branch in (0, 1):
        path = f"{wallet.root_path}/{owner_mixdepth}'/{branch}/{index}"
    else:
        msg = f"Input UTXO {outpoint} has an unsupported wallet derivation branch"
        raise ValueError(msg)

    reconstructed = UTXOInfo(
        txid=txid,
        vout=vout,
        value=output.value,
        address=address,
        confirmations=parent.confirmations,
        scriptpubkey=scriptpubkey,
        path=path,
        mixdepth=owner_mixdepth,
        locktime=locktime,
        frozen=False,
    )
    if reconstructed.is_fidelity_bond:
        if not _is_signable_fidelity_bond(wallet, reconstructed):
            msg = f"Input UTXO {outpoint} is a fidelity bond this wallet cannot sign"
            raise ValueError(msg)
    elif not (reconstructed.is_p2wpkh or reconstructed.is_p2tr):
        msg = f"Input UTXO {outpoint} must be a wallet P2WPKH or P2TR output"
        raise ValueError(msg)
    else:
        key = wallet.get_key_for_address(address)
        if key is None:
            msg = f"Input UTXO {outpoint} has no signing key"
            raise ValueError(msg)
        expected_script = (
            create_p2tr_scriptpubkey(key.get_p2tr_output_xonly())
            if reconstructed.is_p2tr
            else pubkey_to_p2wpkh_script(key.get_public_key_bytes(compressed=True).hex())
        ).hex()
        if scriptpubkey.lower() != expected_script:
            msg = f"Input UTXO {outpoint} script does not match this wallet's signing key"
            raise ValueError(msg)
    return reconstructed


def _is_signable_fidelity_bond(wallet: WalletService, utxo: UTXOInfo) -> bool:
    """Return whether this wallet derives the script key for a bond UTXO."""
    if not utxo.is_fidelity_bond or utxo.locktime is None or not utxo.is_p2wsh:
        return False
    try:
        key = wallet.get_key_for_address(utxo.address)
    except Exception:
        return False
    if key is None:
        return False
    witness_script = mk_freeze_script(
        key.get_public_key_bytes(compressed=True).hex(), utxo.locktime
    )
    expected_scriptpubkey = b"\x00\x20" + sha256(witness_script).digest()
    return utxo.scriptpubkey.lower() == expected_scriptpubkey.hex()


def estimate_fee(
    utxos: list[UTXOInfo],
    destination: str,
    fee_rate: float,
    *,
    has_change: bool,
) -> tuple[int, int]:
    """Estimate the transaction fee and vsize.

    Thin wrapper kept for its existing callers; the single implementation
    lives in coin_selection so selection and signing cannot drift apart.

    Returns ``(fee, vsize)``.
    """
    return estimate_direct_send_fee(utxos, destination, fee_rate, has_change=has_change)


async def select_automatic_direct_send_inputs(
    *,
    wallet: WalletService,
    amount_sats: int,
    destination: str,
    fee_rate: float,
    mixdepth: int | None,
) -> tuple[DirectSendSelection, int]:
    """Select a direct-send source and inputs using the shared privacy policy.

    An explicit mixdepth is authoritative. Otherwise, mixdepths are considered
    from highest to lowest and the first one with a sufficient admissible
    selection wins, regardless of how many inputs a lower mixdepth would need.
    """
    if amount_sats <= 0:
        raise ValueError("Automatic source selection requires a positive send amount")

    from jmwallet.wallet.coin_selection import (
        DirectSendSearchLimitError,
        select_direct_send_utxos,
    )

    mixdepths = (
        [mixdepth] if mixdepth is not None else list(range(wallet.mixdepth_count - 1, -1, -1))
    )
    failures: list[str] = []
    for candidate_mixdepth in mixdepths:
        raw_utxos = await wallet.get_utxos(candidate_mixdepth)
        locked_outpoints = wallet.get_locked_input_outpoints()
        try:
            selection = select_direct_send_utxos(
                raw_utxos,
                amount_sats,
                destination,
                fee_rate,
                mixdepth=candidate_mixdepth,
                excluded_outpoints=locked_outpoints,
            )
        except DirectSendSearchLimitError:
            # The highest-priority source remains unresolved, so do not skip it.
            raise
        except ValueError as exc:
            failures.append(f"mixdepth {candidate_mixdepth}: {exc}")
            continue
        return selection, candidate_mixdepth

    if mixdepth is not None and failures:
        raise ValueError(failures[0])
    detail = "; ".join(failures)
    raise ValueError(f"No mixdepth has sufficient eligible funds ({detail})")


async def resolve_direct_send_locktime(
    *,
    backend: BlockchainBackend,
    utxos: list[UTXOInfo],
    locktime_cutoff: int | None = None,
) -> int:
    """Resolve a valid fidelity-bond or anti-fee-sniping locktime."""
    timelocked = [utxo for utxo in utxos if utxo.is_timelocked and utxo.locktime is not None]
    if not timelocked:
        return compute_tx_locktime(await backend.get_block_height())

    cutoff = await backend.get_median_time_past() if locktime_cutoff is None else locktime_cutoff
    for utxo in timelocked:
        assert utxo.locktime is not None
        if utxo.locktime >= cutoff:
            msg = (
                f"Cannot spend timelocked UTXO {utxo.txid}:{utxo.vout}: "
                f"locktime {utxo.locktime} has not passed chain time {cutoff}"
            )
            raise ValueError(msg)
    return max(utxo.locktime for utxo in timelocked if utxo.locktime is not None)


def build_and_sign_direct_tx(
    *,
    wallet: WalletService,
    utxos: list[UTXOInfo],
    outputs: list[DirectTxOutput],
    locktime: int,
    rbf: bool = True,
) -> BuiltDirectTx:
    """Shuffle, serialize, and sign a fully validated direct transaction."""
    if not utxos:
        raise ValueError("A direct transaction requires at least one input")
    if not outputs:
        raise ValueError("A direct transaction requires at least one output")
    if (
        not isinstance(locktime, int)
        or isinstance(locktime, bool)
        or not 0 <= locktime <= MAX_LOCKTIME
    ):
        raise ValueError(f"Invalid transaction locktime: {locktime!r}")

    outpoints = [(utxo.txid, utxo.vout) for utxo in utxos]
    if len(set(outpoints)) != len(outpoints):
        raise ValueError("A direct transaction cannot contain duplicate inputs")
    required_locktime = max((utxo.locktime or 0 for utxo in utxos if utxo.is_timelocked), default=0)
    if locktime < required_locktime:
        raise ValueError(
            f"Transaction locktime {locktime} does not satisfy input locktime {required_locktime}"
        )
    if any(output.value_sats <= 0 or not output.script_pubkey for output in outputs):
        raise ValueError("Direct transaction outputs require positive values and non-empty scripts")
    if sum(output.value_sats for output in outputs) > sum(utxo.value for utxo in utxos):
        raise ValueError("Direct transaction outputs exceed its input value")

    _ensure_direct_send_outpoints_unlocked(wallet, outpoints)
    ordered_utxos = list(utxos)
    ordered_outputs = list(outputs)
    secure_random.shuffle(ordered_utxos)
    secure_random.shuffle(ordered_outputs)

    sequence = RBF_SEQUENCE if rbf else NON_RBF_LOCKTIME_SEQUENCE
    tx_inputs = [
        TxInput.from_hex(utxo.txid, utxo.vout, sequence=sequence) for utxo in ordered_utxos
    ]
    tx_outputs = [
        TxOutput(value=output.value_sats, script=output.script_pubkey) for output in ordered_outputs
    ]
    unsigned_tx = serialize_transaction(2, tx_inputs, tx_outputs, locktime)
    parsed = deserialize_transaction(unsigned_tx)

    witnesses: list[list[bytes]] = []
    for index, utxo in enumerate(ordered_utxos):
        if utxo.is_p2tr:
            # BIP341 commits to every input's amount and script, in transaction order.
            witness = wallet.sign_input(
                parsed,
                index,
                utxo,
                prevout_values=[item.value for item in ordered_utxos],
                prevout_scripts=[bytes.fromhex(item.scriptpubkey) for item in ordered_utxos],
            ).witness
        else:
            witness = wallet.sign_input(parsed, index, utxo).witness
        if not witness or any(not isinstance(item, bytes) or not item for item in witness):
            raise ValueError(f"Wallet returned an invalid witness for input {index}")
        witnesses.append(witness)

    signed_tx = serialize_transaction(2, tx_inputs, tx_outputs, locktime, witnesses)
    return BuiltDirectTx(
        raw=signed_tx,
        inputs=ordered_utxos,
        outputs=ordered_outputs,
        sequence=sequence,
        locktime=locktime,
    )


async def prepare_direct_send(
    *,
    wallet: WalletService,
    backend: BlockchainBackend,
    mixdepth: int,
    amount_sats: int,
    destination: str,
    fee_rate: float | None = None,
    fee_target_blocks: int = 6,
    tx_fee_factor: float = 0.0,
    max_fee_rate_sat_vb: float = DEFAULT_MAX_FEE_RATE_SAT_VB,
    input_utxos: list[str] | None = None,
    rbf: bool = True,
) -> SignedDirectTx:
    """Build and sign a direct-send transaction WITHOUT broadcasting.

    When *input_utxos* is given as a list of ``txid:vout`` strings, exactly
    those UTXOs are spent and automatic coin selection is skipped entirely;
    anything unusable raises :class:`ValueError` rather than falling back. An
    empty list is an error — omit the argument for automatic selection.
    ``rbf`` controls BIP125 signaling and defaults to enabled.

    Returns a :class:`SignedDirectTx` containing the signed hex and all
    metadata needed to broadcast and record a history entry. Callers that want
    the full build+sign+broadcast flow should use :func:`direct_send` instead.
    """
    if not destination.startswith(("bc1", "tb1", "bcrt1")):
        msg = "Only bech32 addresses are currently supported"
        raise ValueError(msg)

    # Validate the destination address up front (checksum + HRP + network).
    # We compute the scriptPubKey now so a malformed address fails fast,
    # before any fee estimation or UTXO selection side effects.
    network = getattr(wallet, "network", None)
    dest_script = _decode_bech32_scriptpubkey(destination, network=network)

    # --- Fee rate resolution ---
    fee_source = "manual"
    if fee_rate is None:
        fee_rate = await backend.estimate_fee(target_blocks=fee_target_blocks)
        logger.debug("Estimated fee rate: {:.2f} sat/vB ({} blocks)", fee_rate, fee_target_blocks)
        fee_source = "backend estimate"

    enforce_fee_rate_cap(fee_rate, max_fee_rate_sat_vb, source=fee_source)
    if not math.isfinite(tx_fee_factor) or tx_fee_factor < 0:
        msg = f"tx_fee_factor must be a finite non-negative number, got {tx_fee_factor!r}"
        raise ValueError(msg)
    if tx_fee_factor > 0:
        upper_rate = min(fee_rate * (1 + tx_fee_factor), max_fee_rate_sat_vb)
        fee_rate = secure_random.uniform(fee_rate, upper_rate)
        logger.debug("Randomized direct-send fee rate: {:.2f} sat/vB", fee_rate)
    enforce_fee_rate_cap(fee_rate, max_fee_rate_sat_vb, source="final")

    # --- UTXO selection ---
    utxos: list[UTXOInfo]
    locktime_cutoff: int | None = None
    if input_utxos is not None:
        # Explicit coin control (issue #587): spend exactly what was listed,
        # including for sweeps, with no fallback to automatic selection.
        utxos, locktime_cutoff = await resolve_input_utxos(
            wallet=wallet,
            backend=backend,
            mixdepth=mixdepth,
            input_utxos=input_utxos,
        )
    elif amount_sats == 0:
        # Sweep regular coins by default. If there are none, admit expired
        # hot-wallet bonds. This supports explicit bond-redemption flows that
        # freeze every other coin without making bonds part of normal
        # auto-selection or linking them to unrelated funds.
        raw_utxos = await wallet.get_utxos(mixdepth)
        locked_outpoints = {
            (txid.lower(), vout) for txid, vout in wallet.get_locked_input_outpoints()
        }
        blocked_regular_scripts = {
            utxo.scriptpubkey
            for utxo in raw_utxos
            if not utxo.is_fidelity_bond and (utxo.txid, utxo.vout) in locked_outpoints
        }
        sweep_candidates = [
            utxo
            for utxo in raw_utxos
            if (utxo.txid, utxo.vout) not in locked_outpoints
            and (utxo.is_fidelity_bond or utxo.scriptpubkey not in blocked_regular_scripts)
        ]
        utxos = select_spendable_utxos(sweep_candidates)
        if not utxos and any(u.is_fidelity_bond and not u.frozen for u in sweep_candidates):
            locktime_cutoff = await backend.get_median_time_past()
            bond_candidates = select_spendable_utxos(
                sweep_candidates,
                include_fidelity_bonds=True,
                locktime_cutoff=locktime_cutoff,
            )
            utxos = [u for u in bond_candidates if _is_signable_fidelity_bond(wallet, u)]
    else:
        selection, _selected_mixdepth = await select_automatic_direct_send_inputs(
            wallet=wallet,
            amount_sats=amount_sats,
            destination=destination,
            fee_rate=fee_rate,
            mixdepth=mixdepth,
        )
        utxos = selection.utxos

    if not utxos:
        msg = f"No spendable UTXOs in mixdepth {mixdepth}"
        raise ValueError(msg)

    total_input = sum(u.value for u in utxos)
    is_sweep = amount_sats == 0

    # --- Fee estimation ---
    has_change = not is_sweep
    fee, _vsize = estimate_fee(utxos, destination, fee_rate, has_change=has_change)

    if is_sweep:
        send_amount = total_input - fee
        if send_amount <= 0:
            msg = "Insufficient funds after fee deduction for sweep"
            raise ValueError(msg)
        change_amount = 0
    else:
        send_amount = amount_sats
        change_amount = total_input - send_amount - fee
        if change_amount < DUST_THRESHOLD:
            minimum_no_change_fee, _ = estimate_fee(utxos, destination, fee_rate, has_change=False)
            if total_input < send_amount + minimum_no_change_fee:
                msg = (
                    f"Insufficient funds: need {send_amount + minimum_no_change_fee}, "
                    f"have {total_input}"
                )
                raise ValueError(msg)
            # With no change output, every satoshi not sent is the actual fee.
            # Keep the reported fee consistent with the serialized transaction.
            fee = total_input - send_amount
            change_amount = 0

    # --- Destination scriptPubKey ---
    # (already validated and computed at the top of this function)

    # --- Change output ---
    change_script: bytes | None = None
    change_addr: str = ""
    if change_amount > 0:
        change_addr = wallet.get_new_internal_address(mixdepth)
        change_key = wallet.get_key_for_address(change_addr)
        if change_key is None:
            msg = f"Cannot derive key for change address {change_addr}"
            raise ValueError(msg)
        # Derive the script from the address the wallet reports, not from a
        # hardcoded P2WPKH template: a Taproot (BIP86) wallet would otherwise
        # pay change to a P2WPKH script that its own tr() descriptors cannot
        # see, silently hiding the funds.
        change_script = _decode_bech32_scriptpubkey(change_addr, network=network)

    outputs = [
        DirectTxOutput(
            value_sats=send_amount,
            script_pubkey=dest_script,
            address=destination,
        )
    ]
    if change_amount > 0 and change_script is not None:
        outputs.append(
            DirectTxOutput(
                value_sats=change_amount,
                script_pubkey=change_script,
                address=change_addr,
            )
        )

    locktime = await resolve_direct_send_locktime(
        backend=backend,
        utxos=utxos,
        locktime_cutoff=locktime_cutoff,
    )
    built = build_and_sign_direct_tx(
        wallet=wallet,
        utxos=utxos,
        outputs=outputs,
        locktime=locktime,
        rbf=rbf,
    )
    tx_hex = built.raw.hex()

    return SignedDirectTx(
        txid=get_txid(tx_hex),
        tx_hex=tx_hex,
        fee=fee,
        fee_rate=fee_rate,
        send_amount=send_amount,
        change_amount=change_amount,
        num_inputs=len(built.inputs),
        num_outputs=len(built.outputs),
        destination=destination,
        change_address=change_addr if change_amount > 0 else "",
        selected_utxos=[(u.txid, u.vout) for u in built.inputs],
        source_addresses=[u.address for u in built.inputs],
        inputs=[
            {
                "outpoint": f"{u.txid}:{u.vout}",
                "scriptSig": "",
                "nSequence": built.sequence,
                "witness": "",
            }
            for u in built.inputs
        ],
        outputs=[
            {
                "value_sats": output.value_sats,
                "scriptPubKey": output.script_pubkey.hex(),
                "address": output.address,
            }
            for output in built.outputs
        ],
        version=built.version,
        locktime=built.locktime,
    )


async def direct_send(
    *,
    wallet: WalletService,
    backend: BlockchainBackend,
    mixdepth: int,
    amount_sats: int,
    destination: str,
    fee_rate: float | None = None,
    fee_target_blocks: int = 6,
    tx_fee_factor: float = 0.0,
    max_fee_rate_sat_vb: float = DEFAULT_MAX_FEE_RATE_SAT_VB,
    input_utxos: list[str] | None = None,
    rbf: bool = True,
) -> DirectSendResult:
    """Build, sign, and broadcast a direct (non-CoinJoin) transaction.

    Parameters
    ----------
    wallet:
        An initialised and synced :class:`WalletService`.
    backend:
        The blockchain backend for fee estimation and broadcasting.
    mixdepth:
        The mixdepth (account) to spend from.
    amount_sats:
        Amount in satoshis to send.  ``0`` means sweep the entire mixdepth.
    destination:
        Destination Bitcoin address (bech32 only).
    fee_rate:
        Explicit fee rate in sat/vB.  When *None*, the rate is estimated
        from the backend using *fee_target_blocks*.
    fee_target_blocks:
        Number of blocks for fee estimation (ignored when *fee_rate* is set).
    tx_fee_factor:
        Privacy randomization factor. The final rate is selected between the
        resolved rate and that rate multiplied by ``1 + tx_fee_factor``, with
        the upper end limited by *max_fee_rate_sat_vb*.
    max_fee_rate_sat_vb:
        Safety cap on the fee rate (sat/vB).  The resolved rate (manual or
        from backend estimation) is rejected with
        :class:`ExcessiveFeeRateError` when it exceeds this value.  Defaults
        to :data:`DEFAULT_MAX_FEE_RATE_SAT_VB`; daemon and CLI callers wire
        this from ``settings.wallet.max_fee_rate_sat_vb``.
    input_utxos:
        Optional explicit list of ``txid:vout`` outpoints to spend.  When
        given, coin selection is skipped and exactly these UTXOs are used
        (also for sweeps); they must all be unfrozen and in *mixdepth*, or
        :class:`ValueError` is raised naming the reason.  An empty list is an
        error; pass *None* for automatic selection.
    rbf:
        Signal BIP125 opt-in RBF. Enabled by default; disabling it retains a
        non-final sequence so anti-fee-sniping locktime remains effective.

    Returns
    -------
    DirectSendResult
    """
    prepared = await prepare_direct_send(
        wallet=wallet,
        backend=backend,
        mixdepth=mixdepth,
        amount_sats=amount_sats,
        destination=destination,
        fee_rate=fee_rate,
        fee_target_blocks=fee_target_blocks,
        tx_fee_factor=tx_fee_factor,
        max_fee_rate_sat_vb=max_fee_rate_sat_vb,
        input_utxos=input_utxos,
        rbf=rbf,
    )

    tx_bytes_len = len(bytes.fromhex(prepared.tx_hex))
    logger.info("Broadcasting direct-send transaction ({} bytes)", tx_bytes_len)
    broadcast_txid = await backend.broadcast_transaction(prepared.tx_hex)
    txid = resolve_broadcast_txid(
        prepared.tx_hex,
        broadcast_txid,
        local_txid=prepared.txid,
    )

    logger.bind(sensitive=True).info("Broadcast OK: {}", txid)
    return DirectSendResult(
        txid=txid,
        tx_hex=prepared.tx_hex,
        fee=prepared.fee,
        fee_rate=prepared.fee_rate,
        send_amount=prepared.send_amount,
        change_amount=prepared.change_amount,
        num_inputs=prepared.num_inputs,
        num_outputs=prepared.num_outputs,
        inputs=prepared.inputs,
        outputs=prepared.outputs,
        version=prepared.version,
        locktime=prepared.locktime,
    )
