"""Tests for jmwalletd.models — Pydantic request/response models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jmwalletd.models import (
    ConfigGetRequest,
    ConfigSetRequest,
    CreateWalletRequest,
    DirectSendRequest,
    DoCoinjoinRequest,
    ErrorMessage,
    FreezeRequest,
    GetAddressResponse,
    GetInfoResponse,
    HistoryEntry,
    ListUtxosResponse,
    ListWalletsResponse,
    LockWalletResponse,
    RecoverWalletRequest,
    RescanBlockchainResponse,
    SessionResponse,
    StartMakerRequest,
    TokenRequest,
    TokenResponse,
    TxInfo,
    TxInput,
    TxOutput,
    UnlockWalletRequest,
    UnlockWalletResponse,
    UTXOEntry,
    WalletDisplayAccount,
    WalletDisplayBranch,
    WalletDisplayEntry,
    WalletDisplayResponse,
    WalletHistoryResponse,
    WalletInfo,
    YieldGenReportResponse,
)


class TestErrorMessage:
    def test_create(self) -> None:
        msg = ErrorMessage(message="something went wrong")
        assert msg.message == "something went wrong"

    def test_serialization(self) -> None:
        msg = ErrorMessage(message="bad request")
        d = msg.model_dump()
        assert d == {"message": "bad request"}


class TestTokenModels:
    def test_token_request(self) -> None:
        req = TokenRequest(grant_type="refresh_token", refresh_token="abc123")
        assert req.grant_type == "refresh_token"
        assert req.refresh_token == "abc123"

    def test_token_response(self) -> None:
        resp = TokenResponse(
            walletname="test.jmdat",
            token="tok",
            token_type="bearer",
            expires_in=1800,
            scope="walletrpc dGVzdA==",
            refresh_token="ref",
        )
        assert resp.walletname == "test.jmdat"
        assert resp.expires_in == 1800


class TestWalletCreationModels:
    def test_create_wallet_default_type(self) -> None:
        req = CreateWalletRequest(walletname="w.jmdat", password="pass123")
        assert req.wallettype == "sw-fb"

    def test_create_wallet_custom_type(self) -> None:
        req = CreateWalletRequest(walletname="w.jmdat", password="p", wallettype="sw")
        assert req.wallettype == "sw"

    def test_recover_wallet(self) -> None:
        req = RecoverWalletRequest(
            walletname="w.jmdat",
            password="p",
            wallettype="sw",
            seedphrase="abandon " * 11 + "about",
        )
        assert req.seedphrase == "abandon " * 11 + "about"

    def test_unlock_request(self) -> None:
        req = UnlockWalletRequest(password="secret")
        assert req.password == "secret"

    def test_unlock_response(self) -> None:
        resp = UnlockWalletResponse(
            walletname="w.jmdat",
            token="t",
            token_type="bearer",
            expires_in=1800,
            scope="s",
            refresh_token="r",
        )
        assert resp.walletname == "w.jmdat"


class TestWalletDisplayModels:
    def test_display_entry(self) -> None:
        entry = WalletDisplayEntry(
            hd_path="m/84'/1'/0'/0/0",
            address="bcrt1qtest",
            amount="1.00000000",
            available_balance="1.00000000",
            status="used",
            label="",
            extradata="",
        )
        assert entry.hd_path == "m/84'/1'/0'/0/0"

    def test_display_branch(self) -> None:
        branch = WalletDisplayBranch(
            branch="external addresses m/84'/1'/0'/0",
            balance="1.00000000",
            available_balance="1.00000000",
            entries=[],
        )
        assert branch.entries == []

    def test_display_account(self) -> None:
        account = WalletDisplayAccount(
            account="m/84'/1'/0'",
            account_balance="1.00000000",
            available_balance="1.00000000",
            branches=[],
        )
        assert account.account == "m/84'/1'/0'"

    def test_wallet_info(self) -> None:
        info = WalletInfo(
            wallet_name="test.jmdat",
            total_balance="2.00000000",
            available_balance="2.00000000",
            accounts=[],
        )
        assert info.total_balance == "2.00000000"

    def test_wallet_display_response(self) -> None:
        info = WalletInfo(
            wallet_name="test.jmdat",
            total_balance="0.00000000",
            available_balance="0.00000000",
            accounts=[],
        )
        resp = WalletDisplayResponse(walletname="test.jmdat", walletinfo=info)
        assert resp.walletname == "test.jmdat"


class TestUTXOModels:
    def test_utxo_entry(self) -> None:
        utxo = UTXOEntry(
            utxo="abc123:0",
            address="bcrt1qtest",
            path="m/84'/1'/0'/0/0",
            label="",
            value=100_000,
            tries=0,
            tries_remaining=3,
            external=False,
            mixdepth=0,
            confirmations=6,
            frozen=False,
        )
        assert utxo.value == 100_000
        assert utxo.frozen is False

    def test_list_utxos(self) -> None:
        resp = ListUtxosResponse(utxos=[])
        assert resp.utxos == []


class TestTransactionModels:
    def test_tx_input(self) -> None:
        inp = TxInput(outpoint="abc:0", scriptSig="", nSequence=0xFFFFFFFF, witness="")
        assert inp.outpoint == "abc:0"

    def test_tx_output(self) -> None:
        out = TxOutput(value_sats=50_000, scriptPubKey="0014abcd", address="bcrt1qtest")
        assert out.value_sats == 50_000

    def test_tx_info(self) -> None:
        info = TxInfo(hex="0200...", inputs=[], outputs=[], txid="abc", nLockTime=0, nVersion=2)
        assert info.nVersion == 2


class TestDirectSendRequest:
    def test_valid(self) -> None:
        req = DirectSendRequest(mixdepth=0, amount_sats=100_000, destination="bcrt1qtest")
        assert req.mixdepth == 0
        assert req.amount_sats == 100_000

    def test_with_txfee(self) -> None:
        req = DirectSendRequest(
            mixdepth=0, amount_sats=100_000, destination="bcrt1qtest", txfee=1000
        )
        assert req.txfee == 1000

    def test_sweep_amount_is_zero(self) -> None:
        # ``0`` is the sentinel for "sweep the whole mixdepth" and must stay valid.
        req = DirectSendRequest(mixdepth=0, amount_sats=0, destination="bcrt1qtest")
        assert req.amount_sats == 0

    def test_negative_amount_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DirectSendRequest(mixdepth=0, amount_sats=-1, destination="bcrt1qtest")

    def test_negative_mixdepth_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DirectSendRequest(mixdepth=-1, amount_sats=100, destination="bcrt1qtest")

    def test_negative_txfee_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DirectSendRequest(mixdepth=0, amount_sats=100, destination="bcrt1qtest", txfee=-1)

    def test_amount_above_money_supply_rejected(self) -> None:
        # 21M BTC + 1 sat must not be accepted; otherwise downstream
        # ``int.to_bytes(8, ...)`` for unsigned 64-bit serialization can crash.
        with pytest.raises(ValidationError):
            DirectSendRequest(
                mixdepth=0,
                amount_sats=21_000_000 * 100_000_000 + 1,
                destination="bcrt1qtest",
            )


class TestDoCoinjoinRequest:
    def test_valid(self) -> None:
        req = DoCoinjoinRequest(
            mixdepth=0,
            amount_sats=100_000,
            counterparties=5,
            destination="bcrt1qtest",
        )
        assert req.counterparties == 5

    def test_counterparties_below_minimum_rejected(self) -> None:
        # A coinjoin with only 1 counterparty defeats the purpose; the
        # protocol's effective minimum is 2.
        with pytest.raises(ValidationError):
            DoCoinjoinRequest(
                mixdepth=0,
                amount_sats=100_000,
                counterparties=1,
                destination="bcrt1qtest",
            )

    def test_counterparties_huge_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DoCoinjoinRequest(
                mixdepth=0,
                amount_sats=100_000,
                counterparties=10_000_000,
                destination="bcrt1qtest",
            )

    def test_negative_amount_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DoCoinjoinRequest(
                mixdepth=0,
                amount_sats=-1,
                counterparties=5,
                destination="bcrt1qtest",
            )


class TestFreezeRequest:
    def test_with_alias(self) -> None:
        """The reference API uses 'utxo-string' (hyphenated) in JSON."""
        req = FreezeRequest.model_validate({"utxo-string": "abc:0", "freeze": True})
        assert req.utxo_string == "abc:0"
        assert req.freeze is True

    def test_with_python_name(self) -> None:
        req = FreezeRequest(utxo_string="abc:0", freeze=False)
        assert req.utxo_string == "abc:0"

    def test_serialization_uses_alias(self) -> None:
        req = FreezeRequest(utxo_string="abc:0", freeze=True)
        d = req.model_dump(by_alias=True)
        assert "utxo-string" in d


class TestConfigModels:
    def test_config_get(self) -> None:
        req = ConfigGetRequest(section="POLICY", field="tx_fees")
        assert req.section == "POLICY"

    def test_config_set(self) -> None:
        req = ConfigSetRequest(section="POLICY", field="tx_fees", value="3000")
        assert req.value == "3000"


class TestMakerModels:
    def test_start_maker_all_strings(self) -> None:
        """Reference API sends all maker params as strings."""
        req = StartMakerRequest(
            txfee="0",
            cjfee_a="250",
            cjfee_r="0.00025",
            ordertype="reloffer",
            minsize="100000",
        )
        assert req.ordertype == "reloffer"


class TestSessionResponse:
    def test_minimal(self) -> None:
        resp = SessionResponse(
            session=False,
            maker_running=False,
            coinjoin_in_process=False,
            wallet_name="none",
        )
        assert resp.session is False

    def test_full(self) -> None:
        resp = SessionResponse(
            session=True,
            maker_running=True,
            coinjoin_in_process=True,
            wallet_name="w.jmdat",
            schedule=[["entry1", 0, 1.0]],
            offer_list=[{"oid": "1"}],
            nickname="J5abc",
            rescanning=False,
            block_height=800_000,
            descriptor_wallet_name="jm_abcdef12_regtest",
        )
        assert resp.maker_running is True
        assert resp.block_height == 800_000
        assert resp.descriptor_wallet_name == "jm_abcdef12_regtest"

    def test_descriptor_wallet_name_optional(self) -> None:
        resp = SessionResponse(
            session=True,
            maker_running=False,
            coinjoin_in_process=False,
            wallet_name="w.jmdat",
        )
        assert resp.descriptor_wallet_name is None


class TestMiscModels:
    def test_getinfo(self) -> None:
        resp = GetInfoResponse(version="0.17.0")
        assert resp.version == "0.17.0"

    def test_list_wallets(self) -> None:
        resp = ListWalletsResponse(wallets=["a.jmdat", "b.jmdat"])
        assert len(resp.wallets) == 2

    def test_lock_wallet(self) -> None:
        resp = LockWalletResponse(walletname="w.jmdat", already_locked=False)
        assert resp.already_locked is False

    def test_get_address(self) -> None:
        resp = GetAddressResponse(address="bcrt1qtest")
        assert resp.address == "bcrt1qtest"

    def test_rescan_response(self) -> None:
        resp = RescanBlockchainResponse(walletname="w.jmdat")
        assert resp.walletname == "w.jmdat"

    def test_yieldgen_report(self) -> None:
        resp = YieldGenReportResponse(yigen_data=["line1", "line2"])
        assert len(resp.yigen_data) == 2

    def test_wallet_history_response(self) -> None:
        entry = HistoryEntry(
            timestamp="2024-01-01T10:00:00",
            role="maker",
            cj_amount=100_000,
            fee_received=2_500,
            txid="ab" * 32,
        )
        resp = WalletHistoryResponse(history=[entry])
        assert len(resp.history) == 1
        assert resp.history[0].role == "maker"
        assert resp.history[0].fee_received == 2_500

    def test_wallet_history_response_defaults_empty(self) -> None:
        resp = WalletHistoryResponse()
        assert resp.history == []
