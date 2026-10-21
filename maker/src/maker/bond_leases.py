"""Never advertise a fidelity bond this data directory leased to another maker.

A leased bond advertised by its owner during the lease would equivocate its
certificate: two makers would prove the same bond in one period. The market
store records every lease this data directory sold or stocked, and the guard
reads it before bond selection and in the background while making.

This is defense in depth against a seller in another process or an earlier
run, not the rental safety mechanism itself: a reading can become obsolete as
soon as it is taken, so the in-process rental supervisor must also never lease
the advertised bond.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from jmcore.credential_market import BondReference
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_store import blocked_bond_outpoints
from loguru import logger
from pydantic import ValidationError

from maker.fidelity import FidelityBondInfo

REFRESH_SECONDS = 30.0
# A reading older than this allows no bond: the background refresh has stalled.
STALE_SECONDS = 180.0

Outpoint = tuple[str, int]


class BondLeaseGuard:
    """Cached view of which owned bonds are leased, read from the market store.

    Unknown lease history (an unreadable store, or a wallet ledger still
    activating or needing recovery) and a stale reading allow no bond, so the
    maker keeps offering without a bond proof until the history is readable.
    """

    def __init__(
        self,
        store_path: Path,
        network: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store_path = store_path
        self.network = network
        self._clock = clock
        self._blocked: frozenset[Outpoint] | None = None
        self._read_at: float | None = None
        self._status = ""

    def _reference(self, bond: FidelityBondInfo) -> BondReference | None:
        """The market's identity of ``bond``; None when it cannot be one.

        Holds still match such a bond by outpoint, only stock matching needs it.
        """
        if bond.pubkey is None:
            return None
        try:
            return BondReference(
                network=self.network,  # type: ignore[arg-type]
                outpoint=ExternalPoDLEOutpoint(txid=bond.txid, vout=bond.vout),
                pubkey=bond.pubkey.hex(),
                locktime=bond.locktime,
            )
        except (ValidationError, ValueError):
            return None

    async def refresh(
        self, bonds: Iterable[FidelityBondInfo], height: int
    ) -> frozenset[Outpoint] | None:
        """Read the store; the blocked outpoints, or None when history is unknown."""
        references = [ref for bond in bonds if (ref := self._reference(bond)) is not None]
        try:
            blocked = await asyncio.to_thread(
                blocked_bond_outpoints, self.store_path, references, height
            )
        except Exception as exc:
            self._blocked = None
            self._read_at = None
            self._transition(
                "unknown",
                f"Fidelity bond lease history is unknown ({exc}); "
                "offers carry no bond proof until it can be read",
            )
            return None
        self._blocked = blocked
        self._read_at = self._clock()
        return blocked

    def allows(self, bond: FidelityBondInfo) -> bool:
        """Whether the last reading lets the owner advertise ``bond`` now."""
        if (
            self._blocked is None
            or self._read_at is None
            or self._clock() - self._read_at > STALE_SECONDS
        ):
            self._transition(
                "unknown",
                "Fidelity bond lease history is not current; offers carry no bond proof",
            )
            return False
        if (bond.txid, bond.vout) in self._blocked:
            self._transition(
                "leased",
                "The selected fidelity bond is leased to another maker; offers carry no bond proof",
            )
            return False
        self._transition("allowed", None)
        return True

    async def run(
        self,
        selected: Callable[[], FidelityBondInfo | None],
        height: Callable[[], int],
    ) -> None:
        """Refresh the reading of the selected bond until canceled."""
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            bond = selected()
            if bond is not None:
                await self.refresh([bond], height())

    def _transition(self, status: str, message: str | None) -> None:
        """Log each change of advertising status once."""
        if status == self._status:
            return
        previous, self._status = self._status, status
        if message is not None:
            logger.warning(message)
        elif previous:
            logger.info("Fidelity bond lease history is current; bond proofs resume")
