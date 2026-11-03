"""Requests authorized before a wallet lock began must not start wallet work.

FastAPI resolves ``require_auth`` before the handler body, with awaits in
between, and ``_lock_wallet`` resets the token keys before its first await but
only stops services that already exist. These tests run that reset between the
two, in a ``require_wallet_match`` override (resolved after ``require_auth``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tumbler.builder import PlanBuilder, TumbleParameters
from tumbler.persistence import load_plan, save_plan
from tumbler.plan import PlanStatus

from jmcore.bitcoin import TxInput, TxOutput, get_txid, serialize_transaction
from jmwallet.history import read_history
from jmwallet.wallet.spend import SignedDirectTx
from jmwalletd.deps import get_daemon_state, get_optional_token, require_wallet_match
from jmwalletd.errors import InvalidToken
from jmwalletd.models import DoCoinjoinRequest, StartMakerRequest
from jmwalletd.state import CoinjoinState, DaemonState

WALLET = "test_wallet.jmdat"


@pytest.fixture
def lock_begins_after_auth(app_with_wallet: TestClient) -> Iterator[TestClient]:
    app = cast(FastAPI, app_with_wallet.app)
    app.dependency_overrides[require_wallet_match] = get_daemon_state().token_authority.reset
    yield app_with_wallet
    app.dependency_overrides.clear()


def _post(client: TestClient, token: str, path: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(
        f"/api/v1/wallet/{WALLET}/{path}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "maker/start",
            {
                "txfee": "1000",
                "cjfee_a": "500",
                "cjfee_r": "0.002",
                "ordertype": "sw0reloffer",
                "minsize": "100000",
            },
        ),
        (
            "taker/coinjoin",
            {
                "mixdepth": 0,
                "amount_sats": 100000,
                "destination": "bcrt1qdest",
                "counterparties": 3,
                "txfee": 500,
            },
        ),
    ],
)
def test_service_is_not_started_once_lock_began(
    lock_begins_after_auth: TestClient, auth_token: str, path: str, body: dict[str, Any]
) -> None:
    state = get_daemon_state()
    with (
        patch("jmwalletd._backend.get_backend", new_callable=AsyncMock),
        patch("maker.bot.MakerBot", return_value=AsyncMock()),
        patch("taker.taker.Taker", return_value=AsyncMock()),
    ):
        response = _post(lock_begins_after_auth, auth_token, path, body)

    assert response.status_code == 401
    assert state.coinjoin_state == CoinjoinState.NOT_RUNNING
    assert state._maker_task is None
    assert state._taker_task is None


@pytest.mark.parametrize("status", [PlanStatus.PENDING, PlanStatus.RUNNING])
def test_tumbler_is_not_started_once_lock_began(
    lock_begins_after_auth: TestClient, auth_token: str, status: PlanStatus
) -> None:
    """A RUNNING plan without a live runner would be reconciled and saved."""
    state = get_daemon_state()
    params = TumbleParameters(
        destinations=[
            "bcrt1qdest0000000000000000000000000000000dest",
            "bcrt1qdest1111111111111111111111111111111dest",
        ],
        mixdepth_balances={0: 100_000_000, 1: 50_000_000, 2: 25_000_000},
        seed=42,
    )
    plan = PlanBuilder(wallet_name=WALLET, params=params).build()
    plan.status = status
    save_plan(plan, state.data_dir)

    response = _post(lock_begins_after_auth, auth_token, "tumbler/start")

    assert response.status_code == 401
    assert state.coinjoin_state == CoinjoinState.NOT_RUNNING
    assert state.tumble_task is None
    assert load_plan(WALLET, state.data_dir).status == status


def test_rescan_is_not_started_once_lock_began(
    lock_begins_after_auth: TestClient, auth_token: str
) -> None:
    state = get_daemon_state()
    state.wallet_service.backend = MagicMock()

    response = lock_begins_after_auth.get(
        f"/api/v1/wallet/{WALLET}/rescanblockchain/0",
        headers={"Authorization": f"Bearer {auth_token}"},
    )

    assert response.status_code == 401
    assert state._rescan_task is None
    assert state.rescanning is False


def _replace_wallet(state: DaemonState, walletname: str = "wallet_b.jmdat") -> MagicMock:
    """Model the synchronous invalidation boundary of a lock/unlock cycle."""
    state.token_authority.reset()
    state._invalidate_ws_clients()
    state._tx_broadcast_notified.clear()
    replacement = MagicMock(wallet_fingerprint="bbccddee")
    state.wallet_service = replacement
    state.wallet_name = walletname
    state.wallet_mnemonic = "private seed belonging to B"
    state.wallet_bip39_passphrase = "B passphrase"
    state.token_authority.issue(walletname)
    return replacement


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "getseed", None),
        ("GET", "display", None),
        ("GET", "utxos", None),
        ("GET", "history", None),
        ("GET", "address/new/0", None),
        ("GET", "address/timelock/new/2027-01", None),
        ("GET", "getrescaninfo", None),
        ("GET", "rescanblockchain/0", None),
        ("GET", "lock", None),
        ("GET", "taker/status", None),
        ("GET", "taker/stop", None),
        ("GET", "maker/stop", None),
        ("GET", "tumbler/status", None),
        ("POST", "tumbler/stop", None),
        ("POST", "tumbler/plan", {"destinations": ["bcrt1qoriginal"]}),
        (
            "POST",
            "taker/direct-send",
            {"mixdepth": 0, "amount_sats": 1000, "destination": "bcrt1qoriginal"},
        ),
        ("DELETE", "tumbler/plan", None),
        ("POST", "signmessage", {"hd_path": "m/84'/0'/0'/0/0", "message": "test"}),
        ("POST", "freeze", {"utxo-string": "aa" * 32 + ":0", "freeze": True}),
        ("POST", "freeze-batch", {"entries": [{"utxo-string": "aa" * 32 + ":0", "freeze": True}]}),
        ("POST", "configget", {"section": "POLICY", "field": "minimum_makers"}),
        ("POST", "configset", {"section": "POLICY", "field": "minimum_makers", "value": "3"}),
    ],
)
def test_stale_handler_cannot_read_or_mutate_replacement_wallet(
    app_with_wallet: TestClient,
    auth_token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    state = get_daemon_state()
    app = cast(FastAPI, app_with_wallet.app)
    replacements: list[MagicMock] = []

    def switch_after_dependency_auth() -> None:
        replacements.append(_replace_wallet(state))

    app.dependency_overrides[require_wallet_match] = switch_after_dependency_auth
    try:
        response = app_with_wallet.request(
            method,
            f"/api/v1/wallet/{WALLET}/{path}",
            json=body,
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401
    assert "private seed belonging to B" not in response.text
    assert state.wallet_service is replacements[0]
    replacements[0].get_key_for_address.assert_not_called()
    replacements[0].freeze_utxo.assert_not_called()
    replacements[0].set_utxos_frozen.assert_not_called()
    assert not state.config_overrides


def test_yield_report_rechecks_after_dependency_authorization(
    app_with_wallet: TestClient,
    auth_token: str,
) -> None:
    state = get_daemon_state()
    app = cast(FastAPI, app_with_wallet.app)

    def token_after_replacement() -> str:
        _replace_wallet(state)
        return auth_token

    app.dependency_overrides[get_optional_token] = token_after_replacement
    try:
        with patch("jmwallet.history.format_yield_generator_report") as report:
            response = app_with_wallet.get(
                "/api/v1/wallet/yieldgen/report",
                headers={"Authorization": f"Bearer {auth_token}"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401
    report.assert_not_called()


@pytest.mark.parametrize(
    ("path", "seam", "result"),
    [
        ("display", "live_rescan_status", (False, None)),
        ("utxos", "sync_with_registered_bonds", None),
        ("display", "get_balance", 50_000_000),
        ("address/new/0", "get_new_address_verified", "bcrt1qoriginal"),
        (None, "live_rescan_status", (False, None)),
        (None, "get_block_height", 100),
    ],
)
def test_wallet_reads_recheck_after_await(
    app_with_wallet: TestClient,
    auth_token: str,
    path: str | None,
    seam: str,
    result: Any,
) -> None:
    state = get_daemon_state()
    original = state.wallet_service
    target = state if seam == "live_rescan_status" else original
    if seam == "get_block_height":
        target = original.backend

    async def switch_while_awaiting(*args: Any, **kwargs: Any) -> Any:
        _replace_wallet(state)
        return result

    with patch.object(target, seam, AsyncMock(side_effect=switch_while_awaiting)):
        response = app_with_wallet.get(
            f"/api/v1/wallet/{WALLET}/{path}" if path else "/api/v1/session",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    assert response.status_code == 401
    assert "private seed belonging to B" not in response.text


@pytest.mark.asyncio
async def test_wallet_lock_revalidates_inside_lifecycle_mutex(
    daemon_state_with_wallet: DaemonState,
    auth_token: str,
) -> None:
    from jmwalletd.routers.wallet import wallet_lock

    state = daemon_state_with_wallet
    entered = asyncio.Event()

    async def queued_lock() -> Any:
        entered.set()
        return await wallet_lock(WALLET, _auth={}, _wallet=None, state=state, token=auth_token)

    with patch.object(state, "_lock_wallet", AsyncMock()) as lock:
        async with state.wallet_lifecycle_lock:
            task = asyncio.create_task(queued_lock())
            await entered.wait()
            assert not task.done()
            replacement = _replace_wallet(state)
        with pytest.raises(InvalidToken):
            await task
    lock.assert_not_awaited()
    assert state.wallet_service is replacement


def _prepared_send() -> SignedDirectTx:
    raw = serialize_transaction(
        2,
        [TxInput.from_hex("aa" * 32, 0)],
        [TxOutput(50_000, b"\x00\x14" + b"\x22" * 20)],
        0,
    )
    return SignedDirectTx(
        txid=get_txid(raw.hex()),
        tx_hex=raw.hex(),
        fee=150,
        fee_rate=1.0,
        send_amount=50_000,
        change_amount=0,
        num_inputs=1,
        num_outputs=1,
        destination="bcrt1qdestination",
        change_address="",
        selected_utxos=[("aa" * 32, 0)],
        source_addresses=["bcrt1qoriginal"],
        inputs=[],
        outputs=[],
    )


@pytest.mark.parametrize("boundary", ["backend", "sync", "preparation"])
def test_revoked_direct_send_never_starts_submission(
    app_with_wallet: TestClient,
    auth_token: str,
    boundary: str,
) -> None:
    state = get_daemon_state()
    original = state.wallet_service
    original.data_dir = state.data_dir
    backend = MagicMock(broadcast_transaction=AsyncMock())

    async def switch_at_backend(*args: Any, **kwargs: Any) -> Any:
        if boundary == "backend":
            _replace_wallet(state)
        return backend

    async def switch_at_sync() -> None:
        if boundary == "sync":
            _replace_wallet(state)

    async def switch_at_preparation(**kwargs: Any) -> SignedDirectTx:
        _replace_wallet(state)
        return _prepared_send()

    original.sync_with_registered_bonds = AsyncMock(side_effect=switch_at_sync)
    with (
        patch("jmwalletd._backend.get_backend", side_effect=switch_at_backend),
        patch("jmwalletd.send.prepare_direct_send", side_effect=switch_at_preparation),
        patch("jmwalletd.send.append_history_entry") as append,
    ):
        response = _post(
            app_with_wallet,
            auth_token,
            "taker/direct-send",
            {
                "mixdepth": 0,
                "amount_sats": 50_000,
                "destination": "bcrt1qdestination",
            },
        )
    assert response.status_code == 401
    backend.broadcast_transaction.assert_not_awaited()
    append.assert_not_called()


@pytest.mark.parametrize("walletname", [WALLET, "wallet_b.jmdat"])
@pytest.mark.parametrize("failure", [False, True])
def test_authorized_submission_finishes_only_original_wallet_history(
    app_with_wallet: TestClient,
    auth_token: str,
    walletname: str,
    failure: bool,
) -> None:
    state = get_daemon_state()
    original = state.wallet_service
    original.data_dir = state.data_dir
    original.network = "regtest"
    original.wallet_fingerprint = "aabbccdd"
    prepared = _prepared_send()
    new_clients = []

    async def submit(tx_hex: str) -> str:
        assert tx_hex == prepared.tx_hex
        _replace_wallet(state, walletname)
        client = state.register_ws_client()
        assert state.authenticate_ws_client(client)
        new_clients.append(client)
        if failure:
            raise RuntimeError("submission outcome unknown")
        return prepared.txid

    backend = MagicMock(broadcast_transaction=AsyncMock(side_effect=submit))
    with (
        patch("jmwalletd._backend.get_backend", AsyncMock(return_value=backend)),
        patch("jmwalletd.send.prepare_direct_send", AsyncMock(return_value=prepared)),
    ):
        response = _post(
            app_with_wallet,
            auth_token,
            "taker/direct-send",
            {
                "mixdepth": 0,
                "amount_sats": 50_000,
                "destination": prepared.destination,
            },
        )
    assert response.status_code == (409 if failure else 200)
    if not failure:
        assert response.json()["txinfo"]["txid"] == prepared.txid
    rows = read_history(state.data_dir, wallet_fingerprint="aabbccdd")
    assert len(rows) == 1
    assert rows[0].success is not failure
    assert rows[0].failure_reason == (
        "broadcast failed: submission outcome unknown" if failure else ""
    )
    assert read_history(state.data_dir, wallet_fingerprint="bbccddee") == []
    backend.broadcast_transaction.assert_awaited_once_with(prepared.tx_hex)
    assert new_clients[0].queue.empty()
    assert prepared.txid not in state._tx_broadcast_notified


def test_tumbler_plan_cannot_be_saved_after_wallet_replacement(
    app_with_wallet: TestClient,
    auth_token: str,
) -> None:
    state = get_daemon_state()

    async def balances(*args: Any, **kwargs: Any) -> dict[int, int]:
        _replace_wallet(state)
        return {0: 100_000_000, 1: 50_000_000}

    with (
        patch("jmwalletd.routers.tumbler._mixdepth_balances", side_effect=balances),
        patch("jmwalletd.routers.tumbler.save_plan") as save,
    ):
        response = _post(
            app_with_wallet,
            auth_token,
            "tumbler/plan",
            {
                "destinations": ["bcrt1qoriginal"],
            },
        )
    assert response.status_code == 401
    save.assert_not_called()


@pytest.mark.parametrize("running", [False, True])
def test_tumbler_rechecks_plan_replacement_after_balances(
    app_with_wallet: TestClient,
    auth_token: str,
    running: bool,
) -> None:
    state = get_daemon_state()
    existing = PlanBuilder(
        wallet_name=WALLET,
        params=TumbleParameters(
            destinations=["bcrt1qdest0000000000000000000000000000000dest"],
            mixdepth_balances={0: 100_000_000, 1: 50_000_000},
            seed=42,
        ),
    ).build()

    async def balances(*args: Any, **kwargs: Any) -> dict[int, int]:
        save_plan(existing, state.data_dir)
        if running:
            state.tumble_runner = MagicMock()
            state.tumble_task = MagicMock()
            state.tumble_task.done.return_value = False
            state.tumble_plan_wallet = WALLET
        return {0: 100_000_000, 1: 50_000_000}

    with patch("jmwalletd.routers.tumbler._mixdepth_balances", side_effect=balances):
        response = _post(
            app_with_wallet,
            auth_token,
            "tumbler/plan",
            {
                "destinations": ["bcrt1qoriginal"],
                "force": running,
            },
        )
    assert response.status_code == (401 if running else 400)
    assert load_plan(WALLET, state.data_dir).plan_id == existing.plan_id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["maker", "taker"])
@pytest.mark.parametrize("boundary", ["stop", "task"])
async def test_stale_service_stop_cannot_cancel_replacement(
    daemon_state_with_wallet: DaemonState,
    auth_token: str,
    kind: str,
    boundary: str,
) -> None:
    from jmwalletd.routers.coinjoin import stop_coinjoin, stop_maker

    state = daemon_state_with_wallet
    replacement_task = MagicMock()
    replacement_task.done.return_value = False
    replacement_ref = MagicMock()
    running_state = CoinjoinState.MAKER_RUNNING if kind == "maker" else CoinjoinState.TAKER_RUNNING
    state.activate_coinjoin_state(running_state)

    def switch() -> None:
        _replace_wallet(state)
        state.activate_coinjoin_state(running_state)
        setattr(state, f"_{kind}_ref", replacement_ref)
        setattr(state, f"_{kind}_task", replacement_task)

    if boundary == "stop":
        service = MagicMock(stop=AsyncMock(side_effect=switch))
        setattr(state, f"_{kind}_ref", service)
        setattr(state, f"_{kind}_task", None)
    else:
        entered = asyncio.Event()

        async def old_operation() -> None:
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                switch()

        old_task = asyncio.create_task(old_operation())
        await entered.wait()
        setattr(state, f"_{kind}_ref", None)
        setattr(state, f"_{kind}_task", old_task)

    handler = stop_maker if kind == "maker" else stop_coinjoin
    with pytest.raises(InvalidToken):
        await handler(WALLET, _auth={}, _wallet=None, state=state, token=auth_token)
    replacement_task.cancel.assert_not_called()
    assert getattr(state, f"_{kind}_ref") is replacement_ref
    assert state.coinjoin_state == running_state


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["maker", "taker"])
async def test_background_service_setup_cannot_use_replacement_session(
    daemon_state_with_wallet: DaemonState,
    auth_token: str,
    kind: str,
) -> None:
    from jmwalletd.routers.coinjoin import do_coinjoin, start_maker

    state = daemon_state_with_wallet
    backend = MagicMock(close=AsyncMock())
    replacement_task = MagicMock()
    running_state = CoinjoinState.MAKER_RUNNING if kind == "maker" else CoinjoinState.TAKER_RUNNING

    async def get_backend(*args: Any, **kwargs: Any) -> Any:
        _replace_wallet(state)
        state.activate_coinjoin_state(running_state)
        setattr(state, f"_{kind}_task", replacement_task)
        return backend

    with (
        patch("jmwalletd._backend.get_backend", side_effect=get_backend),
        patch("maker.bot.MakerBot") as maker,
        patch("taker.taker.Taker") as taker,
    ):
        if kind == "maker":
            await start_maker(
                WALLET,
                StartMakerRequest(
                    txfee="1000",
                    cjfee_a="500",
                    cjfee_r="0.002",
                    ordertype="sw0reloffer",
                    minsize="100000",
                ),
                _auth={},
                _wallet=None,
                state=state,
                token=auth_token,
            )
        else:
            await do_coinjoin(
                WALLET,
                DoCoinjoinRequest(
                    mixdepth=0, amount_sats=1000, destination="bcrt1qoriginal", counterparties=3
                ),
                _auth={},
                _wallet=None,
                state=state,
                token=auth_token,
            )
        original_task = getattr(state, f"_{kind}_task")
        await original_task
    maker.assert_not_called()
    taker.assert_not_called()
    backend.close.assert_awaited_once()
    assert getattr(state, f"_{kind}_task") is replacement_task
    assert state.coinjoin_state == running_state


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["maker", "taker"])
async def test_tumbler_factories_and_events_stay_bound_to_original_generation(
    daemon_state_with_wallet: DaemonState,
    auth_token: str,
    kind: str,
) -> None:
    from jmwalletd.routers.tumbler import start_plan

    state = daemon_state_with_wallet
    plan = PlanBuilder(
        wallet_name=WALLET,
        params=TumbleParameters(
            destinations=["bcrt1qdest0000000000000000000000000000000dest"],
            mixdepth_balances={0: 100_000_000, 1: 50_000_000},
            seed=42,
        ),
    ).build()
    save_plan(plan, state.data_dir)
    backend = MagicMock(close=AsyncMock())
    runner = MagicMock(run=AsyncMock(return_value=plan))
    replacement_task = MagicMock()
    clients = []

    async def get_backend(*args: Any, **kwargs: Any) -> Any:
        _replace_wallet(state)
        state.tumble_task = replacement_task
        state.activate_coinjoin_state(CoinjoinState.TUMBLER_RUNNING)
        client = state.register_ws_client()
        assert state.authenticate_ws_client(client)
        clients.append(client)
        return backend

    with (
        patch("jmwalletd.routers.tumbler.TumbleRunner", return_value=runner) as runner_cls,
        patch("jmwalletd._backend.get_backend", side_effect=get_backend),
        patch("maker.bot.MakerBot") as maker,
        patch("taker.taker.Taker") as taker,
    ):
        await start_plan(WALLET, _auth={}, _wallet=None, state=state, token=auth_token)
        original_task = state.tumble_task
        context = runner_cls.call_args.args[1]
        factory = context.maker_factory if kind == "maker" else context.taker_factory
        with pytest.raises(InvalidToken):
            await factory(plan.phases[0])
        await original_task
        context.on_state_changed(plan)
    maker.assert_not_called()
    taker.assert_not_called()
    backend.close.assert_awaited_once()
    assert state.tumble_task is replacement_task
    assert state.coinjoin_state == CoinjoinState.TUMBLER_RUNNING
    assert clients[0].queue.empty()
