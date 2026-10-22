"""Never advertise a fidelity bond this data directory leased to another maker.

A leased bond advertised by its owner during the lease would equivocate its
certificate: two makers would prove the same bond in one period. The market
store records every lease this data directory sold or stocked, and the guard
reads it before bond selection and in the background while making.

This is defense in depth against a seller in another process or an earlier
run, not the rental safety mechanism itself: a reading can become obsolete as
soon as it is taken, so the in-process rental supervisor must also never lease
the advertised bond.

The opposite direction is the advertisement record: before a bond proof is
sent, the guard durably records the advertised bond through the next retarget
period, so a later run never rents out a bond this data directory advertised
in the same period.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from jmcore.bond_advertisements import (
    ADVERTISEMENT_FILE,
    BoundAdvertisementRecord,
    locked_advertisements,
)
from jmcore.bond_advertisements import (
    AdvertisementRecord as AdvertisementRecord,
)
from jmcore.bond_advertisements import (
    AdvertisementRecordError as AdvertisementRecordError,
)
from jmcore.bond_advertisements import (
    advertisement_record_path as advertisement_record_path,
)
from jmcore.bond_advertisements import (
    read_advertisements as read_advertisements,
)
from jmcore.bond_advertisements import (
    record_advertisement as record_advertisement,
)
from jmcore.credential_market import BondReference, period_at_height
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_store import MarketStore, blocked_bond_outpoints
from loguru import logger
from pydantic import ValidationError

from maker.fidelity import FidelityBondInfo

REFRESH_SECONDS = 30.0
# A reading older than this allows no bond: the background refresh has stalled.
STALE_SECONDS = 180.0
Outpoint = tuple[str, int]


def bond_references(bonds: Iterable[FidelityBondInfo], network: str) -> list[BondReference]:
    """The market's identities of ``bonds``, skipping any that cannot be one.

    Holds still match such a bond by outpoint, only stock matching needs it.
    """
    references: list[BondReference] = []
    for bond in bonds:
        if bond.pubkey is None:
            continue
        try:
            references.append(
                BondReference(
                    network=network,  # type: ignore[arg-type]
                    outpoint=ExternalPoDLEOutpoint(txid=bond.txid, vout=bond.vout),
                    pubkey=bond.pubkey.hex(),
                    locktime=bond.locktime,
                )
            )
        except (ValidationError, ValueError):
            continue
    return references


class BondLeaseGuard:
    """Cached view of which owned bonds are leased, read from the market store.

    Unknown lease history (an unreadable store, or a wallet ledger still
    activating or needing recovery), a stale reading and an advertised bond
    whose advertisement could not be recorded allow no bond, so the maker keeps
    offering without a bond proof until both are current.
    """

    def __init__(
        self,
        store_path: Path,
        network: str,
        *,
        wallet_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store_path = store_path
        self.record_path = store_path.with_name(ADVERTISEMENT_FILE)
        self.network = network
        self.wallet_id = wallet_id
        self._required_binding: tuple[str, str] | None = None
        self._clock = clock
        self._blocked: frozenset[Outpoint] | None = None
        self._read_at: float | None = None
        # The bond recorded as advertised through the period after the last reading.
        self._recorded: Outpoint | None = None
        self._recorded_through: int | None = None
        self._status = ""

    async def refresh(
        self,
        bonds: Iterable[FidelityBondInfo],
        height: int,
        *,
        advertising: FidelityBondInfo | None = None,
    ) -> frozenset[Outpoint] | None:
        """Read the store; the blocked outpoints, or None when history is unknown.

        ``advertising`` is the bond about to be advertised: unless it is
        blocked, it is recorded before :meth:`allows` can admit it.
        """
        references = bond_references(bonds, self.network)
        self._recorded = None
        self._recorded_through = None
        try:
            blocked, recorded = await asyncio.to_thread(
                self._read_and_record,
                references,
                height,
                advertising,
            )
        except AdvertisementRecordError as exc:
            # Preserve the lease snapshot contract, but never authorize a proof
            # when its advertisement could not be durably recorded.
            try:
                blocked = await asyncio.to_thread(
                    blocked_bond_outpoints,
                    self.store_path,
                    references,
                    height,
                )
            except Exception as read_error:
                self._unknown(read_error)
                return None
            self._blocked = blocked
            self._read_at = self._clock()
            self._transition(
                "unrecorded",
                f"Could not record fidelity bond advertisement ({exc}); "
                "offers carry no bond proof until history is available",
            )
            return blocked
        except Exception as exc:
            self._unknown(exc)
            return None
        self._blocked = blocked
        self._read_at = self._clock()
        self._recorded = recorded
        self._recorded_through = None if recorded is None else period_at_height(height) + 1
        return blocked

    def _read_and_record(
        self,
        references: list[BondReference],
        height: int,
        advertising: FidelityBondInfo | None,
    ) -> tuple[frozenset[Outpoint], Outpoint | None]:
        binding = MarketStore.inspect_advertisement_binding(self.store_path)
        if self._required_binding is not None and binding != self._required_binding:
            raise AdvertisementRecordError("required advertisement binding changed or disappeared")
        if binding is not None:
            self._required_binding = binding
            if binding != (self.wallet_id, self.network):
                raise AdvertisementRecordError("advertisement wallet/network binding mismatch")
        with locked_advertisements(
            self.record_path,
            wallet_id=self.wallet_id,
            network=self.network,
            require_bound=binding is not None,
        ) as history:
            if binding is None and isinstance(history.snapshot, BoundAdvertisementRecord):
                raise AdvertisementRecordError("bound advertisement ledger is incomplete")
            blocked = blocked_bond_outpoints(
                self.store_path, references, height, advertisement_binding=binding
            )
            recorded = None
            if (
                advertising is not None
                and (outpoint := (advertising.txid, advertising.vout)) not in blocked
            ):
                history.record(period_at_height(height), outpoint)
                recorded = outpoint
            return blocked, recorded

    def _unknown(self, exc: Exception) -> None:
        self._blocked = None
        self._read_at = None
        self._transition(
            "unknown",
            f"Fidelity bond lease history is unknown ({exc}); "
            "offers carry no bond proof until it can be read",
        )
        return None

    def allows(self, bond: FidelityBondInfo) -> bool:
        """Whether the last reading lets the owner advertise ``bond`` now."""
        if self._required_binding is not None:
            try:
                if (
                    MarketStore.inspect_advertisement_binding(
                        self.store_path,
                        require_ready=True,
                    )
                    != self._required_binding
                ):
                    raise AdvertisementRecordError("required advertisement binding changed")
                history = read_advertisements(
                    self.record_path,
                    wallet_id=self.wallet_id,
                    network=self.network,
                    require_bound=True,
                )
                if (
                    history is None
                    or self._recorded_through is None
                    or history.through((bond.txid, bond.vout)) < self._recorded_through
                ):
                    raise AdvertisementRecordError("required advertisement entry disappeared")
            except Exception as exc:
                self._unknown(exc)
                return False
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
        if (bond.txid, bond.vout) != self._recorded:
            self._transition(
                "unrecorded",
                "The selected fidelity bond is not recorded as advertised; "
                "offers carry no bond proof",
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
                await self.refresh([bond], height(), advertising=bond)

    def _transition(self, status: str, message: str | None) -> None:
        """Log each change of advertising status once."""
        if status == self._status:
            return
        previous, self._status = self._status, status
        if message is not None:
            logger.warning(message)
        elif previous:
            logger.info("Fidelity bond lease history is current; bond proofs resume")
