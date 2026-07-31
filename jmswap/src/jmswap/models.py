from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from coincurve import PublicKey
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from jmswap.contract import SwapDirection, TaprootSwapContract

TAPROOT_SWAP_PROTOCOL: Literal["taproot-v1"] = "taproot-v1"
TAPROOT_COOPERATIVE_CAPABILITY: Literal["taproot-cooperative-v1"] = "taproot-cooperative-v1"

Hex32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Hex33 = Annotated[str, StringConstraints(pattern=r"^(02|03)[0-9a-f]{64}$")]
Hex66 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{132}$")]
RawTransactionHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]+$", max_length=200_000)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)


class TapLeafDto(WireModel):
    version: Literal[192]
    output: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]+$", max_length=20_000)]


class SwapTreeDto(WireModel):
    claim_leaf: TapLeafDto = Field(alias="claimLeaf")
    refund_leaf: TapLeafDto = Field(alias="refundLeaf")


class CreateNormalSwapRequest(WireModel):
    invoice_amount: Annotated[int, Field(gt=0)] = Field(alias="invoiceAmount")
    refund_public_key: Hex33 = Field(alias="refundPublicKey")
    protocol: Literal["taproot-v1"]

    @model_validator(mode="after")
    def validate_key(self) -> Self:
        PublicKey(bytes.fromhex(self.refund_public_key))
        return self


class CreateNormalSwapResponse(WireModel):
    id: Hex32
    protocol: Literal["taproot-v1"]
    accept_zero_conf: Literal[False] = Field(alias="acceptZeroConf")
    preimage_hash: Hex32 = Field(alias="preimageHash")
    claim_public_key: Hex33 = Field(alias="claimPublicKey")
    timeout_block_height: Annotated[int, Field(gt=0, le=500_000_000)] = Field(
        alias="timeoutBlockHeight"
    )
    address: str
    swap_tree: SwapTreeDto = Field(alias="swapTree")
    expected_amount: Annotated[int, Field(ge=546)] = Field(alias="expectedAmount")

    @model_validator(mode="after")
    def validate_id(self) -> Self:
        if self.id != self.preimage_hash:
            raise ValueError("swap ID must equal the payment hash")
        return self


class AddSwapInvoiceRequest(WireModel):
    invoice: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]
    refund_public_key: Hex33 = Field(alias="refundPublicKey")


class AddSwapInvoiceResponse(WireModel):
    pass


class CreateSwapRequest(WireModel):
    type: Literal["reversesubmarine"]
    pair_id: Literal["BTC/BTC"] = Field(alias="pairId")
    invoice_amount: Annotated[int, Field(gt=0)] = Field(alias="invoiceAmount")
    preimage_hash: Hex32 = Field(alias="preimageHash")
    claim_public_key: Hex33 = Field(alias="claimPublicKey")
    protocol: Literal["taproot-v1"]

    @model_validator(mode="after")
    def validate_key(self) -> Self:
        PublicKey(bytes.fromhex(self.claim_public_key))
        return self


class CreateSwapResponse(WireModel):
    id: Hex32
    protocol: Literal["taproot-v1"]
    accept_zero_conf: Literal[False] = Field(alias="acceptZeroConf")
    invoice: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]
    miner_fee_invoice: Annotated[str, StringConstraints(min_length=1, max_length=20_000)] = Field(
        alias="minerFeeInvoice"
    )
    refund_public_key: Hex33 = Field(alias="refundPublicKey")
    lockup_address: str = Field(alias="lockupAddress")
    swap_tree: SwapTreeDto = Field(alias="swapTree")
    timeout_block_height: Annotated[int, Field(gt=0, le=500_000_000)] = Field(
        alias="timeoutBlockHeight"
    )
    onchain_amount: Annotated[int, Field(ge=546)] = Field(alias="onchainAmount")


class CooperativeStartRequest(WireModel):
    id: Hex32
    session_id: Hex32 = Field(alias="sessionId")
    transaction: RawTransactionHex
    preimage: Hex32 | None = None


class CooperativeStartResponse(WireModel):
    session_id: Hex32 = Field(alias="sessionId")
    pub_nonce: Hex66 = Field(alias="pubNonce")
    transaction: RawTransactionHex


class CooperativeFinishRequest(WireModel):
    id: Hex32
    session_id: Hex32 = Field(alias="sessionId")
    pub_nonce: Hex66 = Field(alias="pubNonce")
    partial_signature: Hex32 = Field(alias="partialSignature")


class CooperativeFinishResponse(WireModel):
    session_id: Hex32 = Field(alias="sessionId")
    partial_signature: Hex32 = Field(alias="partialSignature")
    transaction: RawTransactionHex


class SwapOffer(WireModel):
    percentage_fee: Decimal = Field(ge=0, le=100)
    mining_fee: Annotated[int, Field(gt=0)]
    min_amount: Annotated[int, Field(ge=546)]
    max_forward_amount: Annotated[int, Field(gt=0)]
    max_reverse_amount: Annotated[int, Field(gt=0)]
    relays: str
    protocols: tuple[Literal["taproot-v1", "taproot-cooperative-v1"], ...]
    pow_nonce: str = "0x0"


class InvoiceDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    payment_hash: Hex32
    amount_sat: Annotated[int, Field(gt=0)]


class FundingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    txid: Hex32
    vout: Annotated[int, Field(ge=0, le=0xFFFFFFFF)]
    value: Annotated[int, Field(gt=0)]
    script_pubkey: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]+$")]
    confirmations: Annotated[int, Field(ge=0)] = 0


class SwapRecord(BaseModel):
    """Durable state. Secret nonces are deliberately not representable."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: Hex32
    direction: SwapDirection
    payment_hash: Hex32
    locktime: Annotated[int, Field(gt=0, le=500_000_000)]
    provider_pubkey: Hex33
    user_pubkey: Hex33
    local_seckey: Hex32
    network: Literal["mainnet", "testnet", "signet", "regtest"]
    address: str
    output_script: str
    lightning_amount: Annotated[int, Field(gt=0)]
    onchain_amount: Annotated[int, Field(ge=546)]
    preimage: Hex32 | None = None
    invoice: str | None = None
    miner_fee_invoice: str | None = None
    miner_fee_hash: Hex32 | None = None
    funding: FundingOutput | None = None
    cooperative_transaction: RawTransactionHex | None = None
    spending_txid: Hex32 | None = None

    @property
    def contract(self) -> TaprootSwapContract:
        return TaprootSwapContract(
            direction=self.direction,
            payment_hash=bytes.fromhex(self.payment_hash),
            locktime=self.locktime,
            provider_pubkey=bytes.fromhex(self.provider_pubkey),
            user_pubkey=bytes.fromhex(self.user_pubkey),
        )

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        if self.id != self.payment_hash:
            raise ValueError("swap ID must equal payment hash")
        contract = self.contract
        if contract.address(self.network) != self.address:
            raise ValueError("persisted address does not match contract")
        if contract.output_script.hex() != self.output_script:
            raise ValueError("persisted output script does not match contract")
        local_pubkey = PublicKey.from_secret(bytes.fromhex(self.local_seckey)).format(
            compressed=True
        )
        if local_pubkey.hex() not in (self.provider_pubkey, self.user_pubkey):
            raise ValueError("persisted secret key does not belong to contract")
        if self.preimage is not None:
            from jmcore.bitcoin import sha256

            if sha256(bytes.fromhex(self.preimage)).hex() != self.payment_hash:
                raise ValueError("persisted preimage does not match payment hash")
        if self.funding is not None:
            if self.funding.script_pubkey != self.output_script:
                raise ValueError("funding output does not match contract")
            if self.funding.value != self.onchain_amount:
                raise ValueError("funding value does not match swap")
        return self
