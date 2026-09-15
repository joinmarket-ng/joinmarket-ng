"""Authenticated native seller lifecycle for the single active wallet."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field, SecretStr, model_validator

from jmcore.credential_market import (
    CredentialPackage,
    Hex32,
    MarketModel,
    PaymentTerms,
    SignedDocument,
)
from jmcore.external_podle import ExternalPoDLE
from jmcore.market_store import MarketStore, MarketStoreError, WalletLedgerDiagnosis
from jmcore.paths import get_market_store_path, get_used_commitments_path
from jmcore.settings import get_settings
from jmcore.wallet_market import WalletMarketSellerOptions
from jmwalletd.deps import get_daemon_state, require_auth, require_wallet_match
from jmwalletd.errors import ActionNotAllowed, BackendNotReady, InvalidRequestFormat
from jmwalletd.state import CoinjoinState, DaemonState

router = APIRouter(tags=["credential-market"])


class LedgerMaintenanceRequest(MarketModel):
    history_confirmed: bool = False
    writers_stopped: bool = False
    previous_commitments_path: str | None = Field(default=None, max_length=4096)


class InventoryRequest(MarketModel):
    product: Literal["podle", "bond"]
    credential: ExternalPoDLE | None = None

    @model_validator(mode="after")
    def matching_product(self) -> InventoryRequest:
        if (self.product == "podle") != (self.credential is not None):
            raise ValueError("Only PoDLE inventory requires a supplied credential")
        return self


class SettlementRequest(MarketModel):
    onchain_outpoint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}:[0-9]{1,10}$")
    preimage: SecretStr | None = None
    acknowledge_ln_settlement: bool = False

    @model_validator(mode="after")
    def settlement_form(self) -> SettlementRequest:
        if (self.onchain_outpoint is None) == (self.preimage is None):
            raise ValueError("Exactly one settlement form is required")
        if self.preimage is not None:
            value = self.preimage.get_secret_value()
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("Preimage must be 32 lowercase hexadecimal bytes")
            if not self.acknowledge_ln_settlement:
                raise ValueError("Local Lightning settlement acknowledgment is required")
        elif self.acknowledge_ln_settlement:
            raise ValueError("Lightning acknowledgment cannot settle an on-chain payment")
        return self


def _wallet_directory(state: DaemonState) -> Path:
    path = state.wallet_service.data_dir
    if not isinstance(path, Path) or path.resolve() != state.data_dir.resolve():
        raise ActionNotAllowed("Wallet and daemon must use the same data directory.")
    return path.resolve()


@router.get("/wallet/{walletname}/market/ledger")
async def ledger_status(
    walletname: str,
    request: Request,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> WalletLedgerDiagnosis:
    async with state.wallet_lifecycle_lock:
        require_auth(request, state)
        require_wallet_match(walletname, state)
        path = _wallet_directory(state)
        return MarketStore.diagnose_wallet(
            get_market_store_path(path),
            wallet_id=state.wallet_service.market_wallet_id,
            commitments_path=get_used_commitments_path(path, create=False),
        )


@router.post("/wallet/{walletname}/market/ledger/{operation}")
async def maintain_ledger(
    walletname: str,
    operation: Literal["activate", "recover", "block", "rebind"],
    request: Request,
    body: LedgerMaintenanceRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> WalletLedgerDiagnosis:
    async with state.wallet_lifecycle_lock:
        require_auth(request, state)
        require_wallet_match(walletname, state)
        if (
            state.coinjoin_state != CoinjoinState.NOT_RUNNING
            or state._market_seller_ref is not None
        ):
            raise ActionNotAllowed("Stop wallet trading services before ledger maintenance.")
        path = _wallet_directory(state)
        try:
            wallet_id = state.wallet_service.market_wallet_id
            database = get_market_store_path(path)
            history = get_used_commitments_path(path, create=False)
            if operation == "activate":
                state.wallet_service.activate_market_ledger(
                    history_confirmed=body.history_confirmed
                )
            elif operation == "rebind":
                if body.previous_commitments_path is None:
                    raise ValueError("Previous commitments path is required")
                MarketStore.rebind_wallet(
                    database,
                    wallet_id=wallet_id,
                    previous_commitments_path=Path(body.previous_commitments_path),
                    commitments_path=history,
                    history_confirmed=body.history_confirmed,
                    writers_stopped=body.writers_stopped,
                )
            else:
                with MarketStore(database, wallet_id=wallet_id, create=False) as store:
                    if operation == "block":
                        store.mark_recovery_required()
                    else:
                        store.recover_wallet(history, history_confirmed=body.history_confirmed)
        except (MarketStoreError, ValueError, OSError) as exc:
            raise ActionNotAllowed(
                "Ledger maintenance could not complete; inspect ledger status."
            ) from exc
        return MarketStore.diagnose_wallet(database, wallet_id=wallet_id, commitments_path=history)


@router.get("/wallet/{walletname}/market/seller")
async def seller_status(
    walletname: str,
    request: Request,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> dict[str, str | None]:
    async with state.wallet_lifecycle_lock:
        require_auth(request, state)
        require_wallet_match(walletname, state)
        return state.market_seller_status()


@router.post("/wallet/{walletname}/market/seller/start", status_code=202)
async def start_seller(
    walletname: str,
    request: Request,
    body: WalletMarketSellerOptions,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> dict[str, str | None]:
    async with state.wallet_lifecycle_lock:
        # Dependencies ran before lock acquisition. Recheck against this wallet
        # generation so a queued request cannot act after lock/unlock or switching.
        require_auth(request, state)
        require_wallet_match(walletname, state)
        settings = get_settings().model_copy(update={"data_dir": state.data_dir}, deep=True)
        try:
            await state.start_market_seller(settings, body)
        except ImportError as exc:
            raise BackendNotReady("Market seller module is unavailable.") from exc
        except ValueError as exc:
            raise InvalidRequestFormat("Market seller wallet or configuration is invalid.") from exc
        return state.market_seller_status()


@router.post("/wallet/{walletname}/market/seller/stop")
async def stop_seller(
    walletname: str,
    request: Request,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> dict[str, str | None]:
    async with state.wallet_lifecycle_lock:
        require_auth(request, state)
        require_wallet_match(walletname, state)
        await state.stop_market_seller()
        result = state.market_seller_status()
        state.broadcast_ws({"market_seller": result})
        return result


@router.post("/wallet/{walletname}/market/seller/inventory")
async def add_inventory(
    walletname: str,
    request: Request,
    body: InventoryRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> dict[str, bool]:
    async with state.wallet_lifecycle_lock:
        require_auth(request, state)
        require_wallet_match(walletname, state)
        seller = state._market_seller_ref
        if seller is None:
            raise ActionNotAllowed("Start the market seller first.")
        try:
            if body.credential is None:
                seller.add_bond_inventory()
            else:
                seller.add_podle_inventory(body.credential)
        except (MarketStoreError, ValueError) as exc:
            raise ActionNotAllowed("Inventory could not be recorded.") from exc
        return {"recorded": True}


@router.post("/wallet/{walletname}/market/seller/payments")
async def add_payment(
    walletname: str,
    request: Request,
    body: PaymentTerms,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> dict[str, bool]:
    async with state.wallet_lifecycle_lock:
        require_auth(request, state)
        require_wallet_match(walletname, state)
        seller = state._market_seller_ref
        if seller is None:
            raise ActionNotAllowed("Start the market seller first.")
        try:
            seller.add_payment(body)
        except (MarketStoreError, ValueError) as exc:
            raise ActionNotAllowed("Payment terms could not be queued.") from exc
        return {"recorded": True}


@router.get("/wallet/{walletname}/market/seller/pending")
async def pending_quotes(
    walletname: str,
    request: Request,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> list[SignedDocument]:
    async with state.wallet_lifecycle_lock:
        require_auth(request, state)
        require_wallet_match(walletname, state)
        seller = state._market_seller_ref
        if seller is None:
            raise ActionNotAllowed("Start the market seller first.")
        try:
            return seller.pending_quotes()
        except (MarketStoreError, ValueError) as exc:
            raise ActionNotAllowed("Seller quotes are unavailable.") from exc


@router.post("/wallet/{walletname}/market/seller/settle/{quote_id}")
async def settle_quote(
    walletname: str,
    quote_id: Hex32,
    request: Request,
    body: SettlementRequest,
    _auth: dict[str, Any] = Depends(require_auth),
    _wallet: None = Depends(require_wallet_match),
    state: DaemonState = Depends(get_daemon_state),
) -> CredentialPackage:
    async with state.wallet_lifecycle_lock:
        require_auth(request, state)
        require_wallet_match(walletname, state)
        seller = state._market_seller_ref
        if seller is None:
            raise ActionNotAllowed("Start the market seller first.")
    # Let wallet lock revoke/cancel service while chain verification awaits.
    try:
        package = await seller.settle(
            quote_id,
            onchain_outpoint=body.onchain_outpoint,
            preimage=bytes.fromhex(body.preimage.get_secret_value()) if body.preimage else None,
            acknowledge_ln_settlement=body.acknowledge_ln_settlement,
        )
    except (MarketStoreError, ValueError) as exc:
        raise ActionNotAllowed("Settlement could not be verified or finalized.") from exc
    async with state.wallet_lifecycle_lock:
        require_auth(request, state)
        require_wallet_match(walletname, state)
        if state._market_seller_ref is not seller:
            raise ActionNotAllowed("Seller session changed during settlement.")
        return package
