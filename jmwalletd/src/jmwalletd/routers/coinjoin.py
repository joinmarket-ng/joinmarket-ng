"""Maker and taker (coinjoin) endpoints."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from loguru import logger

from jmcore.paths import remove_nick_state, write_nick_state
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
    StartMakerRequest,
    TxInfo,
    TxInput,
    TxOutput,
)
from jmwalletd.state import CoinjoinState, DaemonState

router = APIRouter()


def build_coinjoin_taker_config(
    *,
    body: Any,
    mnemonic: Any,
    jm_settings: Any,
    taker_config_cls: Any,
) -> Any:
    """Build a ``TakerConfig`` for a one-shot ``do_coinjoin`` request.

    Mirrors ``taker.cli.build_taker_config`` so a CoinJoin started through the
    daemon honors the same ``[taker]`` policy settings (passed via config or
    ``TAKER__*`` env) as the CLI taker. Previously this endpoint set only the
    network/Tor/directory fields, so every other taker policy silently fell
    back to ``TakerConfig`` defaults.

    In particular ``minimum_makers`` is capped against the requested
    ``counterparties`` (as ``build_taker_config`` and
    ``build_tumbler_taker_config`` do): a request for fewer makers than the
    policy ``minimum_makers`` (default 4) would otherwise select a valid
    N-maker CoinJoin and then reject it with ``Not enough makers selected: N``.
    """
    from taker.config import BroadcastPolicy, MaxCjFee

    counterparties = int(body.counterparties)
    effective_minimum_makers = min(jm_settings.taker.minimum_makers, counterparties)

    # Resolve fee settings: config fee_rate takes precedence over a block
    # target, falling back to the wallet's default block target.
    effective_fee_rate: float | None = None
    effective_block_target: int | None = None
    if jm_settings.taker.fee_rate is not None:
        effective_fee_rate = jm_settings.taker.fee_rate
    else:
        effective_block_target = (
            jm_settings.taker.fee_block_target
            if jm_settings.taker.fee_block_target is not None
            else jm_settings.wallet.default_fee_block_target
        )

    try:
        broadcast_policy = BroadcastPolicy(jm_settings.taker.tx_broadcast)
    except ValueError:
        broadcast_policy = BroadcastPolicy.MULTIPLE_PEERS

    return taker_config_cls(
        mnemonic=mnemonic,
        mixdepth=body.mixdepth,
        amount=body.amount_sats,
        destination_address=body.destination,
        counterparty_count=counterparties,
        network=jm_settings.network_config.network,
        directory_servers=jm_settings.get_directory_servers(),
        socks_host=jm_settings.tor.socks_host,
        socks_port=jm_settings.tor.socks_port,
        stream_isolation=jm_settings.tor.stream_isolation,
        connection_timeout=jm_settings.tor.connection_timeout,
        mixdepth_count=jm_settings.wallet.mixdepth_count,
        gap_limit=jm_settings.wallet.gap_limit,
        scan_range=jm_settings.wallet.scan_range,
        dust_threshold=jm_settings.wallet.dust_threshold,
        max_cj_fee=MaxCjFee(
            abs_fee=jm_settings.taker.max_cj_fee_abs,
            rel_fee=jm_settings.taker.max_cj_fee_rel,
        ),
        tx_fee_factor=jm_settings.taker.tx_fee_factor,
        fee_rate=effective_fee_rate,
        fee_block_target=effective_block_target,
        max_fee_rate_sat_vb=jm_settings.wallet.max_fee_rate_sat_vb,
        bondless_makers_allowance=jm_settings.taker.bondless_makers_allowance,
        bond_value_exponent=jm_settings.taker.bond_value_exponent,
        bondless_makers_allowance_require_zero_fee=jm_settings.taker.bondless_require_zero_fee,
        maker_timeout_sec=jm_settings.taker.maker_timeout_sec,
        order_wait_time=jm_settings.taker.order_wait_time,
        tx_broadcast=broadcast_policy,
        broadcast_peer_count=jm_settings.taker.broadcast_peer_count,
        minimum_makers=effective_minimum_makers,
        rescan_interval_sec=jm_settings.taker.rescan_interval_sec,
        pending_tx_abandon_hours=jm_settings.taker.pending_tx_abandon_hours,
        taker_utxo_age=jm_settings.taker.taker_utxo_age,
        taker_utxo_retries=jm_settings.taker.taker_utxo_retries,
        taker_utxo_amtpercent=jm_settings.taker.taker_utxo_amtpercent,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/taker/direct-send
# ---------------------------------------------------------------------------
@router.post("/wallet/{walletname}/taker/direct-send", operation_id="directsend")
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
            max_fee_rate_sat_vb=get_settings().wallet.max_fee_rate_sat_vb,
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
@router.post("/wallet/{walletname}/taker/coinjoin", status_code=202, operation_id="docoinjoin")
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
                backend = await get_backend(
                    state.data_dir,
                    force_new=True,
                    mnemonic=state.wallet_mnemonic,
                    network=get_settings().network_config.network.value,
                )
                jm_settings = get_settings()
                config = build_coinjoin_taker_config(
                    body=body,
                    mnemonic=state.wallet_mnemonic,
                    jm_settings=jm_settings,
                    taker_config_cls=TakerConfig,
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
# GET /api/v1/wallet/{walletname}/taker/stop
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/taker/stop", status_code=202, operation_id="stopcoinjoin")
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
@router.post("/wallet/{walletname}/maker/start", status_code=202, operation_id="startmaker")
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
                backend = await get_backend(
                    state.data_dir,
                    force_new=True,
                    wallet_service=ws,
                )
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
                write_nick_state(state.data_dir, "maker", maker.nick)

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
                remove_nick_state(state.data_dir, "maker")

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
@router.get("/wallet/{walletname}/maker/stop", status_code=202, operation_id="stopmaker")
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
