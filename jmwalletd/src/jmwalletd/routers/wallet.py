"""Wallet lifecycle and info endpoints.

Covers: getinfo, session, wallet/all, wallet/create, wallet/recover,
wallet/{name}/unlock, wallet/{name}/lock, token refresh.
"""

from __future__ import annotations

import asyncio
from typing import Any

import jwt as pyjwt
from fastapi import APIRouter, Depends, Request
from loguru import logger

from jmwalletd.deps import (
    get_daemon_state,
    get_optional_token,
    require_auth,
    require_auth_allow_expired,
    require_wallet_match,
)
from jmwalletd.errors import (
    BackendNotReady,
    InvalidCredentials,
    InvalidRequestFormat,
    InvalidToken,
    LockExists,
    WalletAlreadyExists,
    WalletAlreadyUnlocked,
    WalletNotFound,
)
from jmwalletd.legacy_schedule import plan_to_legacy_schedule
from jmwalletd.models import (
    CreateWalletRequest,
    CreateWalletResponse,
    GetInfoResponse,
    ListWalletsResponse,
    LockWalletResponse,
    RecoverWalletRequest,
    SessionResponse,
    TokenRequest,
    TokenResponse,
    UnlockWalletRequest,
    UnlockWalletResponse,
)
from jmwalletd.state import CoinjoinState, DaemonState
from jmwalletd.wallet_ops import (
    create_wallet,
    open_wallet_with_mnemonic,
    recover_wallet,
)


def _get_offer_list_from_maker(
    state: DaemonState,
) -> list[dict[str, str | int | float]] | None:
    """Read the current offer list directly from the running maker bot.

    ``state.offer_list`` is populated in ``_run_maker`` **after**
    ``maker.start()`` returns, but that call blocks for the lifetime of
    the maker (it awaits ``asyncio.gather(*listen_tasks)``).  As a result,
    the assignment was unreachable and the frontend never received offers.

    This helper reads ``current_offers`` from the live maker reference,
    which is available as soon as offers are created during ``start()``.
    """
    maker = state._maker_ref
    if maker is None:
        return None

    offers = getattr(maker, "current_offers", None)
    if not offers:
        return None

    return [
        {
            "oid": getattr(o, "oid", 0),
            "ordertype": str(getattr(o, "ordertype", "")),
            "minsize": getattr(o, "minsize", 0),
            "maxsize": getattr(o, "maxsize", 0),
            "txfee": getattr(o, "txfee", 0),
            "cjfee": str(getattr(o, "cjfee", "")),
        }
        for o in offers
    ]


def _get_running_tumble_schedule(
    state: DaemonState,
) -> list[list[str | int | float]] | None:
    """Render the live tumbler plan as a legacy schedule, or ``None``.

    Mirrors the reference implementation, where ``/session`` only carries a
    schedule while a tumble is in progress (single-shot taker coinjoins do
    not expose one). The plan is read straight from the live runner so
    completion flags and txids are always current (issue #553).
    """
    if state.coinjoin_state != CoinjoinState.TUMBLER_RUNNING:
        return None
    runner = state.tumble_runner
    if runner is None:
        return None
    return plan_to_legacy_schedule(runner.plan)


router = APIRouter()


async def _require_tx_monitor_ready(state: DaemonState, *, lock_on_failure: bool = False) -> None:
    """Reject lifecycle completion while transaction history is unbaselined."""
    if not await state.wait_tx_monitor_ready():
        if lock_on_failure:
            await state.lock_wallet()
        raise BackendNotReady("Transaction monitor baseline is not ready.")


async def _background_wallet_sync(state: DaemonState, walletname: str) -> None:
    """Run wallet sync in the background after unlock.

    This keeps unlock responsive while the neutrino backend performs longer
    rescans.  Session status exposes `rescanning=true` during this task.
    """
    ws = state.wallet_service
    if ws is None:
        return

    state.rescanning = True
    state.rescan_progress = 0.0

    try:
        await ws.sync()
        logger.info("Background wallet sync completed for {}", walletname)
    except Exception:
        logger.exception("Background wallet sync failed for {}", walletname)
        raise
    finally:
        state.rescanning = False
        state.rescan_progress = 0.0


# ---------------------------------------------------------------------------
# GET /api/v1/getinfo
# ---------------------------------------------------------------------------
@router.get("/getinfo", operation_id="version")
async def get_info() -> GetInfoResponse:
    """Return backend information."""
    from jmcore.version import __version__

    return GetInfoResponse(version=__version__, backend="joinmarket-ng")


# ---------------------------------------------------------------------------
# GET /api/v1/session
# ---------------------------------------------------------------------------
@router.get("/session", operation_id="session")
async def get_session(
    request: Request,
    state: DaemonState = Depends(get_daemon_state),
) -> SessionResponse:
    """Heartbeat / status endpoint.

    If an Authorization header is present, it is validated. An invalid
    token returns 401. A missing token is fine (unauthenticated access).
    """
    token = get_optional_token(request)
    token_valid = False

    if token is not None:
        try:
            state.token_authority.verify_access(token)
            token_valid = True
        except pyjwt.InvalidTokenError as exc:
            raise InvalidToken(str(exc)) from exc

    # Bitcoin Core is the source of truth for rescan state: the in-memory
    # flag can go stale while Core keeps scanning server-side (issue #551).
    rescanning, _progress = await state.live_rescan_status()

    resp = SessionResponse(
        session=state.wallet_loaded,
        maker_running=state.maker_running,
        coinjoin_in_process=state.taker_running,
        wallet_name=state.wallet_name if state.wallet_loaded else "",
        rescanning=rescanning,
    )

    # Populate extra fields only when authenticated.
    if state.wallet_loaded and token_valid:
        resp.schedule = _get_running_tumble_schedule(state)
        resp.nickname = state.nickname

        # Read offer_list directly from the running maker bot so that the
        # frontend receives it as soon as offers are created (the old path
        # through state.offer_list was unreachable because maker.start()
        # blocks until shutdown).
        offer_list = _get_offer_list_from_maker(state)
        resp.offer_list = offer_list if offer_list else state.offer_list

        try:
            backend = state.wallet_service.backend
            resp.block_height = await backend.get_block_height()
        except Exception:
            resp.block_height = None

        # Expose the underlying bitcoind descriptor wallet name for clients
        # (e.g. test setup / debugging tools) that need to query Bitcoin Core
        # directly. Only present when the active backend is a descriptor
        # wallet — other backends (Neutrino) leave this unset.
        try:
            backend = state.wallet_service.backend
            wallet_name_attr = getattr(backend, "wallet_name", None)
            if isinstance(wallet_name_attr, str) and wallet_name_attr:
                resp.descriptor_wallet_name = wallet_name_attr
        except Exception:
            resp.descriptor_wallet_name = None

    return resp


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/all
# ---------------------------------------------------------------------------
@router.get("/wallet/all", operation_id="listwallets")
async def list_wallets(
    state: DaemonState = Depends(get_daemon_state),
) -> ListWalletsResponse:
    """List available wallet files."""
    return ListWalletsResponse(wallets=state.list_wallets())


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/create
# ---------------------------------------------------------------------------
@router.post("/wallet/create", status_code=201, operation_id="createwallet")
async def wallet_create(
    body: CreateWalletRequest,
    state: DaemonState = Depends(get_daemon_state),
) -> CreateWalletResponse:
    """Create a new wallet."""
    if state.wallet_loaded:
        raise WalletAlreadyUnlocked()

    wallet_path = state.wallets_dir / body.walletname
    if wallet_path.exists():
        raise WalletAlreadyExists()

    try:
        wallet_service, seedphrase = await create_wallet(
            wallet_path=wallet_path,
            password=body.password,
            wallet_type=body.wallettype,
            data_dir=state.data_dir,
        )
    except FileExistsError as exc:
        raise WalletAlreadyExists() from exc
    except OSError as exc:
        raise LockExists(str(exc)) from exc
    except ValueError as exc:
        raise InvalidRequestFormat(str(exc)) from exc

    state.wallet_service = wallet_service
    state.wallet_mnemonic = seedphrase
    state.wallet_name = body.walletname
    state.wallet_password = body.password

    # A generated wallet has no pre-existing history to suppress, so monitoring
    # can become live without risking a readiness error after seed generation.
    state.start_tx_monitor(baseline_existing=False)
    await _require_tx_monitor_ready(state)

    tokens = state.token_authority.issue(body.walletname)

    return CreateWalletResponse(
        walletname=body.walletname,
        seedphrase=seedphrase,
        token=tokens.token,
        token_type=tokens.token_type,
        expires_in=tokens.expires_in,
        scope=tokens.scope,
        refresh_token=tokens.refresh_token,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/recover
# ---------------------------------------------------------------------------
@router.post("/wallet/recover", status_code=201, operation_id="recoverwallet")
async def wallet_recover(
    body: RecoverWalletRequest,
    state: DaemonState = Depends(get_daemon_state),
) -> CreateWalletResponse:
    """Recover a wallet from a seed phrase."""
    if state.wallet_loaded:
        raise WalletAlreadyUnlocked()

    wallet_path = state.wallets_dir / body.walletname
    if wallet_path.exists():
        raise WalletAlreadyExists()

    try:
        wallet_service = await recover_wallet(
            wallet_path=wallet_path,
            password=body.password,
            wallet_type=body.wallettype,
            seedphrase=body.seedphrase,
            data_dir=state.data_dir,
        )
    except FileExistsError as exc:
        raise WalletAlreadyExists() from exc
    except OSError as exc:
        raise LockExists(str(exc)) from exc
    except ValueError as exc:
        raise InvalidRequestFormat(str(exc)) from exc

    state.wallet_service = wallet_service
    state.wallet_mnemonic = body.seedphrase
    state.wallet_name = body.walletname
    state.wallet_password = body.password

    state.start_tx_monitor()
    try:
        await _require_tx_monitor_ready(state, lock_on_failure=True)
    except BackendNotReady:
        # Recovery is a create operation. Remove the encrypted file so the
        # caller can retry the same request after a transient backend outage.
        try:
            wallet_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to roll back wallet recovery for {}", body.walletname)
        raise

    tokens = state.token_authority.issue(body.walletname)

    return CreateWalletResponse(
        walletname=body.walletname,
        seedphrase=body.seedphrase,
        token=tokens.token,
        token_type=tokens.token_type,
        expires_in=tokens.expires_in,
        scope=tokens.scope,
        refresh_token=tokens.refresh_token,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/wallet/{walletname}/unlock
# ---------------------------------------------------------------------------
@router.post("/wallet/{walletname}/unlock", operation_id="unlockwallet")
async def wallet_unlock(
    walletname: str,
    body: UnlockWalletRequest,
    state: DaemonState = Depends(get_daemon_state),
) -> UnlockWalletResponse:
    """Unlock (decrypt) a wallet."""
    wallet_path = state.wallets_dir / walletname
    if not wallet_path.exists():
        raise WalletNotFound()

    # If the same wallet is already unlocked, just verify password and re-issue tokens.
    if state.wallet_loaded and state.wallet_name == walletname:
        if body.password != state.wallet_password:
            raise InvalidCredentials()
        await _require_tx_monitor_ready(state, lock_on_failure=True)
        # ``rotate_refresh=False``: a second unlock of the same wallet (another
        # tab/device, or a client re-running its unlock flow) must not
        # invalidate the refresh token already held by the first client.
        tokens = state.token_authority.issue(walletname, rotate_refresh=False)
        return UnlockWalletResponse(
            walletname=walletname,
            token=tokens.token,
            token_type=tokens.token_type,
            expires_in=tokens.expires_in,
            scope=tokens.scope,
            refresh_token=tokens.refresh_token,
        )

    # If a different wallet is loaded, lock it first.
    if state.wallet_loaded:
        await state.lock_wallet()

    try:
        wallet_service, seedphrase = await open_wallet_with_mnemonic(
            wallet_path=wallet_path,
            password=body.password,
            data_dir=state.data_dir,
            sync_on_open=False,
        )
    except OSError as exc:
        raise LockExists(str(exc)) from exc
    except ValueError as exc:
        raise InvalidCredentials(str(exc)) from exc

    state.wallet_service = wallet_service
    state.wallet_mnemonic = seedphrase
    state.wallet_name = walletname
    state.wallet_password = body.password

    # Kick off sync asynchronously so unlock returns immediately.
    if state._wallet_sync_task is not None and not state._wallet_sync_task.done():
        state._wallet_sync_task.cancel()
    state._wallet_sync_task = asyncio.create_task(_background_wallet_sync(state, walletname))

    # Start pushing WebSocket notifications for wallet transactions (issue #560).
    state.start_tx_monitor()
    await _require_tx_monitor_ready(state, lock_on_failure=True)

    tokens = state.token_authority.issue(walletname)

    return UnlockWalletResponse(
        walletname=walletname,
        token=tokens.token,
        token_type=tokens.token_type,
        expires_in=tokens.expires_in,
        scope=tokens.scope,
        refresh_token=tokens.refresh_token,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/wallet/{walletname}/lock
# ---------------------------------------------------------------------------
@router.get("/wallet/{walletname}/lock", operation_id="lockwallet")
async def wallet_lock(
    walletname: str,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> LockWalletResponse:
    """Lock the current wallet and stop all services."""
    already_locked = await state.lock_wallet()
    return LockWalletResponse(walletname=walletname, already_locked=already_locked)


# ---------------------------------------------------------------------------
# POST /api/v1/token
# ---------------------------------------------------------------------------
@router.post("/token", operation_id="token")
async def token_refresh(
    body: TokenRequest,
    _auth: dict[str, Any] = Depends(require_auth_allow_expired),
    state: DaemonState = Depends(get_daemon_state),
) -> TokenResponse:
    """Refresh the access/refresh token pair."""
    if body.grant_type != "refresh_token":
        raise InvalidRequestFormat("Unsupported grant_type. Must be 'refresh_token'.")

    try:
        state.token_authority.verify_refresh(body.refresh_token)
    except pyjwt.InvalidTokenError as exc:
        logger.debug("Refresh token verification failed: {}", exc)
        raise InvalidToken(f"Invalid refresh token: {exc}") from exc

    tokens = state.token_authority.issue(state.wallet_name)

    return TokenResponse(
        walletname=state.wallet_name,
        token=tokens.token,
        token_type=tokens.token_type,
        expires_in=tokens.expires_in,
        scope=tokens.scope,
        refresh_token=tokens.refresh_token,
    )
