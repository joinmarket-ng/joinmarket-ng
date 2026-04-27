"""Tumbler endpoints backed by :mod:`tumbler`.

The router exposes a small, stateless-ish HTTP surface over the persistent
YAML plan managed by :mod:`tumbler.persistence` and the in-memory runner
owned by :class:`jmwalletd.state.DaemonState`:

* ``POST /tumbler/plan``    -- build a new plan and persist it as ``PENDING``.
* ``POST /tumbler/start``   -- run the pending plan; the runner updates the
                               plan in place and the daemon keeps a handle on
                               the task.
* ``GET /tumbler/status``   -- fetch the current plan (in-memory if running,
                               otherwise from disk). Flags a ``stale`` plan
                               whose on-disk status is ``RUNNING`` but no
                               runner is live (crash recovery marker).
* ``POST /tumbler/stop``    -- cooperatively request the runner to stop; the
                               task transitions the plan to ``CANCELLED``
                               and tears down its taker / maker.
* ``DELETE /tumbler/plan``  -- remove a terminal or pending plan. Refuses
                               when the runner is live -- stop first.

See ``docs/technical/tumbler-redesign.md`` for the state matrix and
subset-sum rationale. The router itself is intentionally thin so all plan
semantics stay in :mod:`tumbler`.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from loguru import logger
from tumbler.builder import PlanBuilder, TumbleParameters
from tumbler.persistence import (
    PlanCorruptError,
    PlanNotFoundError,
    delete_plan,
    load_plan,
    plan_path,
    save_plan,
)
from tumbler.plan import (
    MakerSessionPhase,
    Plan,
    PlanStatus,
    TakerCoinjoinPhase,
)
from tumbler.runner import RunnerContext, TumbleRunner

from jmcore.settings import get_settings
from jmwalletd.deps import get_daemon_state, require_auth, require_wallet_match
from jmwalletd.errors import (
    ActionNotAllowed,
    BackendNotReady,
    InvalidRequestFormat,
    NoWalletFound,
    ServiceAlreadyStarted,
    ServiceNotStarted,
)
from jmwalletd.models import (
    TumblerPhaseResponse,
    TumblerPlanRequest,
    TumblerPlanResponse,
)
from jmwalletd.state import CoinjoinState, DaemonState

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_legacy_tumbler_parameters(raw: dict[str, object] | None) -> dict[str, object]:
    """Translate legacy JAM tumbler option names to ``TumbleParameters`` kwargs.

    The current JAM sweep page still sends the old ``tumbler_options`` field
    names in its testing payload. Accept them here so the HTTP surface remains
    compatible while the frontend catches up.
    """
    if not raw:
        return {}

    params = dict(raw)
    maker_count_range = params.pop("makercountrange", None)
    if isinstance(maker_count_range, list) and len(maker_count_range) >= 1:
        minimum = params.pop("minmakercount", None)
        params.setdefault(
            "maker_count_min", minimum if minimum is not None else maker_count_range[0]
        )
        spread = maker_count_range[1] if len(maker_count_range) > 1 else 0
        if minimum is None:
            minimum = maker_count_range[0]
        if isinstance(minimum, int) and isinstance(spread, int):
            params.setdefault("maker_count_max", minimum + spread)

    time_lambda = params.pop("timelambda", None)
    if time_lambda is not None:
        params.setdefault("time_lambda_seconds", time_lambda)

    # These legacy testing-only knobs do not have direct equivalents in the new
    # planner and should not be forwarded into ``TumbleParameters``.
    params.pop("addrcount", None)
    params.pop("mixdepthcount", None)
    params.pop("txcountparams", None)
    params.pop("stage1_timelambda_increase", None)
    params.pop("liquiditywait", None)
    params.pop("waittime", None)
    # Dropped in the redesign: the bondless-taker burst phase no longer
    # exists in the new plan model. Swallow the key if a legacy client
    # (e.g. an old JAM) still sends it so we don't break their flow.
    params.pop("include_bondless_bursts", None)

    return params


def _phase_to_response(phase: Any) -> TumblerPhaseResponse:
    """Flatten the discriminated-union phase into the wire response shape."""
    common: dict[str, Any] = {
        "kind": str(phase.kind),
        "index": phase.index,
        "status": str(phase.status),
        "wait_seconds": phase.wait_seconds,
        "started_at": phase.started_at.isoformat() if phase.started_at else None,
        "finished_at": phase.finished_at.isoformat() if phase.finished_at else None,
        "error": phase.error,
        "attempt_count": getattr(phase, "attempt_count", None),
    }
    if isinstance(phase, TakerCoinjoinPhase):
        common.update(
            mixdepth=phase.mixdepth,
            amount=phase.amount,
            amount_fraction=phase.amount_fraction,
            counterparty_count=phase.counterparty_count,
            destination=phase.destination,
            txid=phase.txid,
        )
    elif isinstance(phase, MakerSessionPhase):
        common.update(
            duration_seconds=phase.duration_seconds,
            target_cj_count=phase.target_cj_count,
            idle_timeout_seconds=phase.idle_timeout_seconds,
            cj_served=phase.cj_served,
        )
    return TumblerPhaseResponse(**common)


def _plan_to_response(plan: Plan, *, stale: bool = False) -> TumblerPlanResponse:
    return TumblerPlanResponse(
        plan_id=plan.plan_id,
        wallet_name=plan.wallet_name,
        status=str(plan.status),
        destinations=list(plan.destinations),
        current_phase=plan.current_phase,
        phases=[_phase_to_response(p) for p in plan.phases],
        created_at=plan.created_at.isoformat(),
        updated_at=plan.updated_at.isoformat(),
        error=plan.error,
        stale=stale,
    )


async def _mixdepth_balances(wallet_service: Any, num_mixdepths: int = 5) -> dict[int, int]:
    """Return confirmed balance per mixdepth in satoshis.

    :class:`WalletService.get_balance` is ``async`` and returns an ``int`` of
    sats that already excludes frozen UTXOs.
    """
    balances: dict[int, int] = {}
    for mixdepth in range(num_mixdepths):
        try:
            balances[mixdepth] = int(await wallet_service.get_balance(mixdepth))
        except Exception:
            logger.exception("failed to read balance for mixdepth {}", mixdepth)
            balances[mixdepth] = 0
    return balances


def _reconcile_on_request(state: DaemonState, wallet_name: str) -> Plan | None:
    """Bring on-disk state in line with in-memory reality before answering.

    If the daemon has no live runner for ``wallet_name`` but the plan on disk
    is ``RUNNING``, transition it to ``FAILED``. This covers restarts where
    the startup reconciliation already ran but a fresh wallet-specific plan
    was somehow left dangling. Returns the possibly-updated plan or ``None``
    if no plan exists for the wallet.
    """
    try:
        plan = load_plan(wallet_name, state.data_dir)
    except PlanNotFoundError:
        return None
    except PlanCorruptError as exc:
        raise ActionNotAllowed(f"Tumbler plan is corrupt: {exc}") from exc

    runner_alive = (
        state.tumble_runner is not None
        and state.tumble_task is not None
        and not state.tumble_task.done()
        and state.tumble_plan_wallet == wallet_name
    )
    if plan.status == PlanStatus.RUNNING and not runner_alive:
        plan.status = PlanStatus.FAILED
        plan.error = plan.error or "daemon restarted mid-run"
        save_plan(plan, state.data_dir)
    return plan


def _runner_alive_for(state: DaemonState, wallet_name: str) -> bool:
    return (
        state.tumble_runner is not None
        and state.tumble_task is not None
        and not state.tumble_task.done()
        and state.tumble_plan_wallet == wallet_name
    )


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/tumbler/plan
# ---------------------------------------------------------------------------
@router.post("/wallet/{walletname}/tumbler/plan", status_code=201, operation_id="tumblerplan")
async def create_plan(
    walletname: str,
    body: TumblerPlanRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> TumblerPlanResponse:
    """Build and persist a fresh tumble plan for the active wallet.

    An already-running plan for the wallet is always protected: callers must
    ``POST /tumbler/stop`` first. A plan in any other state (pending,
    completed, failed, cancelled) is overwritten unconditionally -- passing
    ``force=true`` is only required for a pending plan, to make the
    destructive intent explicit.
    """
    if _runner_alive_for(state, state.wallet_name):
        raise ServiceAlreadyStarted("A tumbler is already running; stop it first.")

    existing = _reconcile_on_request(state, state.wallet_name)
    if existing is not None and existing.status == PlanStatus.PENDING and not body.force:
        raise ActionNotAllowed("A pending plan already exists; pass force=true to overwrite it.")

    ws = state.wallet_service
    if ws is None:
        raise NoWalletFound()

    balances = await _mixdepth_balances(ws, num_mixdepths=getattr(ws, "mixdepth_count", 5))
    if not any(v > 0 for v in balances.values()):
        raise ActionNotAllowed("Wallet has no confirmed coins to tumble.")

    extra = _normalize_legacy_tumbler_parameters(body.parameters)
    try:
        params = TumbleParameters(
            destinations=list(body.destinations),
            mixdepth_balances=balances,
            **extra,  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise InvalidRequestFormat(f"Invalid tumbler parameters: {exc}") from exc

    try:
        plan = PlanBuilder(wallet_name=state.wallet_name, params=params).build()
    except ValueError as exc:
        raise InvalidRequestFormat(str(exc)) from exc

    save_plan(plan, state.data_dir)
    state.broadcast_ws({"tumbler": {"event": "plan_created", "wallet_name": plan.wallet_name}})
    logger.info(
        "tumbler plan created: wallet={} phases={} destinations={}",
        plan.wallet_name,
        len(plan.phases),
        len(plan.destinations),
    )
    return _plan_to_response(plan)


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/{walletname}/tumbler/status
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/tumbler/status", operation_id="tumblerstatus")
async def get_status(
    walletname: str,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> TumblerPlanResponse:
    """Return the live plan if the runner is active, otherwise the on-disk plan.

    When the on-disk plan is ``RUNNING`` but no runner is live, the response's
    ``stale`` flag is set so the UI can prompt the user to acknowledge the
    failure and delete the plan.
    """
    if _runner_alive_for(state, state.wallet_name):
        # ``tumble_runner`` is the authoritative state while running.
        return _plan_to_response(state.tumble_runner.plan)

    try:
        plan = load_plan(state.wallet_name, state.data_dir)
    except PlanNotFoundError as exc:
        raise NoWalletFound("No tumbler plan exists for this wallet.") from exc
    except PlanCorruptError as exc:
        raise ActionNotAllowed(f"Tumbler plan is corrupt: {exc}") from exc

    stale = plan.status == PlanStatus.RUNNING
    if stale:
        # Best-effort reconcile so successive calls do not keep flagging.
        plan.status = PlanStatus.FAILED
        plan.error = plan.error or "daemon restarted mid-run"
        save_plan(plan, state.data_dir)
    return _plan_to_response(plan, stale=stale)


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/tumbler/start
# ---------------------------------------------------------------------------
@router.post("/wallet/{walletname}/tumbler/start", status_code=202, operation_id="tumblerstart")
async def start_plan(
    walletname: str,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> JSONResponse:
    """Load the pending plan and run it in the background."""
    if state.coinjoin_state != CoinjoinState.NOT_RUNNING:
        raise ServiceAlreadyStarted("A coinjoin or maker service is already running.")
    if not state.wallet_mnemonic:
        raise NoWalletFound("Wallet mnemonic not available in daemon state.")

    plan = _reconcile_on_request(state, state.wallet_name)
    if plan is None:
        raise NoWalletFound("No tumbler plan exists for this wallet; create one first.")
    if plan.status in (PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED):
        raise ActionNotAllowed(f"Plan is in terminal state {plan.status.value}; create a new plan.")
    if plan.status == PlanStatus.RUNNING:
        raise ServiceAlreadyStarted("Plan is already running.")

    ws = state.wallet_service
    if ws is None:
        raise NoWalletFound()

    # Factories are closed over the current wallet/settings at start time.
    jm_settings = get_settings()

    from jmwalletd._backend import get_backend
    from maker.bot import MakerBot
    from maker.config import MakerConfig
    from taker.config import TakerConfig
    from taker.taker import Taker

    async def _taker_factory(phase: Any) -> Any:
        backend = await get_backend(
            state.data_dir,
            force_new=True,
            wallet_service=ws,
        )
        config = TakerConfig(
            mnemonic=state.wallet_mnemonic,
            mixdepth=getattr(phase, "mixdepth", 0),
            amount=getattr(phase, "amount", 0) or 0,
            # ``destination`` is resolved inside the runner (INTERNAL sentinel),
            # so we pass a throwaway here; the Taker reads it only when
            # ``do_coinjoin`` is not given one.
            destination_address="",  # type: ignore[arg-type]
            counterparty_count=getattr(phase, "counterparty_count", 1),
            network=jm_settings.network_config.network,
            directory_servers=jm_settings.get_directory_servers(),
            socks_host=jm_settings.tor.socks_host,
            socks_port=jm_settings.tor.socks_port,
            stream_isolation=jm_settings.tor.stream_isolation,
        )
        return Taker(wallet=ws, backend=backend, config=config)

    async def _maker_factory(_phase: Any) -> Any:
        backend = await get_backend(
            state.data_dir,
            force_new=True,
            wallet_service=ws,
        )
        config = MakerConfig(
            mnemonic=state.wallet_mnemonic,
            network=jm_settings.network_config.network,
            directory_servers=jm_settings.get_directory_servers(),
            socks_host=jm_settings.tor.socks_host,
            socks_port=jm_settings.tor.socks_port,
            stream_isolation=jm_settings.tor.stream_isolation,
        )
        # Tumbler maker sessions must run as 0-fee sw0absoffer with no
        # fidelity bond. See ``tumbler.maker_policy`` for the rationale.
        from tumbler.maker_policy import apply_tumbler_maker_policy

        apply_tumbler_maker_policy(config)
        return MakerBot(wallet=ws, backend=backend, config=config)

    def _on_state_changed(p: Plan) -> None:
        state.broadcast_ws(
            {
                "tumbler": {
                    "event": "plan_updated",
                    "wallet_name": p.wallet_name,
                    "status": str(p.status),
                    "current_phase": p.current_phase,
                    "total_phases": len(p.phases),
                }
            }
        )

    async def _get_confirmations(txid: str) -> int | None:
        """Return confirmation count for ``txid`` via the shared backend.

        Two-stage lookup (see :func:`tumbler.confirmations.resolve_confirmations`):

        1. ``backend.get_transaction(txid)`` (full nodes / mempool.space).
        2. Watched-address fallback via the CoinJoin history file
           (works with neutrino, which cannot fetch arbitrary txids by
           id but *can* match watched addresses via BIP158).
        """
        from tumbler.confirmations import resolve_confirmations

        try:
            backend = await get_backend(state.data_dir, wallet_service=ws)
        except Exception:
            logger.exception("get_confirmations(%s) backend resolution failed", txid)
            return None
        return await resolve_confirmations(txid, backend, state.data_dir)

    ctx = RunnerContext(
        wallet_service=ws,
        wallet_name=state.wallet_name,
        data_dir=state.data_dir,
        taker_factory=_taker_factory,
        maker_factory=_maker_factory,
        on_state_changed=_on_state_changed,
        get_confirmations=_get_confirmations,
    )
    runner = TumbleRunner(plan, ctx)

    state.tumble_runner = runner
    state.tumble_plan_wallet = state.wallet_name
    state.activate_coinjoin_state(CoinjoinState.TUMBLER_RUNNING)

    async def _run() -> None:
        try:
            await runner.run()
        except Exception:
            logger.exception("tumbler runner crashed")
        finally:
            state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
            state.tumble_runner = None
            state.tumble_plan_wallet = None
            state.tumble_task = None

    state.tumble_task = asyncio.create_task(_run())
    return JSONResponse(content={}, status_code=202)


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/tumbler/stop
# ---------------------------------------------------------------------------
@router.post("/wallet/{walletname}/tumbler/stop", status_code=202, operation_id="tumblerstop")
async def stop_plan(
    walletname: str,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> JSONResponse:
    """Cooperatively stop the running plan; transition it to ``CANCELLED``."""
    if not _runner_alive_for(state, state.wallet_name):
        raise ServiceNotStarted("No tumbler is running for this wallet.")

    runner = state.tumble_runner
    task = state.tumble_task
    assert runner is not None and task is not None  # noqa: S101  -- invariant
    try:
        await runner.stop_and_wait(task)
    except asyncio.CancelledError:  # pragma: no cover - cooperative path
        pass
    except Exception:
        logger.exception("error while stopping tumbler runner")
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    return JSONResponse(content={}, status_code=202)


# ---------------------------------------------------------------------------
# DELETE /api/v1/wallet/{walletname}/tumbler/plan
# ---------------------------------------------------------------------------
@router.delete(
    "/wallet/{walletname}/tumbler/plan", status_code=204, operation_id="tumblerplandelete"
)
async def delete_plan_endpoint(
    walletname: str,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> JSONResponse:
    """Remove a non-running plan from disk."""
    if _runner_alive_for(state, state.wallet_name):
        raise ActionNotAllowed("A tumbler is running; stop it before deleting the plan.")

    # Reconcile first so a stale ``RUNNING`` plan on disk is flipped to FAILED
    # before deletion; keeps the observable event stream consistent.
    _reconcile_on_request(state, state.wallet_name)

    removed = delete_plan(state.wallet_name, state.data_dir)
    if not removed:
        raise NoWalletFound("No tumbler plan exists for this wallet.")
    state.broadcast_ws({"tumbler": {"event": "plan_deleted", "wallet_name": state.wallet_name}})
    return JSONResponse(content=None, status_code=204)


# The ``plan_path`` import is kept public here so tests that want to assert
# the schedules directory layout can do so without pulling tumbler directly.
_ = plan_path
# ``BackendNotReady`` is kept imported because factories that import taker/
# maker modules at call time may fail with it; the import site lives inside
# start_plan's closures so mypy sees the name as used.
_ = BackendNotReady
