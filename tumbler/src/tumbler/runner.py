"""
Tumble runner.

Executes a :class:`~tumbler.plan.Plan` against a live wallet plus blockchain
and directory backends. The runner owns the Taker / MakerBot lifecycle for the
duration of each phase and is responsible for:

* transitioning phase and plan statuses through
  :data:`~tumbler.plan.PhaseStatus`,
* persisting the plan to YAML on every state change (so that a restart of
  jmwalletd can resume),
* guaranteeing that the Taker and MakerBot are torn down in ``finally`` even
  on cancellation, without closing the shared wallet,
* honouring cooperative cancellation via :meth:`TumbleRunner.request_stop`.

The runner is intentionally agnostic of the transport in front of it
(CLI, jmwalletd router, tests). All external dependencies are injected via
the :class:`RunnerContext` dataclass so that unit tests can substitute fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from jmcore.settings import get_settings
from jmwallet.wallet.service import WalletService
from loguru import logger

from tumbler.persistence import save_plan
from tumbler.plan import (
    BondlessTakerBurstPhase,
    MakerSessionPhase,
    Phase,
    PhaseStatus,
    Plan,
    PlanStatus,
    TakerCoinjoinPhase,
)


class _BackendFactory(Protocol):
    """Awaitable that returns a fresh blockchain backend."""

    async def __call__(self) -> Any: ...  # pragma: no cover - protocol


class _TakerFactory(Protocol):
    """Builds a Taker for a single taker-coinjoin phase."""

    async def __call__(self, phase: TakerCoinjoinPhase) -> Any: ...  # pragma: no cover


class _MakerFactory(Protocol):
    """Builds a MakerBot for a maker-session phase."""

    async def __call__(self, phase: MakerSessionPhase) -> Any: ...  # pragma: no cover


@dataclass
class RunnerContext:
    """
    Collected dependencies for a :class:`TumbleRunner`.

    ``taker_factory`` / ``maker_factory`` return *started-capable* objects;
    the runner calls ``.start()`` on the taker itself so the wallet-sync
    cost is counted against the phase, not the factory.
    """

    wallet_service: WalletService
    wallet_name: str
    data_dir: Path | None
    taker_factory: _TakerFactory
    maker_factory: _MakerFactory | None = None
    # Callback invoked right after ``save_plan`` so the daemon can push
    # websocket updates to the UI. Not called for cancellation-only saves.
    on_state_changed: Callable[[Plan], None] | None = None
    # Override for tests: replacement for ``asyncio.sleep``.
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    # Confirmation gate between phases. After a taker-CJ phase broadcasts
    # a txid, the next phase is delayed until that txid reaches
    # ``min_confirmations_between_phases`` confirmations. This mirrors the
    # reference tumbler's ``restart_waiter`` and prevents the next phase
    # from hitting the Taker's ``taker_utxo_age`` wall when it tries to
    # spend an unconfirmed output. Set to ``0`` to disable.
    min_confirmations_between_phases: int = 5
    # Optional callback returning the current confirmation count of a txid,
    # or ``None`` if the transaction is not (yet) visible to the backend.
    # Required when ``min_confirmations_between_phases > 0``.
    get_confirmations: Callable[[str], Awaitable[int | None]] | None = None
    # Polling interval for ``get_confirmations``. Tests override this to
    # keep runs fast.
    confirmation_poll_interval: float = 5.0


class TumbleRunner:
    """Runs a :class:`Plan` through to completion, updating it in place."""

    def __init__(self, plan: Plan, ctx: RunnerContext):
        self.plan = plan
        self.ctx = ctx
        self._stop_requested = asyncio.Event()
        self._active_taker: Any | None = None
        self._active_maker: Any | None = None

    # -------------------------------------------------------------- lifecycle

    async def run(self) -> Plan:
        """Execute every phase in order. Idempotent for already-finished plans."""
        if self.plan.status == PlanStatus.COMPLETED:
            return self.plan
        self.plan.status = PlanStatus.RUNNING
        self.plan.error = None
        self._persist()

        try:
            while True:
                phase = self.plan.current()
                if phase is None:
                    break
                if self._stop_requested.is_set():
                    phase.status = PhaseStatus.CANCELLED
                    self.plan.status = PlanStatus.CANCELLED
                    self._persist()
                    return self.plan
                await self._run_one_phase(phase)
                if phase.status == PhaseStatus.FAILED:
                    self.plan.status = PlanStatus.FAILED
                    self.plan.error = phase.error
                    self._persist()
                    return self.plan
                if phase.status == PhaseStatus.CANCELLED:
                    self.plan.status = PlanStatus.CANCELLED
                    self._persist()
                    return self.plan
                # Before advancing, wait for the phase's output(s) to reach
                # ``taker_utxo_age`` confirmations so the next phase does not
                # try to spend an unconfirmed UTXO. This mirrors the reference
                # tumbler's ``restart_waiter``.
                next_index = self.plan.current_phase + 1
                has_next = next_index < len(self.plan.phases)
                if has_next:
                    try:
                        await self._wait_for_phase_confirmations(phase)
                    except _StopRequestedError:
                        self.plan.status = PlanStatus.CANCELLED
                        self._persist()
                        return self.plan
                self.plan.current_phase += 1
                self._persist()
                if phase.wait_seconds > 0 and self.plan.current() is not None:
                    try:
                        await self._wait_interruptibly(phase.wait_seconds)
                    except _StopRequestedError:
                        self.plan.status = PlanStatus.CANCELLED
                        self._persist()
                        return self.plan
        finally:
            await self._teardown_active()

        self.plan.status = PlanStatus.COMPLETED
        self._persist()
        return self.plan

    def request_stop(self) -> None:
        """Signal the runner to stop between phases (and interrupt the active one)."""
        self._stop_requested.set()

    async def stop_and_wait(self, task: asyncio.Task[Plan]) -> Plan:
        """Request a stop and await the underlying task's completion."""
        self.request_stop()
        await self._teardown_active()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            return await task
        return self.plan

    # ------------------------------------------------------------ phase impl

    async def _run_one_phase(self, phase: Phase) -> None:
        phase.status = PhaseStatus.RUNNING
        phase.started_at = datetime.now(UTC)
        phase.error = None
        self._persist()
        try:
            if isinstance(phase, TakerCoinjoinPhase):
                await self._run_taker_phase(phase)
            elif isinstance(phase, MakerSessionPhase):
                await self._run_maker_phase(phase)
            elif isinstance(phase, BondlessTakerBurstPhase):
                await self._run_bondless_burst_phase(phase)
            else:  # pragma: no cover - exhaustiveness
                raise RuntimeError(f"unknown phase kind: {phase!r}")
        except _StopRequestedError:
            phase.status = PhaseStatus.CANCELLED
        except asyncio.CancelledError:
            phase.status = PhaseStatus.CANCELLED
            raise
        except TakerPhaseError as exc:
            # Known, already-explained failure (e.g. not enough makers).
            # The taker itself has logged the cause; no traceback needed.
            logger.error("tumbler phase {} failed: {}", phase.index, exc)
            phase.status = PhaseStatus.FAILED
            phase.error = str(exc)
        except Exception as exc:
            logger.exception("tumbler phase %s failed", phase.index)
            phase.status = PhaseStatus.FAILED
            phase.error = f"{type(exc).__name__}: {exc}"
        else:
            phase.status = PhaseStatus.COMPLETED
        finally:
            phase.finished_at = datetime.now(UTC)

    # -------------------------------------- taker (single CJ) ---------------

    async def _run_taker_phase(self, phase: TakerCoinjoinPhase) -> None:
        taker = await self.ctx.taker_factory(phase)
        self._active_taker = taker
        try:
            await taker.start()
            destination = await self._resolve_destination(phase)
            amount = await self._resolve_amount(phase)
            # ``Taker.do_coinjoin(amount, destination, mixdepth, counterparty_count)``
            # returns the broadcast txid as a str, or None on failure.
            # Note: ``rounding`` is intentionally dropped: the reference
            # taker ignores it and the kwarg is not part of ``do_coinjoin``'s
            # signature. The field is retained on the phase for future
            # plumbing but must not be forwarded here.
            result = await taker.do_coinjoin(
                amount=amount,
                destination=destination,
                mixdepth=phase.mixdepth,
                counterparty_count=phase.counterparty_count,
            )
            if result is None:
                raise TakerPhaseError(
                    "CoinJoin did not broadcast: taker returned no txid "
                    "(see taker logs above for the cause, e.g. not enough compatible makers)"
                )
            if isinstance(result, str):
                phase.txid = result
            else:
                # Defensive: some fakes return an object with a .txid attribute.
                txid = getattr(result, "txid", None)
                if isinstance(txid, str):
                    phase.txid = txid
        finally:
            await self._teardown_taker()

    async def _resolve_amount(self, phase: TakerCoinjoinPhase) -> int:
        """Resolve phase amount in satoshis.

        ``TakerCoinjoinPhase`` exposes either an absolute ``amount`` (sats) or
        a ``amount_fraction`` of the mixdepth balance. The reference
        ``run_schedule`` resolves fractions by reading the current mixdepth
        balance immediately before the CJ; we mirror that so the phase is
        always dispatched to ``Taker.do_coinjoin`` as an int.
        """
        if phase.amount is not None:
            return phase.amount
        fraction = phase.amount_fraction
        assert fraction is not None  # guaranteed by TakerCoinjoinPhase validator
        if fraction == 0.0:
            # Sweep sentinel: Taker.do_coinjoin treats amount=0 as sweep.
            return 0
        balance = await self.ctx.wallet_service.get_balance(phase.mixdepth)
        return int(int(balance) * fraction)

    async def _resolve_destination(self, phase: TakerCoinjoinPhase) -> str:
        """Resolve the 'INTERNAL' sentinel to a concrete next-mixdepth address."""
        if phase.destination != "INTERNAL":
            return phase.destination
        next_mixdepth = (phase.mixdepth + 1) % 5
        return self._get_internal_address(next_mixdepth)

    def _get_internal_address(self, mixdepth: int) -> str:
        """Return the next unused internal (change-chain) address for a mixdepth.

        ``WalletService`` does not expose a one-shot helper for internal addresses,
        so we follow the same pattern as :class:`taker.taker.Taker` for its
        destination / change picks: advance the change-chain index counter and
        request that index on the change chain.
        """
        wallet = self.ctx.wallet_service
        index = wallet.get_next_address_index(mixdepth, 1)
        return str(wallet.get_change_address(mixdepth, index))

    async def _teardown_taker(self) -> None:
        taker = self._active_taker
        self._active_taker = None
        if taker is None:
            return
        try:
            # Prefer ``stop(close_wallet=False)`` when the taker supports it,
            # so we leave the shared wallet open for the next phase.
            stop = taker.stop
            try:
                await stop(close_wallet=False)
            except TypeError:
                # Back-compat with Takers that do not yet expose the kwarg;
                # fall back to manual teardown that mirrors ``stop`` minus
                # the ``wallet.close()`` call.
                await self._manual_taker_teardown(taker)
        except Exception:  # pragma: no cover - teardown best effort
            logger.exception("taker teardown error")

    async def _manual_taker_teardown(self, taker: Any) -> None:
        taker.running = False
        tasks = list(getattr(taker, "_background_tasks", []))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if hasattr(taker, "_background_tasks"):
            taker._background_tasks.clear()
        directory_client = getattr(taker, "directory_client", None)
        if directory_client is not None:
            with contextlib.suppress(Exception):
                await directory_client.close_all()

    # -------------------------------------- maker session -------------------

    async def _run_maker_phase(self, phase: MakerSessionPhase) -> None:
        if self.ctx.maker_factory is None:
            raise RuntimeError(
                "plan contains a MakerSessionPhase but no maker_factory was provided"
            )
        maker = await self.ctx.maker_factory(phase)
        self._active_maker = maker
        start_task = asyncio.create_task(maker.start())
        try:
            deadline = _deadline(phase)
            last_served = phase.cj_served
            last_progress = _now()
            while True:
                if maker_finished(maker, phase, start_task):
                    break
                if self._stop_requested.is_set():
                    raise _StopRequestedError()
                if deadline is not None and _now() >= deadline:
                    break
                if phase.cj_served != last_served:
                    last_served = phase.cj_served
                    last_progress = _now()
                if (
                    phase.idle_timeout_seconds is not None
                    and (_now() - last_progress).total_seconds() >= phase.idle_timeout_seconds
                ):
                    logger.info(
                        "maker phase %s: idle timeout (%.1fs) reached with %d cj served",
                        phase.index,
                        phase.idle_timeout_seconds,
                        phase.cj_served,
                    )
                    break
                await self.ctx.sleep(1.0)
        finally:
            await self._teardown_maker(start_task)
        # Surface start-task failures (e.g., Tor unavailable) as phase failure.
        if start_task.done() and not start_task.cancelled():
            exc = start_task.exception()
            if exc is not None:
                raise exc

    async def _teardown_maker(self, start_task: asyncio.Task[None]) -> None:
        maker = self._active_maker
        self._active_maker = None
        if maker is None:
            return
        try:
            await maker.stop()
        except Exception:
            logger.exception("maker teardown error")
        if not start_task.done():
            start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await start_task

    # -------------------------------------- bondless burst ------------------

    async def _run_bondless_burst_phase(self, phase: BondlessTakerBurstPhase) -> None:
        """
        Run a burst of small taker CoinJoins within a single mixdepth.

        Each sub-CJ is executed via a fresh short-lived taker built from the
        maker factory's twin, the :class:`RunnerContext.taker_factory`. A
        sub-CJ is equivalent to a :class:`TakerCoinjoinPhase` with
        ``destination='INTERNAL'`` whose target is the same mixdepth (a fresh
        internal change-chain address is derived for every sub-CJ).
        """
        while phase.completed_count < phase.cj_count:
            if self._stop_requested.is_set():
                raise _StopRequestedError()
            sub = TakerCoinjoinPhase(
                index=phase.index,
                mixdepth=phase.mixdepth,
                amount_fraction=phase.amount_fraction,
                counterparty_count=phase.counterparty_count,
                destination="INTERNAL_SAME",
                rounding=phase.rounding,
            )
            # Special sentinel: same-mixdepth internal address (no cross-mix).
            taker = await self.ctx.taker_factory(sub)
            self._active_taker = taker
            try:
                await taker.start()
                destination = self._get_internal_address(phase.mixdepth)
                # Resolve the fractional amount off the current mixdepth
                # balance for each sub-CJ, matching _run_taker_phase.
                balance = await self.ctx.wallet_service.get_balance(phase.mixdepth)
                amount = int(int(balance) * phase.amount_fraction)
                result = await taker.do_coinjoin(
                    amount=amount,
                    destination=destination,
                    mixdepth=phase.mixdepth,
                    counterparty_count=phase.counterparty_count,
                )
                if result is None:
                    raise RuntimeError("taker.do_coinjoin returned no txid")
                if isinstance(result, str):
                    phase.txids.append(result)
                else:
                    txid = getattr(result, "txid", None)
                    if isinstance(txid, str):
                        phase.txids.append(txid)
            finally:
                await self._teardown_taker()
            phase.completed_count += 1
            self._persist()

    # --------------------------------------------------- misc helpers -------

    async def _teardown_active(self) -> None:
        await self._teardown_taker()
        if self._active_maker is not None:
            # _run_maker_phase always wraps teardown itself, but a cancellation
            # raised between the factory and the try block would strand the
            # reference. Best-effort stop here.
            try:
                await self._active_maker.stop()
            except Exception:  # pragma: no cover
                logger.exception("stray maker teardown failed")
            self._active_maker = None

    async def _wait_interruptibly(self, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop_requested.wait(), timeout=seconds)
        except TimeoutError:
            return
        raise _StopRequestedError()

    async def _wait_for_phase_confirmations(self, phase: Phase) -> None:
        """Wait for the phase's broadcast txid(s) to reach the confirmation gate.

        Raises ``_StopRequestedError`` if a stop is signalled while polling.
        Silently returns if the gate is disabled, no callback is wired, or the
        phase produced no txids (e.g., a maker session).
        """
        min_conf = self.ctx.min_confirmations_between_phases
        if min_conf <= 0:
            return
        get_confirmations = self.ctx.get_confirmations
        if get_confirmations is None:
            return
        txids = _phase_txids(phase)
        if not txids:
            return
        for txid in txids:
            logger.info("tumbler: waiting for txid {} to reach {} confirmations", txid, min_conf)
            while True:
                if self._stop_requested.is_set():
                    raise _StopRequestedError()
                try:
                    confirmations = await get_confirmations(txid)
                except Exception:  # pragma: no cover - transient backend errors
                    logger.exception("get_confirmations({}) failed; retrying", txid)
                    confirmations = None
                if confirmations is not None and confirmations >= min_conf:
                    break
                try:
                    await asyncio.wait_for(
                        self._stop_requested.wait(),
                        timeout=self.ctx.confirmation_poll_interval,
                    )
                except TimeoutError:
                    continue
                raise _StopRequestedError()

    def _persist(self) -> None:
        save_plan(self.plan, self.ctx.data_dir)
        if self.ctx.on_state_changed is not None:
            try:
                self.ctx.on_state_changed(self.plan)
            except Exception:  # pragma: no cover
                logger.exception("on_state_changed callback failed")


# -- Small standalone helpers, exposed for test access -----------------------


class _StopRequestedError(Exception):
    """Internal sentinel raised when a cooperative stop is requested."""


class TakerPhaseError(Exception):
    """Raised when a taker-coinjoin phase fails for a known, already-logged reason.

    Runner catches this without emitting a traceback: the taker itself has
    already logged the specific cause (e.g. "not enough compatible makers").
    """


def _now() -> datetime:
    return datetime.now(UTC)


def _phase_txids(phase: Phase) -> list[str]:
    """Return txids produced by a phase, if any."""
    if isinstance(phase, TakerCoinjoinPhase):
        return [phase.txid] if phase.txid else []
    if isinstance(phase, BondlessTakerBurstPhase):
        return list(phase.txids)
    # Maker sessions do not produce a single broadcast txid we control.
    return []


def _deadline(phase: MakerSessionPhase) -> datetime | None:
    if phase.duration_seconds is None:
        return None
    return _now().replace() + _td_from_seconds(phase.duration_seconds)


def _td_from_seconds(seconds: float) -> Any:
    from datetime import timedelta

    return timedelta(seconds=seconds)


def maker_finished(maker: Any, phase: MakerSessionPhase, start_task: asyncio.Task[None]) -> bool:
    """True if the maker's start task is already done or the cj target is met."""
    if start_task.done():
        return True
    if phase.target_cj_count is not None:
        served = int(getattr(maker, "coinjoins_completed", 0))
        phase.cj_served = served
        if served >= phase.target_cj_count:
            return True
    return False


_ = get_settings  # noqa: F841 - kept for future contexts that need it
