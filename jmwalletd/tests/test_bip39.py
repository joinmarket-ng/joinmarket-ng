"""BIP39 identity, reopen, and downstream daemon regressions."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from jmcore.settings import JoinMarketSettings, reset_settings
from jmwalletd._backend import get_backend, reset_backend
from jmwalletd.errors import InvalidCredentials
from jmwalletd.maker_config import build_daemon_maker_config
from jmwalletd.models import CreateWalletRequest, UnlockWalletRequest
from jmwalletd.routers.coinjoin import build_coinjoin_taker_config
from jmwalletd.routers.tumbler import build_tumbler_taker_config
from jmwalletd.routers.wallet import wallet_unlock
from jmwalletd.state import DaemonState
from jmwalletd.wallet_ops import (
    _derive_key_from_header,
    _load_wallet_file,
    _save_wallet_file,
    create_wallet,
    open_wallet_with_mnemonic,
    recover_wallet,
)

MNEMONIC = "abandon " * 11 + "about"
PASSPHRASE = "test daemon passphrase"


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(tmp_path / "absent.toml"))
    reset_settings()
    yield
    reset_settings()


@pytest.mark.parametrize("passphrase", ["", PASSPHRASE, "caf\u00e9"])
def test_bound_container_reopen_and_mismatch(tmp_path: Path, passphrase: str) -> None:
    path = tmp_path / "wallet.jmdat"
    _save_wallet_file(
        wallet_path=path,
        mnemonic=MNEMONIC,
        password="encryption",
        wallet_type="sw-fb",
        bip39_passphrase=passphrase,
    )
    before = path.read_bytes()
    assert before[4] == (2 if passphrase else 1)
    assert _load_wallet_file(
        wallet_path=path, password="encryption", bip39_passphrase=passphrase
    ) == (MNEMONIC, None)
    with pytest.raises(ValueError, match="saved wallet identity"):
        _load_wallet_file(wallet_path=path, password="encryption", bip39_passphrase="wrong")
    if passphrase:
        with pytest.raises(ValueError, match="saved wallet identity"):
            _load_wallet_file(wallet_path=path, password="encryption")
    assert path.read_bytes() == before


def test_legacy_container_remains_unmodified(tmp_path: Path) -> None:
    path = tmp_path / "legacy.jmdat"
    _save_wallet_file(
        wallet_path=path, mnemonic=MNEMONIC, password="encryption", wallet_type="sw-fb"
    )
    before = path.read_bytes()
    assert _load_wallet_file(
        wallet_path=path, password="encryption", bip39_passphrase=PASSPHRASE
    ) == (MNEMONIC, None)
    assert path.read_bytes() == before


def test_request_passphrase_is_masked() -> None:
    request = CreateWalletRequest(
        walletname="wallet.jmdat", password="encryption", bip39_passphrase=SecretStr(PASSPHRASE)
    )
    assert PASSPHRASE not in repr(request)
    assert PASSPHRASE not in request.model_dump_json()
    assert UnlockWalletRequest(password="encryption").bip39_passphrase.get_secret_value() == ""


@pytest.mark.parametrize("operation", ["create", "recover", "open"])
async def test_lifecycle_carries_passphrase(tmp_path: Path, operation: str) -> None:
    path = tmp_path / "wallet.jmdat"
    backend = MagicMock()
    backend.get_block_height = AsyncMock(return_value=10)
    service = MagicMock()
    service.sync = AsyncMock()
    service.sync_with_registered_bonds = AsyncMock()
    if operation == "open":
        _save_wallet_file(
            wallet_path=path,
            mnemonic=MNEMONIC,
            password="encryption",
            wallet_type="sw",
            bip39_passphrase=PASSPHRASE,
        )
    with (
        patch(
            "jmwalletd._backend.get_backend", new_callable=AsyncMock, return_value=backend
        ) as factory,
        patch("jmwallet.wallet.service.WalletService", return_value=service) as constructor,
        patch("jmwalletd.wallet_ops._get_network", return_value="regtest"),
    ):
        kwargs = dict(
            wallet_path=path, password="encryption", data_dir=tmp_path, bip39_passphrase=PASSPHRASE
        )
        if operation == "create":
            await create_wallet(**kwargs, wallet_type="sw")
        elif operation == "recover":
            await recover_wallet(**kwargs, wallet_type="sw", seedphrase=MNEMONIC)
        else:
            await open_wallet_with_mnemonic(**kwargs)
    assert factory.call_args.kwargs["passphrase"] == PASSPHRASE
    assert constructor.call_args.kwargs["passphrase"] == PASSPHRASE
    _load_wallet_file(wallet_path=path, password="encryption", bip39_passphrase=PASSPHRASE)


async def test_mismatch_fails_before_backend(tmp_path: Path) -> None:
    path = tmp_path / "wallet.jmdat"
    _save_wallet_file(
        wallet_path=path,
        mnemonic=MNEMONIC,
        password="encryption",
        wallet_type="sw",
        bip39_passphrase=PASSPHRASE,
    )
    with (
        patch("jmwalletd._backend.get_backend", new_callable=AsyncMock) as factory,
        pytest.raises(ValueError, match="saved wallet identity"),
    ):
        await open_wallet_with_mnemonic(wallet_path=path, password="encryption", data_dir=tmp_path)
    factory.assert_not_called()


async def test_backend_cache_separates_passphrase_identities(tmp_path: Path) -> None:
    settings = JoinMarketSettings()
    settings.bitcoin.backend_type = "descriptor_wallet"
    reset_backend()
    try:
        with patch("jmcore.settings.get_settings", return_value=settings):
            empty = await get_backend(tmp_path, mnemonic=MNEMONIC, network="regtest")
            protected = await get_backend(
                tmp_path, mnemonic=MNEMONIC, network="regtest", passphrase=PASSPHRASE
            )
            same = await get_backend(
                tmp_path, mnemonic=MNEMONIC, network="regtest", passphrase=PASSPHRASE
            )
        assert empty is not protected
        assert protected is same
    finally:
        reset_backend()


def test_maker_and_taker_configs_carry_passphrase(tmp_path: Path) -> None:
    settings = JoinMarketSettings()
    config = build_daemon_maker_config(settings, MNEMONIC, tmp_path, passphrase=PASSPHRASE)
    assert config.passphrase.get_secret_value() == PASSPHRASE
    body = MagicMock(amount_sats=10000, destination="", mixdepth=0, counterparties=4, txfee=None)
    for builder, input_args in (
        (build_coinjoin_taker_config, {"body": body}),
        (
            build_tumbler_taker_config,
            {"phase": MagicMock(amount=10000, mixdepth=0, counterparty_count=4)},
        ),
    ):
        constructor = MagicMock()
        builder(
            **input_args,
            mnemonic=MNEMONIC,
            jm_settings=settings,
            taker_config_cls=constructor,
            passphrase=PASSPHRASE,
        )
        assert constructor.call_args.kwargs["passphrase"].get_secret_value() == PASSPHRASE


async def test_wrong_bound_passphrase_preserves_active_session(
    daemon_state_with_wallet: DaemonState,
) -> None:
    state = daemon_state_with_wallet
    state.wallet_password = "encryption"
    state.wallet_bip39_passphrase = PASSPHRASE
    path = state.wallets_dir / state.wallet_name
    _save_wallet_file(
        wallet_path=path,
        mnemonic=MNEMONIC,
        password="encryption",
        wallet_type="sw",
        bip39_passphrase=PASSPHRASE,
    )
    before = state.token_authority.issue(state.wallet_name)
    service = state.wallet_service
    with pytest.raises(InvalidCredentials):
        await wallet_unlock(
            state.wallet_name,
            UnlockWalletRequest(password="encryption", bip39_passphrase=SecretStr("wrong")),
            state,
        )
    assert state.wallet_service is service
    assert state.wallet_bip39_passphrase == PASSPHRASE
    state.token_authority.verify_access(before.token)


async def test_legacy_identity_change_resets_session_and_lock_clears_secret(
    daemon_state_with_wallet: DaemonState,
) -> None:
    state = daemon_state_with_wallet
    state.wallet_password = "encryption"
    path = state.wallets_dir / state.wallet_name
    _save_wallet_file(wallet_path=path, mnemonic=MNEMONIC, password="encryption", wallet_type="sw")
    before = state.token_authority.issue(state.wallet_name)
    new_service = MagicMock()
    new_service.sync = AsyncMock()
    new_service.sync_with_registered_bonds = AsyncMock()
    with (
        patch(
            "jmwalletd.routers.wallet.open_wallet_with_mnemonic",
            new_callable=AsyncMock,
            return_value=(new_service, MNEMONIC),
        ),
        patch("jmwalletd.routers.wallet._require_tx_monitor_ready", new_callable=AsyncMock),
        patch.object(state, "start_tx_monitor"),
        patch("jmwalletd.routers.wallet._background_wallet_sync", new_callable=AsyncMock),
    ):
        await wallet_unlock(
            state.wallet_name,
            UnlockWalletRequest(password="encryption", bip39_passphrase=SecretStr(PASSPHRASE)),
            state,
        )
        if state._wallet_sync_task is not None:
            await state._wallet_sync_task
    assert state.wallet_service is new_service
    assert state.wallet_bip39_passphrase == PASSPHRASE
    with pytest.raises(jwt.InvalidTokenError):
        state.token_authority.verify_access(before.token)
    await state.lock_wallet()
    assert state.wallet_bip39_passphrase == ""


def _replace_payload(path: Path, update: dict[str, Any]) -> None:
    raw = path.read_bytes()
    key, ciphertext = _derive_key_from_header(raw, password="encryption")
    fernet = Fernet(base64.urlsafe_b64encode(key))
    data = json.loads(fernet.decrypt(ciphertext))
    data.update(update)
    if update == {}:
        data.pop("identity_version")
        data.pop("wallet_identity")
    path.write_bytes(raw[: len(raw) - len(ciphertext)] + fernet.encrypt(json.dumps(data).encode()))


@pytest.mark.parametrize(
    "update",
    [
        {},
        {"wallet_identity": None},
        {"identity_version": None},
        {"identity_version": 99},
        {"wallet_identity": {"fingerprint": "11223344", "bip39": "none"}},
    ],
)
async def test_invalid_v2_preserves_active_session(
    daemon_state_with_wallet: DaemonState,
    update: dict[str, Any],
) -> None:
    state = daemon_state_with_wallet
    target = state.wallets_dir / "other.jmdat"
    _save_wallet_file(
        wallet_path=target,
        mnemonic=MNEMONIC,
        password="encryption",
        wallet_type="sw",
        bip39_passphrase=PASSPHRASE,
    )
    _replace_payload(target, update)
    service = state.wallet_service
    tokens = state.token_authority.issue(state.wallet_name)
    client = state.register_ws_client()
    assert state.authenticate_ws_client(client)
    generation = client.generation
    assert generation is not None
    with (
        patch("jmwalletd._backend.get_backend", new_callable=AsyncMock) as factory,
        pytest.raises(InvalidCredentials),
    ):
        await wallet_unlock(
            "other.jmdat",
            UnlockWalletRequest(password="encryption", bip39_passphrase=SecretStr(PASSPHRASE)),
            state,
        )
    factory.assert_not_called()
    assert state.wallet_service is service
    state.token_authority.verify_access(tokens.token)
    assert state.ws_client_is_current(client, generation)


async def test_normalized_reunlock_retains_refresh_token(
    daemon_state_with_wallet: DaemonState,
) -> None:
    state = daemon_state_with_wallet
    state.wallet_password = "encryption"
    state.wallet_bip39_passphrase = "caf\u00e9"
    (state.wallets_dir / state.wallet_name).write_bytes(b"already open")
    tokens = state.token_authority.issue(state.wallet_name)
    with (
        patch(
            "jmwalletd.routers.wallet.open_wallet_with_mnemonic",
            side_effect=AssertionError("reopened"),
        ),
        patch("jmwalletd.routers.wallet._require_tx_monitor_ready", new_callable=AsyncMock),
    ):
        result = await wallet_unlock(
            state.wallet_name,
            UnlockWalletRequest(password="encryption", bip39_passphrase=SecretStr("cafe\u0301")),
            state,
        )
    state.token_authority.verify_refresh(result.refresh_token)
    state.token_authority.verify_refresh(tokens.refresh_token)
    state.token_authority.verify_access(tokens.token)
