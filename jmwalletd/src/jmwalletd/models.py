"""Pydantic request/response models for the wallet daemon API.

All models match the schemas defined in the reference JoinMarket OpenAPI spec
(jm-wallet-rpc.yaml) to ensure JAM compatibility.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Shared / Error
# ---------------------------------------------------------------------------


class ErrorMessage(BaseModel):
    """Standard error response body."""

    message: str = ""


# ---------------------------------------------------------------------------
# Token / Auth
# ---------------------------------------------------------------------------


class TokenRequest(BaseModel):
    """POST /api/v1/token request."""

    grant_type: str
    refresh_token: str


class TokenResponse(BaseModel):
    """Token issuance response (used by create, unlock, recover, refresh)."""

    walletname: str = ""
    token: str
    token_type: str = "bearer"
    expires_in: int = 1800
    scope: str = ""
    refresh_token: str


# ---------------------------------------------------------------------------
# Wallet lifecycle
# ---------------------------------------------------------------------------


class CreateWalletRequest(BaseModel):
    """POST /api/v1/wallet/create request."""

    walletname: str
    password: str
    wallettype: str = "sw-fb"


class CreateWalletResponse(BaseModel):
    """Response for wallet create and recover."""

    walletname: str
    seedphrase: str
    token: str
    token_type: str = "bearer"
    expires_in: int = 1800
    scope: str = ""
    refresh_token: str


class RecoverWalletRequest(BaseModel):
    """POST /api/v1/wallet/recover request."""

    walletname: str
    password: str
    wallettype: str = "sw-fb"
    seedphrase: str


class UnlockWalletRequest(BaseModel):
    """POST /api/v1/wallet/{walletname}/unlock request."""

    password: str


class UnlockWalletResponse(BaseModel):
    """Response for wallet unlock."""

    walletname: str
    token: str
    token_type: str = "bearer"
    expires_in: int = 1800
    scope: str = ""
    refresh_token: str


class LockWalletResponse(BaseModel):
    """Response for wallet lock."""

    walletname: str
    already_locked: bool


class ListWalletsResponse(BaseModel):
    """Response for GET /api/v1/wallet/all."""

    wallets: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Wallet info / display
# ---------------------------------------------------------------------------


class GetInfoResponse(BaseModel):
    """Response for GET /api/v1/getinfo."""

    version: str


class SessionResponse(BaseModel):
    """Response for GET /api/v1/session."""

    session: bool
    maker_running: bool
    coinjoin_in_process: bool
    schedule: list[list[str | int | float]] | None = None
    wallet_name: str = ""
    offer_list: list[dict[str, str | int | float]] | None = None
    nickname: str | None = None
    rescanning: bool = False
    block_height: int | None = None


class WalletDisplayEntry(BaseModel):
    """A single address entry in the wallet display."""

    hd_path: str
    address: str
    amount: str
    available_balance: str = "0.00000000"
    status: str = ""
    label: str = ""
    extradata: str = ""


class WalletDisplayBranch(BaseModel):
    """A branch (external/internal/bond) within an account."""

    branch: str
    balance: str
    available_balance: str = "0.00000000"
    entries: list[WalletDisplayEntry] = Field(default_factory=list)


class WalletDisplayAccount(BaseModel):
    """A single account (mixdepth) in the wallet display."""

    account: str
    account_balance: str
    available_balance: str = "0.00000000"
    branches: list[WalletDisplayBranch] = Field(default_factory=list)


class WalletInfo(BaseModel):
    """The walletinfo object within the display response."""

    wallet_name: str = "JM wallet"
    total_balance: str = "0.00000000"
    available_balance: str = "0.00000000"
    accounts: list[WalletDisplayAccount] = Field(default_factory=list)


class WalletDisplayResponse(BaseModel):
    """Response for GET /api/v1/wallet/{walletname}/display."""

    walletname: str
    walletinfo: WalletInfo


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------


class GetAddressResponse(BaseModel):
    """Response for new address / timelock address requests."""

    address: str


# ---------------------------------------------------------------------------
# UTXOs
# ---------------------------------------------------------------------------


class UTXOEntry(BaseModel):
    """A single UTXO in the listutxos response."""

    utxo: str  # "txid:vout"
    address: str
    path: str
    label: str = ""
    value: int
    tries: int = 0
    tries_remaining: int = 3
    external: bool = False
    mixdepth: int
    confirmations: int
    frozen: bool = False


class ListUtxosResponse(BaseModel):
    """Response for GET /api/v1/wallet/{walletname}/utxos."""

    utxos: list[UTXOEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


class GetSeedResponse(BaseModel):
    """Response for GET /api/v1/wallet/{walletname}/getseed."""

    seedphrase: str


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class TxInput(BaseModel):
    """A transaction input."""

    outpoint: str
    scriptSig: str = ""
    nSequence: int = 4294967295
    witness: str = ""


class TxOutput(BaseModel):
    """A transaction output."""

    value_sats: int
    scriptPubKey: str
    address: str


class TxInfo(BaseModel):
    """Full transaction information."""

    hex: str
    inputs: list[TxInput] = Field(default_factory=list)
    outputs: list[TxOutput] = Field(default_factory=list)
    txid: str
    nLockTime: int = 0
    nVersion: int = 2


class DirectSendRequest(BaseModel):
    """POST /api/v1/wallet/{walletname}/taker/direct-send request."""

    mixdepth: int
    amount_sats: int
    destination: str
    txfee: int | None = None


class DirectSendResponse(BaseModel):
    """Response for direct-send."""

    txinfo: TxInfo


# ---------------------------------------------------------------------------
# Coinjoin / Taker
# ---------------------------------------------------------------------------


class DoCoinjoinRequest(BaseModel):
    """POST /api/v1/wallet/{walletname}/taker/coinjoin request."""

    mixdepth: int
    amount_sats: int
    counterparties: int
    destination: str
    txfee: int | None = None


class TumblerPlanRequest(BaseModel):
    """POST /api/v1/wallet/{walletname}/tumbler/plan request.

    Destinations are the external bitcoin addresses the tumble should
    ultimately sweep funds to; order is significant (first destination
    maps to the first non-empty landing mixdepth). ``parameters`` lets
    the caller override the builder defaults; omit for sensible knobs.
    ``force`` overrides a pending-only plan on disk; plans that are
    ``RUNNING`` in the daemon are always protected (stop first).
    """

    destinations: list[str] = Field(..., min_length=1)
    parameters: dict[str, object] | None = None
    force: bool = False


class TumblerPhaseResponse(BaseModel):
    """A single phase rendered for the API. Keeps the discriminator flat."""

    kind: str
    index: int
    status: str
    wait_seconds: float
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    # Optional variant fields; only the ones relevant to ``kind`` are populated.
    mixdepth: int | None = None
    amount: int | None = None
    amount_fraction: float | None = None
    counterparty_count: int | None = None
    destination: str | None = None
    rounding: int | None = None
    txid: str | None = None
    txids: list[str] | None = None
    completed_count: int | None = None
    cj_count: int | None = None
    duration_seconds: float | None = None
    target_cj_count: int | None = None
    idle_timeout_seconds: float | None = None
    cj_served: int | None = None


class TumblerPlanResponse(BaseModel):
    """Response body returned by tumbler plan / status endpoints."""

    plan_id: str
    wallet_name: str
    status: str
    destinations: list[str]
    current_phase: int
    phases: list[TumblerPhaseResponse]
    created_at: str
    updated_at: str
    error: str | None = None
    # True iff the plan was found in ``RUNNING`` on disk but no runner is
    # live in the daemon; set by the status endpoint so the UI can flag it.
    stale: bool = False


# ---------------------------------------------------------------------------
# Maker
# ---------------------------------------------------------------------------


class StartMakerRequest(BaseModel):
    """POST /api/v1/wallet/{walletname}/maker/start request.

    All fields are strings per the reference implementation.
    """

    txfee: str
    cjfee_a: str
    cjfee_r: str
    ordertype: str
    minsize: str


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class ConfigGetRequest(BaseModel):
    """POST /api/v1/wallet/{walletname}/configget request."""

    section: str
    field: str


class ConfigGetResponse(BaseModel):
    """Response for configget."""

    configvalue: str


class ConfigSetRequest(BaseModel):
    """POST /api/v1/wallet/{walletname}/configset request."""

    section: str
    field: str
    value: str


# ---------------------------------------------------------------------------
# Freeze
# ---------------------------------------------------------------------------


class FreezeRequest(BaseModel):
    """POST /api/v1/wallet/{walletname}/freeze request.

    Note: the field name uses a hyphen in the reference API JSON.
    We use Field(alias=...) to handle this.
    """

    utxo_string: str = Field(alias="utxo-string")
    freeze: bool

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Sign message
# ---------------------------------------------------------------------------


class SignMessageRequest(BaseModel):
    """POST /api/v1/wallet/{walletname}/signmessage request."""

    hd_path: str
    message: str


class SignMessageResponse(BaseModel):
    """Response for signmessage."""

    signature: str
    message: str
    address: str


# ---------------------------------------------------------------------------
# Rescan
# ---------------------------------------------------------------------------


class RescanBlockchainResponse(BaseModel):
    """Response for rescanblockchain."""

    walletname: str


class RescanInfoResponse(BaseModel):
    """Response for getrescaninfo."""

    rescanning: bool
    progress: float | None = None


# ---------------------------------------------------------------------------
# Yield generator report
# ---------------------------------------------------------------------------


class YieldGenReportResponse(BaseModel):
    """Response for yieldgen/report."""

    yigen_data: list[str] = Field(default_factory=list)
