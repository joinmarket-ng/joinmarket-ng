"""Maker and taker (coinjoin) endpoints."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from loguru import logger

from jmcore.settings import get_settings
from jmwalletd.deps import get_daemon_state, require_auth, require_wallet_match
from jmwalletd.errors import (
    ActionNotAllowed,
    BackendNotReady,
    InvalidRequestFormat,
    NoWalletFound,
    ServiceAlreadyStarted,
    ServiceNotStarted,
    TransactionFailed,
)
from jmwalletd.models import (
    DirectSendRequest,
    DirectSendResponse,
    DoCoinjoinRequest,
    GetScheduleResponse,
    RunScheduleRequest,
    StartMakerRequest,
    TxInfo,
    TxInput,
    TxOutput,
)
from jmwalletd.state import CoinjoinState, DaemonState

router = APIRouter()


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/taker/direct-send
# ---------------------------------------------------------------------------
@router.post("/wallet/{walletname}/taker/direct-send")
async def direct_send(
    walletname: str,
    body: DirectSendRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> DirectSendResponse:
    """Send bitcoin directly (without coinjoin)."""
    if state.taker_running:
        raise ActionNotAllowed("A coinjoin is already in progress.")

    ws = state.wallet_service

    try:
        from jmwalletd.send import do_direct_send

        tx_result = await do_direct_send(
            wallet_service=ws,
            mixdepth=body.mixdepth,
            amount_sats=body.amount_sats,
            destination=body.destination,
        )
    except ValueError as exc:
        raise InvalidRequestFormat(str(exc)) from exc
    except Exception as exc:
        logger.exception("Direct send failed")
        raise TransactionFailed(str(exc)) from exc

    # Build the txinfo response.
    txinfo = _build_txinfo(tx_result)

    # Notify WebSocket clients about the transaction.
    state.broadcast_ws({"txid": txinfo.txid, "txdetails": txinfo.model_dump()})

    return DirectSendResponse(txinfo=txinfo)


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/taker/coinjoin
# ---------------------------------------------------------------------------
@router.post("/wallet/{walletname}/taker/coinjoin", status_code=202)
async def do_coinjoin(
    walletname: str,
    body: DoCoinjoinRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> JSONResponse:
    """Initiate a coinjoin transaction (asynchronous)."""
    if state.coinjoin_state != CoinjoinState.NOT_RUNNING:
        raise ServiceAlreadyStarted("A coinjoin or maker service is already running.")
    if not state.wallet_mnemonic:
        raise NoWalletFound("Wallet mnemonic not available in daemon state.")

    try:
        from jmwalletd._backend import get_backend
        from taker.config import TakerConfig
        from taker.taker import Taker

        state.activate_coinjoin_state(CoinjoinState.TAKER_RUNNING)

        async def _run_coinjoin() -> None:
            taker: Any | None = None
            try:
                backend = await get_backend(state.data_dir, force_new=True)
                jm_settings = get_settings()
                config = TakerConfig(
                    mnemonic=state.wallet_mnemonic,
                    mixdepth=body.mixdepth,
                    amount=body.amount_sats,
                    destination_address=body.destination,  # type: ignore[arg-type]
                    counterparty_count=body.counterparties,
                    network=jm_settings.network_config.network,
                    directory_servers=jm_settings.get_directory_servers(),
                    socks_host=jm_settings.tor.socks_host,
                    socks_port=jm_settings.tor.socks_port,
                    stream_isolation=jm_settings.tor.stream_isolation,
                )
                taker = Taker(
                    wallet=ws,
                    backend=backend,
                    config=config,
                )
                state._taker_ref = taker
                await taker.start()
                await taker.do_coinjoin(
                    amount=body.amount_sats,
                    destination=body.destination,
                    mixdepth=body.mixdepth,
                    counterparty_count=body.counterparties,
                )
            except Exception:
                logger.exception("Coinjoin failed")
            finally:
                # Always tear down the taker so its directory-client and
                # background tasks do not leak. Keep the shared wallet open
                # for any subsequent operation on the daemon.
                if taker is not None:
                    try:
                        await taker.stop(close_wallet=False)
                    except Exception:
                        logger.exception("Taker teardown failed")
                state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
                state._taker_ref = None

        ws = state.wallet_service
        state._taker_task = asyncio.create_task(_run_coinjoin())

    except ImportError:
        state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
        raise BackendNotReady("Taker module not available.") from None
    except Exception as exc:
        state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
        raise BackendNotReady(str(exc)) from exc

    return JSONResponse(content={}, status_code=202)


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/taker/schedule
# ---------------------------------------------------------------------------
@router.post("/wallet/{walletname}/taker/schedule", status_code=202)
async def run_schedule(
    walletname: str,
    body: RunScheduleRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> JSONResponse:
    """Start a tumbler schedule (asynchronous).

    Note: The Taker class does not yet have a dedicated tumbler mode.
    This endpoint creates a Taker that can run a multi-step schedule
    via ``taker.run_schedule()`` when that API is available.
    """
    if state.coinjoin_state != CoinjoinState.NOT_RUNNING:
        raise ServiceAlreadyStarted("A coinjoin or maker service is already running.")
    if not state.wallet_mnemonic:
        raise NoWalletFound("Wallet mnemonic not available in daemon state.")

    try:
        from jmwalletd._backend import get_backend
        from taker.config import TakerConfig
        from taker.taker import Taker

        state.activate_coinjoin_state(CoinjoinState.TAKER_RUNNING)

        async def _run_tumbler() -> None:
            taker: Any | None = None
            try:
                ws = state.wallet_service
                backend = await get_backend(state.data_dir, force_new=True)
                jm_settings = get_settings()
                config = TakerConfig(
                    mnemonic=state.wallet_mnemonic,
                    network=jm_settings.network_config.network,
                    directory_servers=jm_settings.get_directory_servers(),
                    socks_host=jm_settings.tor.socks_host,
                    socks_port=jm_settings.tor.socks_port,
                    stream_isolation=jm_settings.tor.stream_isolation,
                )
                taker = Taker(
                    wallet=ws,
                    backend=backend,
                    config=config,
                )
                state._taker_ref = taker
                # run_schedule is the closest to tumbler in the Taker API.
                if hasattr(taker, "run_schedule") and body.destination_addresses:
                    schedule = cast(list[str | int | float], body.destination_addresses)
                    state.current_schedule = [schedule]
                await taker.start()
                if hasattr(taker, "run_schedule") and body.destination_addresses:
                    from taker.config import Schedule, ScheduleEntry

                    entries = [
                        ScheduleEntry(
                            mixdepth=0,
                            amount=0,
                            counterparty_count=1,
                            destination=destination,
                        )
                        for destination in body.destination_addresses
                    ]
                    schedule_obj = Schedule(entries=entries)
                    await taker.run_schedule(schedule_obj)
            except Exception:
                logger.exception("Tumbler failed")
            finally:
                # Always tear down the taker so its directory-client and
                # background tasks do not leak. Keep the shared wallet open.
                if taker is not None:
                    try:
                        await taker.stop(close_wallet=False)
                    except Exception:
                        logger.exception("Taker teardown failed")
                state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
                state.current_schedule = None
                state._taker_ref = None

        state._taker_task = asyncio.create_task(_run_tumbler())

        # Return initial schedule placeholder.
        return JSONResponse(content={"schedule": []}, status_code=202)

    except ImportError:
        state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
        raise BackendNotReady("Taker module not available.") from None
    except Exception as exc:
        state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
        raise BackendNotReady(str(exc)) from exc


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/{walletname}/taker/schedule
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/taker/schedule")
async def get_schedule(
    walletname: str,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> GetScheduleResponse:
    """Get the current tumbler schedule."""
    if state.current_schedule is None:
        raise NoWalletFound("No schedule is currently running.")

    return GetScheduleResponse(schedule=state.current_schedule)


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/{walletname}/taker/stop
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/taker/stop", status_code=202)
async def stop_coinjoin(
    walletname: str,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> JSONResponse:
    """Stop a running coinjoin/tumbler."""
    if not state.taker_running:
        raise ServiceNotStarted()

    # Signal the taker to stop if a reference is held.
    if state._taker_ref is not None:
        try:
            await state._taker_ref.stop()
        except Exception:
            logger.exception("Error stopping taker")

    if state._taker_task is not None and not state._taker_task.done():
        state._taker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await state._taker_task

    state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
    state.current_schedule = None
    state._taker_ref = None
    state._taker_task = None
    return JSONResponse(content={}, status_code=202)


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/maker/start
# ---------------------------------------------------------------------------
@router.post("/wallet/{walletname}/maker/start", status_code=202)
async def start_maker(
    walletname: str,
    body: StartMakerRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> JSONResponse:
    """Start the yield generator (maker) service."""
    if state.coinjoin_state != CoinjoinState.NOT_RUNNING:
        raise ServiceAlreadyStarted("A coinjoin or maker service is already running.")
    if not state.wallet_mnemonic:
        raise NoWalletFound("Wallet mnemonic not available in daemon state.")

    # Parse maker parameters.
    try:
        txfee = int(body.txfee)
        cjfee_a = int(body.cjfee_a)
        cjfee_r = str(body.cjfee_r)
        minsize = int(body.minsize)
    except ValueError as exc:
        raise InvalidRequestFormat(f"Invalid maker parameter: {exc}") from exc

    try:
        from jmwalletd._backend import get_backend
        from maker.bot import MakerBot
        from maker.config import MakerConfig

        state.activate_coinjoin_state(CoinjoinState.MAKER_RUNNING)

        async def _run_maker() -> None:
            try:
                ws = state.wallet_service
                backend = await get_backend(state.data_dir, force_new=True)
                jm_settings = get_settings()
                config = MakerConfig(
                    mnemonic=state.wallet_mnemonic,
                    offer_type=body.ordertype,  # type: ignore[arg-type]
                    min_size=minsize,
                    cj_fee_relative=cjfee_r,
                    cj_fee_absolute=cjfee_a,
                    tx_fee_contribution=txfee,
                    network=jm_settings.network_config.network,
                    directory_servers=jm_settings.get_directory_servers(),
                    socks_host=jm_settings.tor.socks_host,
                    socks_port=jm_settings.tor.socks_port,
                    stream_isolation=jm_settings.tor.stream_isolation,
                )
                maker = MakerBot(
                    wallet=ws,
                    backend=backend,
                    config=config,
                )
                state._maker_ref = maker
                state.nickname = maker.nick

                await maker.start()
                # NOTE: maker.start() blocks until shutdown (it awaits
                # asyncio.gather on listen tasks).  The session endpoint
                # now reads current_offers directly from the maker ref,
                # so there is nothing to do here.
            except Exception:
                logger.exception("Maker failed")
            finally:
                state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
                state.offer_list = None
                state.nickname = None
                state._maker_ref = None
                state._maker_task = None

        state._maker_task = asyncio.create_task(_run_maker())
    except ImportError:
        state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
        raise BackendNotReady("Maker module not available.") from None
    except Exception as exc:
        state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
        raise BackendNotReady(str(exc)) from exc

    return JSONResponse(content={}, status_code=202)


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/{walletname}/maker/stop
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/maker/stop", status_code=202)
async def stop_maker(
    walletname: str,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> JSONResponse:
    """Stop the yield generator (maker) service."""
    if not state.maker_running:
        raise ServiceNotStarted()

    # Signal the maker to stop if a reference is held.
    if state._maker_ref is not None:
        try:
            await state._maker_ref.stop()
        except Exception:
            logger.exception("Error stopping maker")

    if state._maker_task is not None and not state._maker_task.done():
        state._maker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await state._maker_task

    state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
    state.offer_list = None
    state.nickname = None
    state._maker_ref = None
    state._maker_task = None
    return JSONResponse(content={}, status_code=202)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_txinfo(tx_result: Any) -> TxInfo:
    """Convert a transaction result from jmwallet into a TxInfo response model."""
    inputs = [
        TxInput(
            outpoint=inp.get("outpoint", ""),
            scriptSig=inp.get("scriptSig", ""),
            nSequence=inp.get("nSequence", 4294967295),
            witness=inp.get("witness", ""),
        )
        for inp in getattr(tx_result, "inputs", [])
    ]

    outputs = [
        TxOutput(
            value_sats=out.get("value_sats", 0),
            scriptPubKey=out.get("scriptPubKey", ""),
            address=out.get("address", ""),
        )
        for out in getattr(tx_result, "outputs", [])
    ]

    # DirectSendResult uses ``tx_hex``; fall back to ``hex`` for compat.
    tx_hex = getattr(tx_result, "tx_hex", None) or getattr(tx_result, "hex", "")

    return TxInfo(
        hex=tx_hex,
        inputs=inputs,
        outputs=outputs,
        txid=getattr(tx_result, "txid", ""),
        nLockTime=getattr(tx_result, "locktime", 0),
        nVersion=getattr(tx_result, "version", 2),
    )
