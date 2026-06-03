"""
Standalone recovery of reverse-submarine-swap lockup outputs.

When a CoinJoin that was supposed to spend a swap lockup never confirms (the
provider settled the hold invoice off a competing spend, the round was
aborted after lockup, or the process crashed), the locked on-chain funds are
still claimable by us: the HTLC claim path only requires the preimage and the
claim key, both of which we persist via :mod:`taker.swap.persistence`.

Critically, the claim path imposes **no** CLTV restriction (only the refund
path does), so we can sweep a lockup output at any time while it remains
unspent, even after ``timeout_block_height``. The race we must win is against
the provider's refund: claim before the provider spends the output back to
itself.

This module builds, signs, and broadcasts a single-input claim transaction
that sweeps a lockup output to a wallet-controlled address.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from jmcore.bitcoin import (
    ParsedTransaction,
    TxInput,
    TxOutput,
    get_txid,
    parse_transaction,
    script_to_p2wsh_scriptpubkey,
    serialize_transaction,
)
from jmwallet.backends.base import BlockchainBackend

from taker.swap.keys import SwapKeyProvider, SwapWallet
from taker.swap.persistence import (
    SwapPersistence,
    SwapRecord,
    SwapRecordStatus,
    build_swap_persistence,
)

logger = logging.getLogger(__name__)

# A zero-arg async callable returning a fresh destination address.
AddressProvider = Callable[[], Awaitable[str]]

# Builds the claim witness stack for an already-parsed transaction. The wallet
# satisfies this (signing internally); the taker never sees the private key.
WitnessBuilder = Callable[[ParsedTransaction, int, bytes, int], list[bytes]]

# Conservative vsize estimate for a 1-input (P2WSH HTLC claim) 1-output
# (P2WPKH) sweep. Witness carries signature (~73) + preimage (32) +
# witness script (~110), so the real vsize sits around 165 vbytes; we round
# up for safety so the claim is not stuck below the relay floor.
CLAIM_TX_VSIZE = 175
# Never build a claim that pays less than this absolute fee, so the sweep
# always relays even when the supplied feerate is unrealistically low.
MIN_CLAIM_FEE_SATS = 250
# Refuse to broadcast if the recovered amount would be below this; a smaller
# output is dust and not worth a transaction.
DUST_THRESHOLD_SATS = 546


class RecoveryOutcome(StrEnum):
    """Result of attempting to recover a single swap record."""

    CLAIMED = "claimed"  # We broadcast a claim tx sweeping the lockup
    ALREADY_SPENT = "already_spent"  # Lockup already gone (CoinJoin or refund)
    NO_LOCKUP = "no_lockup"  # No lockup output found on-chain yet
    DUST = "dust"  # Lockup too small to sweep profitably
    SKIPPED = "skipped"  # Record already terminal
    # Our CoinJoin still in flight; claiming now would double-spend it.
    PENDING_COINJOIN = "pending_coinjoin"


@dataclass
class RecoveryResult:
    """Outcome of recovering one swap record."""

    swap_id: str
    outcome: RecoveryOutcome
    txid: str | None = None
    value: int = 0
    fee: int = 0
    detail: str = ""


def build_claim_transaction(
    *,
    lockup_txid: str,
    lockup_vout: int,
    lockup_value: int,
    witness_script: bytes,
    destination_address: str,
    fee_sats: int,
    witness_builder: WitnessBuilder,
) -> tuple[str, int]:
    """Build and sign a claim transaction sweeping a lockup output.

    The claim path takes the HTLC ``OP_IF`` branch, which is not subject to
    the CLTV in the refund branch, so ``locktime`` is 0 and the input sequence
    is final.

    Args:
        lockup_txid: Lockup transaction id (RPC/big-endian hex).
        lockup_vout: Lockup output index.
        lockup_value: Lockup output value in sats.
        witness_script: The HTLC witness (redeem) script bytes.
        destination_address: Where to sweep the funds.
        fee_sats: Absolute fee to pay.
        witness_builder: Callable that signs and assembles the claim witness
            for the parsed transaction (provided by the wallet, so no private
            key is handled here).

    Returns:
        ``(signed_tx_hex, output_value)``.

    Raises:
        ValueError: If the resulting output would be dust or negative.
    """
    output_value = lockup_value - fee_sats
    if output_value < DUST_THRESHOLD_SATS:
        raise ValueError(
            f"Lockup value {lockup_value} minus fee {fee_sats} = {output_value} "
            f"is below dust threshold {DUST_THRESHOLD_SATS}"
        )

    scriptpubkey = script_to_p2wsh_scriptpubkey(witness_script).hex()
    tx_input = TxInput.from_hex(
        lockup_txid,
        lockup_vout,
        sequence=0xFFFFFFFF,
        value=lockup_value,
        scriptpubkey=scriptpubkey,
    )
    tx_output = TxOutput.from_address(destination_address, output_value)

    # Serialize without witness, then reparse so the signer sees a clean
    # ParsedTransaction for BIP-143 sighash construction.
    unsigned_hex = serialize_transaction(2, [tx_input], [tx_output], 0).hex()
    parsed: ParsedTransaction = parse_transaction(unsigned_hex)

    witness = witness_builder(parsed, 0, witness_script, lockup_value)

    signed_hex = serialize_transaction(2, [tx_input], [tx_output], 0, witnesses=[witness]).hex()
    return signed_hex, output_value


class SwapRecovery:
    """Scan, claim, and reconcile persisted swap lockup outputs."""

    def __init__(
        self,
        backend: BlockchainBackend,
        persistence: SwapPersistence,
        key_provider: SwapKeyProvider,
        network: str = "mainnet",
    ) -> None:
        self.backend = backend
        self.persistence = persistence
        self.key_provider = key_provider
        self.network = network

    @staticmethod
    def _fee_for(feerate_sat_vb: float) -> int:
        return max(MIN_CLAIM_FEE_SATS, math.ceil(CLAIM_TX_VSIZE * feerate_sat_vb))

    async def _coinjoin_in_flight(self, coinjoin_txid: str) -> bool:
        """Heuristic: is our CoinJoin still able to spend the lockup?

        Returns True when claiming would risk double-spending an in-flight
        CoinJoin, and we should therefore wait instead of sweeping.

        - If the backend can see the CoinJoin (mempool or confirmed), it is
          alive and we must not claim.
        - If it is absent and the backend has mempool visibility, the CoinJoin
          has dropped and the lockup is safe to sweep.
        - If it is absent but the backend cannot see the mempool (light
          client), we cannot prove the CoinJoin is gone, so we conservatively
          treat it as in-flight and refuse to claim without ``force_claim``.
        """
        try:
            tx = await self.backend.get_transaction(coinjoin_txid)
        except Exception as exc:  # noqa: BLE001 - backend errors must not force a claim
            logger.warning(
                "Could not confirm CoinJoin %s status (%s); treating as in-flight",
                coinjoin_txid,
                exc,
            )
            return True
        if tx is not None:
            return True
        return not self.backend.has_mempool_access()

    async def _locate_lockup(self, record: SwapRecord) -> tuple[str, int, int] | None:
        """Return ``(txid, vout, value)`` of the unspent lockup, or None.

        Uses ``scan_external_address`` (confirmed UTXO set), so a return of
        None means the output is either unconfirmed, never created, or already
        spent.
        """
        expected_spk = script_to_p2wsh_scriptpubkey(record.witness_script).hex().lower()
        utxos = await self.backend.scan_external_address(record.lockup_address)
        for utxo in utxos:
            if utxo.scriptpubkey.lower() != expected_spk:
                continue
            # If we already know the outpoint, match it exactly; otherwise take
            # the first script match (provider locks a single HTLC output).
            if record.has_lockup and (utxo.txid != record.txid or utxo.vout != record.vout):
                continue
            return utxo.txid, utxo.vout, utxo.value
        return None

    async def recover_record(
        self,
        record: SwapRecord,
        *,
        destination_address: str,
        feerate_sat_vb: float = 2.0,
        broadcast: bool = True,
        force_claim: bool = False,
    ) -> RecoveryResult:
        """Attempt to recover a single swap record.

        Args:
            record: The persisted swap record to recover.
            destination_address: Wallet address to sweep recovered funds to.
            feerate_sat_vb: Fee rate for the claim transaction.
            broadcast: If False, build and persist intent but do not broadcast
                (used by tests and dry-run inspection).
            force_claim: Claim an unspent lockup even when our own CoinJoin may
                still be in flight. Off by default so an in-process watcher
                never races (and double-spends) the round it just broadcast.

        Returns:
            A :class:`RecoveryResult` describing the outcome. The record's
            status is updated and persisted as a side effect.
        """
        if record.is_terminal:
            return RecoveryResult(record.swap_id, RecoveryOutcome.SKIPPED, detail=record.status)

        located = await self._locate_lockup(record)
        if located is None:
            # The lockup is gone (or never confirmed). If we previously saw it,
            # it has been spent: by our CoinJoin (resolved) or by the provider's
            # refund. Reconcile terminal status so we stop retrying.
            if record.has_lockup:
                if record.coinjoin_txid:
                    record.status = SwapRecordStatus.RESOLVED
                    detail = "lockup spent by confirmed CoinJoin"
                else:
                    record.status = SwapRecordStatus.REFUNDED
                    detail = "lockup spent without a CoinJoin (provider refund)"
                self.persistence.save(record)
                return RecoveryResult(record.swap_id, RecoveryOutcome.ALREADY_SPENT, detail=detail)
            return RecoveryResult(
                record.swap_id,
                RecoveryOutcome.NO_LOCKUP,
                detail="no confirmed lockup output found",
            )

        txid, vout, value = located
        # Persist the freshly observed outpoint so a later run can reconcile it.
        record.txid, record.vout, record.value = txid, vout, value
        if record.status == SwapRecordStatus.PENDING_LOCKUP:
            record.status = SwapRecordStatus.LOCKED
        self.persistence.save(record)

        # Safety: the lockup is still unspent. If we broadcast a CoinJoin that
        # is meant to spend it, claiming now would double-spend our own input
        # and conflict the round. Only sweep once we are sure the CoinJoin will
        # not confirm (it has dropped from the mempool), unless forced.
        if record.coinjoin_txid and not force_claim:
            if await self._coinjoin_in_flight(record.coinjoin_txid):
                return RecoveryResult(
                    record.swap_id,
                    RecoveryOutcome.PENDING_COINJOIN,
                    value=value,
                    detail=f"CoinJoin {record.coinjoin_txid} still in flight; not claiming",
                )

        fee = self._fee_for(feerate_sat_vb)

        def witness_builder(
            parsed: ParsedTransaction, input_index: int, ws: bytes, val: int
        ) -> list[bytes]:
            return self.key_provider.build_swap_claim_witness(
                parsed, input_index, ws, val, record.swap_index
            )

        try:
            signed_hex, output_value = build_claim_transaction(
                lockup_txid=txid,
                lockup_vout=vout,
                lockup_value=value,
                witness_script=record.witness_script,
                destination_address=destination_address,
                fee_sats=fee,
                witness_builder=witness_builder,
            )
        except ValueError as exc:
            return RecoveryResult(
                record.swap_id, RecoveryOutcome.DUST, value=value, fee=fee, detail=str(exc)
            )

        claim_txid = get_txid(signed_hex)
        if not broadcast:
            return RecoveryResult(
                record.swap_id,
                RecoveryOutcome.CLAIMED,
                txid=claim_txid,
                value=output_value,
                fee=fee,
                detail="built but not broadcast (dry run)",
            )

        broadcast_txid = await self.backend.broadcast_transaction(signed_hex)
        record.recovery_txid = broadcast_txid
        record.status = SwapRecordStatus.RECOVERED
        self.persistence.save(record)
        logger.info(
            "Recovered swap %s: swept %d sats (fee %d) to %s in tx %s",
            record.swap_id,
            output_value,
            fee,
            destination_address,
            broadcast_txid,
        )
        return RecoveryResult(
            record.swap_id,
            RecoveryOutcome.CLAIMED,
            txid=broadcast_txid,
            value=output_value,
            fee=fee,
        )

    async def recover_all(
        self,
        *,
        address_provider: AddressProvider,
        feerate_sat_vb: float = 2.0,
        broadcast: bool = True,
        force_claim: bool = False,
    ) -> list[RecoveryResult]:
        """Recover every unresolved persisted swap record.

        Args:
            address_provider: Callable returning a fresh destination address
                for each claim (so recovered funds land on unused addresses).
            feerate_sat_vb: Fee rate for the claim transactions.
            broadcast: If False, do not broadcast (dry run).
            force_claim: Sweep unspent lockups even when the associated CoinJoin
                may still be in flight (see :meth:`recover_record`).
        """
        results: list[RecoveryResult] = []
        for record in self.persistence.list_unresolved():
            destination = await address_provider()
            result = await self.recover_record(
                record,
                destination_address=destination,
                feerate_sat_vb=feerate_sat_vb,
                broadcast=broadcast,
                force_claim=force_claim,
            )
            results.append(result)
        return results


def build_swap_recovery(
    wallet: SwapWallet,
    backend: BlockchainBackend,
    *,
    network: str = "mainnet",
    persistence: SwapPersistence | None = None,
) -> SwapRecovery | None:
    """Construct a :class:`SwapRecovery` for ``wallet``, or None if disabled.

    Returns None when the wallet cannot persist records (no ``data_dir``), so
    callers can treat recovery as a no-op for ephemeral wallets.
    """
    store = persistence if persistence is not None else build_swap_persistence(wallet)
    if store is None:
        return None
    return SwapRecovery(backend, store, wallet, network=network)


def wallet_address_provider(wallet: SwapWallet, *, mixdepth: int = 0) -> AddressProvider:
    """Adapt ``wallet.get_new_address`` into an :data:`AddressProvider`."""

    async def _provider() -> str:
        return wallet.get_new_address(mixdepth)

    return _provider
