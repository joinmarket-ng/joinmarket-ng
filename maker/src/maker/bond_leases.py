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
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal

from jmcore.credential_market import BondReference, period_at_height
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_store import blocked_bond_outpoints
from jmcore.paths import get_market_store_path
from jmcore.secure_files import atomic_write_private
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from maker.fidelity import FidelityBondInfo

REFRESH_SECONDS = 30.0
# A reading older than this allows no bond: the background refresh has stalled.
STALE_SECONDS = 180.0
ADVERTISEMENT_FILE = "advertised-bonds.json"

Outpoint = tuple[str, int]

_OUTPOINT_KEY = re.compile(r"[0-9a-f]{64}:[0-9]{1,10}")
# Writers in this process (the guard and the rental supervisor) never interleave.
_RECORD_LOCK = threading.Lock()


class AdvertisementRecordError(Exception):
    """The advertised-bond record is unreadable or could not be written."""


class AdvertisementRecord(BaseModel):
    """The retarget periods through which this data directory may have advertised a bond.

    Every bond counts as advertised through ``since``, the period after the one
    the record was created in, which covers earlier runs and versions that kept
    no record (including a proof sent just after a boundary the maker had not
    seen yet). A listed bond also counts as advertised through its own period.
    Every field is required, so a truncated record is unreadable, never empty.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    since: int = Field(ge=0)
    advertised_through: dict[str, int]

    @field_validator("advertised_through")
    @classmethod
    def valid_entries(cls, value: dict[str, int]) -> dict[str, int]:
        for key, period in value.items():
            if not _OUTPOINT_KEY.fullmatch(key) or period < 0:
                raise ValueError("Invalid advertised bond entry")
        return value

    def through(self, outpoint: Outpoint) -> int:
        """The last period ``outpoint`` may have been advertised in."""
        return max(self.since, self.advertised_through.get(_key(outpoint), -1))

    def rentable(self, outpoint: Outpoint, period: int) -> bool:
        """Whether leasing ``outpoint`` for ``period`` cannot overlap an advertisement."""
        return self.through(outpoint) < period


def _key(outpoint: Outpoint) -> str:
    return f"{outpoint[0]}:{outpoint[1]}"


def advertisement_record_path(data_dir: Path) -> Path:
    """The advertised-bond record beside the market store of ``data_dir``."""
    return get_market_store_path(data_dir).with_name(ADVERTISEMENT_FILE)


def read_advertisements(path: Path) -> AdvertisementRecord | None:
    """The record, or None when absent; an unreadable record raises."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AdvertisementRecordError(f"could not read {path}") from exc
    try:
        return AdvertisementRecord.model_validate_json(raw)
    except ValidationError as exc:
        raise AdvertisementRecordError(
            f"{path} is corrupt; deleting it is safe and pauses bond rental for one period"
        ) from exc


def record_advertisement(
    path: Path, period: int, outpoint: Outpoint | None = None
) -> AdvertisementRecord:
    """Durably record ``outpoint`` as advertised through ``period + 1``.

    The next period is included because a proof can be sent just after a period
    boundary, before the maker has seen the new block. An absent record is
    created first, counting every bond as advertised through ``period + 1`` for
    the same reason.
    """
    with _RECORD_LOCK:
        current = read_advertisements(path)
        updated = current or AdvertisementRecord(version=1, since=period + 1, advertised_through={})
        if outpoint is not None and updated.through(outpoint) < period + 1:
            updated = AdvertisementRecord(
                version=1,
                since=updated.since,
                advertised_through={**updated.advertised_through, _key(outpoint): period + 1},
            )
        if updated != current:
            try:
                atomic_write_private(path, updated.model_dump_json().encode())
                _fsync_directory(path.parent)
            except OSError as exc:
                raise AdvertisementRecordError(f"could not write {path}") from exc
        return updated


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store_path = store_path
        self.record_path = store_path.with_name(ADVERTISEMENT_FILE)
        self.network = network
        self._clock = clock
        self._blocked: frozenset[Outpoint] | None = None
        self._read_at: float | None = None
        # The bond recorded as advertised through the period after the last reading.
        self._recorded: Outpoint | None = None
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
        references = [ref for bond in bonds if (ref := self._reference(bond)) is not None]
        self._recorded = None
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
        if advertising is not None and (outpoint := (advertising.txid, advertising.vout)) not in (
            blocked
        ):
            try:
                await asyncio.to_thread(
                    record_advertisement, self.record_path, period_at_height(height), outpoint
                )
            except Exception as exc:
                self._transition(
                    "unrecorded",
                    f"Could not record the advertised fidelity bond ({exc}); "
                    "offers carry no bond proof until it is recorded",
                )
            else:
                self._recorded = outpoint
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
