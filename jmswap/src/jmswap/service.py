from __future__ import annotations

import asyncio
import secrets
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from coincurve import PrivateKey
from pydantic import ValidationError

from jmswap.contract import SwapDirection, TaprootSwapContract, payment_hash
from jmswap.errors import ProtocolError
from jmswap.fees import forward_onchain_amount, reverse_onchain_amount
from jmswap.interfaces import ChainBackend, LightningBackend
from jmswap.models import (
    TAPROOT_SWAP_PROTOCOL,
    AddSwapInvoiceRequest,
    AddSwapInvoiceResponse,
    CooperativeFinishRequest,
    CooperativeFinishResponse,
    CooperativeStartRequest,
    CooperativeStartResponse,
    CreateNormalSwapRequest,
    CreateNormalSwapResponse,
    CreateSwapRequest,
    CreateSwapResponse,
    FundingOutput,
    SwapRecord,
    SwapTreeDto,
)
from jmswap.musig import MuSig2Session
from jmswap.persistence import SwapStore
from jmswap.transaction import (
    add_keypath_witness,
    validate_cooperative_transaction,
    validate_final_cooperative_transaction,
)

LOCKTIME_DELTA = 70
MUSIG_SESSION_TTL_SECONDS = 90
MUSIG_SESSION_MAX_ENTRIES = 32


@dataclass(frozen=True)
class ProviderConfig:
    network: Literal["mainnet", "testnet", "signet", "regtest"]
    percentage_fee: Decimal
    mining_fee: int
    minimum_amount: int = 20_000
    maximum_forward_amount: int = 10_000_000
    maximum_reverse_amount: int = 10_000_000

    def __post_init__(self) -> None:
        if not isinstance(self.percentage_fee, Decimal) or not 0 <= self.percentage_fee <= 100:
            raise ValueError("percentage fee must be a Decimal between 0 and 100")
        if type(self.mining_fee) is not int or self.mining_fee <= 0:
            raise ValueError("mining fee must be positive")
        if (
            not 546
            <= self.minimum_amount
            <= min(self.maximum_forward_amount, self.maximum_reverse_amount)
        ):
            raise ValueError("provider amount limits are inconsistent")

    def validate_amount(self, amount: int, direction: SwapDirection) -> None:
        # Electrum advertises limits from the provider's perspective, while
        # SwapDirection describes the user's side of the same swap.
        maximum = (
            self.maximum_forward_amount
            if direction is SwapDirection.REVERSE
            else self.maximum_reverse_amount
        )
        if not self.minimum_amount <= amount <= maximum:
            raise ProtocolError("swap amount is outside provider limits")


@dataclass
class _SessionEntry:
    expires: float
    direction: SwapDirection
    swap_id: str
    unsigned_transaction: str
    message: bytes
    signer: MuSig2Session
    preimage: bytes | None


class CooperativeSessionRegistry:
    """Bounded volatile sessions. Secret nonce state never reaches persistence."""

    def __init__(self) -> None:
        self._entries: dict[str, _SessionEntry] = {}
        self._lock = threading.Lock()

    def add(self, session_id: str, entry: _SessionEntry) -> None:
        with self._lock:
            now = time.monotonic()
            self._entries = {key: item for key, item in self._entries.items() if item.expires > now}
            if session_id in self._entries:
                raise ProtocolError("MuSig session ID is already active")
            if any(item.swap_id == entry.swap_id for item in self._entries.values()):
                raise ProtocolError("swap already has an active MuSig session")
            if len(self._entries) >= MUSIG_SESSION_MAX_ENTRIES:
                raise ProtocolError("MuSig session capacity reached")
            self._entries[session_id] = entry

    def consume(self, session_id: str) -> _SessionEntry:
        with self._lock:
            entry = self._entries.pop(session_id, None)
        if entry is None:
            raise ProtocolError("unknown or already used MuSig session")
        if time.monotonic() >= entry.expires:
            raise ProtocolError("MuSig session expired")
        return entry

    def __len__(self) -> int:
        return len(self._entries)


class SwapService:
    def __init__(
        self,
        *,
        provider_seckey: bytes,
        config: ProviderConfig,
        lightning: LightningBackend,
        chain: ChainBackend,
        store: SwapStore,
    ) -> None:
        if len(provider_seckey) != 32:
            raise ValueError("provider secret key must be 32 bytes")
        self._provider_seckey = provider_seckey
        self._provider_pubkey = PrivateKey(provider_seckey).public_key.format(compressed=True)
        self.config = config
        self.lightning = lightning
        self.chain = chain
        self.store = store
        self.sessions = CooperativeSessionRegistry()
        self._cooperative_lock = asyncio.Lock()

    async def dispatch(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "createnormalswap": self.createnormalswap,
            "addswapinvoice": self.addswapinvoice,
            "createswap": self.createswap,
            "claimtaprootswap": self.claimtaprootswap,
            "refundtaprootswap": self.refundtaprootswap,
        }
        handler = handlers.get(method)
        if handler is None:
            raise ProtocolError("unsupported swap method")
        try:
            return await handler(payload)
        except ValidationError as exc:
            raise ProtocolError(f"invalid {method} payload") from exc

    async def createnormalswap(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = CreateNormalSwapRequest.model_validate(payload)
        self.config.validate_amount(request.invoice_amount, SwapDirection.FORWARD)
        preimage = secrets.token_bytes(32)
        swap_hash = payment_hash(preimage)
        self._ensure_new(swap_hash.hex())
        height = await self.chain.current_height()
        locktime = height + LOCKTIME_DELTA
        user_pubkey = bytes.fromhex(request.refund_public_key)
        contract = TaprootSwapContract(
            SwapDirection.FORWARD,
            swap_hash,
            locktime,
            self._provider_pubkey,
            user_pubkey,
        )
        onchain_amount = forward_onchain_amount(
            request.invoice_amount, self.config.percentage_fee, self.config.mining_fee
        )
        record = self._record(
            contract,
            lightning_amount=request.invoice_amount,
            onchain_amount=onchain_amount,
            preimage=preimage.hex(),
        )
        self.store.save(record)
        response = CreateNormalSwapResponse(
            id=record.id,
            protocol=TAPROOT_SWAP_PROTOCOL,
            acceptZeroConf=False,
            preimageHash=record.payment_hash,
            claimPublicKey=record.provider_pubkey,
            timeoutBlockHeight=record.locktime,
            address=record.address,
            swapTree=SwapTreeDto.model_validate(contract.serialized_tree()),
            expectedAmount=record.onchain_amount,
        )
        return response.model_dump(by_alias=True)

    async def addswapinvoice(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = AddSwapInvoiceRequest.model_validate(payload)
        invoice = await self.lightning.decode_invoice(request.invoice)
        record = self._require_record(invoice.payment_hash, SwapDirection.FORWARD)
        if invoice.amount_sat != record.lightning_amount:
            raise ProtocolError("invoice amount does not match swap")
        if request.refund_public_key != record.user_pubkey:
            raise ProtocolError("refund public key does not match swap")
        if record.invoice is not None:
            raise ProtocolError("swap invoice is already registered")
        updated = record.model_copy(update={"invoice": request.invoice})
        self.store.save(updated)
        return AddSwapInvoiceResponse().model_dump(by_alias=True)

    async def createswap(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = CreateSwapRequest.model_validate(payload)
        self._ensure_new(request.preimage_hash)
        self.config.validate_amount(request.invoice_amount, SwapDirection.REVERSE)
        height = await self.chain.current_height()
        contract = TaprootSwapContract(
            SwapDirection.REVERSE,
            bytes.fromhex(request.preimage_hash),
            height + LOCKTIME_DELTA,
            self._provider_pubkey,
            bytes.fromhex(request.claim_public_key),
        )
        onchain_amount = reverse_onchain_amount(
            request.invoice_amount, self.config.percentage_fee, self.config.mining_fee
        )
        prepay_amount = self.config.mining_fee * 2
        main_amount = request.invoice_amount - prepay_amount
        if main_amount <= 0:
            raise ProtocolError("swap amount cannot cover miner fee invoice")
        main_invoice = await self.lightning.create_hold_invoice(main_amount, request.preimage_hash)
        miner_invoice, miner_details = await self.lightning.create_invoice(prepay_amount)
        if miner_details.payment_hash == request.preimage_hash:
            raise ProtocolError("miner fee invoice reuses main payment hash")
        record = self._record(
            contract,
            lightning_amount=request.invoice_amount,
            onchain_amount=onchain_amount,
            invoice=main_invoice,
            miner_fee_invoice=miner_invoice,
            miner_fee_hash=miner_details.payment_hash,
        )
        self.store.save(record)
        response = CreateSwapResponse(
            id=record.id,
            protocol=TAPROOT_SWAP_PROTOCOL,
            acceptZeroConf=False,
            invoice=main_invoice,
            minerFeeInvoice=miner_invoice,
            refundPublicKey=record.provider_pubkey,
            lockupAddress=record.address,
            swapTree=SwapTreeDto.model_validate(contract.serialized_tree()),
            timeoutBlockHeight=record.locktime,
            onchainAmount=record.onchain_amount,
        )
        return response.model_dump(by_alias=True)

    async def claimtaprootswap(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cooperative(payload, SwapDirection.REVERSE)

    async def refundtaprootswap(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._cooperative(payload, SwapDirection.FORWARD)

    async def register_funding(self, swap_id: str, txid: str, vout: int) -> SwapRecord:
        record = self._require_record(swap_id)
        funding = await self.chain.funding_output(txid, vout)
        if funding is None:
            raise ProtocolError("funding output is unknown")
        self._validate_funding(record, funding, require_confirmed=False)
        if record.funding is not None and (
            record.funding.txid != funding.txid or record.funding.vout != funding.vout
        ):
            raise ProtocolError("swap funding outpoint is already registered")
        updated = record.model_copy(update={"funding": funding})
        self.store.save(updated)
        return updated

    async def _cooperative(
        self, payload: dict[str, Any], direction: SwapDirection
    ) -> dict[str, Any]:
        async with self._cooperative_lock:
            if {"pubNonce", "partialSignature"} & set(payload):
                return await self._finish_cooperative(payload, direction)
            return await self._start_cooperative(payload, direction)

    async def _start_cooperative(
        self, payload: dict[str, Any], direction: SwapDirection
    ) -> dict[str, Any]:
        expected = {"id", "sessionId", "transaction"}
        if direction is SwapDirection.REVERSE:
            expected.add("preimage")
        if set(payload) != expected:
            raise ProtocolError("invalid cooperative spend request fields")
        request = CooperativeStartRequest.model_validate(payload)
        record = self._require_record(request.id, direction)
        funding = await self._confirmed_funding(record)
        preimage = bytes.fromhex(request.preimage) if request.preimage is not None else None
        await self._check_cooperative_eligible(record, direction, preimage)
        checked = validate_cooperative_transaction(
            request.transaction, funding=funding, network=record.network
        )
        signer = MuSig2Session(
            contract=record.contract,
            local_seckey=self._provider_seckey,
            msg32=checked.message,
            session_id32=bytes.fromhex(request.session_id),
        )
        self.sessions.add(
            request.session_id,
            _SessionEntry(
                time.monotonic() + MUSIG_SESSION_TTL_SECONDS,
                direction,
                record.id,
                checked.raw_unsigned,
                checked.message,
                signer,
                preimage,
            ),
        )
        return CooperativeStartResponse(
            sessionId=request.session_id,
            pubNonce=signer.public_nonce.hex(),
            transaction=checked.raw_unsigned,
        ).model_dump(by_alias=True)

    async def _finish_cooperative(
        self, payload: dict[str, Any], direction: SwapDirection
    ) -> dict[str, Any]:
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str):
            raise ProtocolError("invalid MuSig session ID")
        entry = self.sessions.consume(session_id)
        if set(payload) != {"id", "sessionId", "pubNonce", "partialSignature"}:
            raise ProtocolError("invalid cooperative signing request fields")
        request = CooperativeFinishRequest.model_validate(payload)
        if entry.direction is not direction or entry.swap_id != request.id:
            raise ProtocolError("MuSig session does not match swap")
        record = self._require_record(request.id, direction)
        funding = await self._confirmed_funding(record)
        await self._check_cooperative_eligible(record, direction, entry.preimage)
        checked = validate_cooperative_transaction(
            entry.unsigned_transaction, funding=funding, network=record.network
        )
        if checked.message != entry.message:
            raise ProtocolError("cooperative transaction identity changed while signing")
        provider_partial = entry.signer.sign_partial(bytes.fromhex(request.pub_nonce))
        signature = entry.signer.aggregate(bytes.fromhex(request.partial_signature))
        raw_signed = add_keypath_witness(entry.unsigned_transaction, signature)
        txid = validate_final_cooperative_transaction(
            raw_signed,
            raw_unsigned=entry.unsigned_transaction,
            contract=record.contract,
            message=entry.message,
            expected_signature=signature,
        )
        update: dict[str, Any] = {
            "cooperative_transaction": raw_signed,
            "spending_txid": txid,
        }
        if entry.preimage is not None:
            update["preimage"] = entry.preimage.hex()
        self.store.save(record.model_copy(update=update))
        return CooperativeFinishResponse(
            sessionId=request.session_id,
            partialSignature=provider_partial.hex(),
            transaction=raw_signed,
        ).model_dump(by_alias=True)

    async def _confirmed_funding(self, record: SwapRecord) -> FundingOutput:
        if record.funding is None:
            raise ProtocolError("swap has no registered funding output")
        current = await self.chain.funding_output(record.funding.txid, record.funding.vout)
        if current is None:
            raise ProtocolError("funding output is unavailable")
        self._validate_funding(record, current, require_confirmed=True)
        return current

    @staticmethod
    def _validate_funding(
        record: SwapRecord, funding: FundingOutput, *, require_confirmed: bool
    ) -> None:
        if funding.value != record.onchain_amount or funding.script_pubkey != record.output_script:
            raise ProtocolError("funding output does not match swap")
        if require_confirmed and funding.confirmations < 1:
            raise ProtocolError("funding output is not confirmed")

    async def _check_cooperative_eligible(
        self,
        record: SwapRecord,
        direction: SwapDirection,
        preimage: bytes | None,
    ) -> None:
        if record.cooperative_transaction is not None or record.spending_txid is not None:
            raise ProtocolError("funding output already has a spender")
        if direction is SwapDirection.REVERSE:
            if preimage is None or payment_hash(preimage).hex() != record.payment_hash:
                raise ProtocolError("cooperative claim preimage does not match swap")
            return
        if await self.chain.current_height() < record.locktime:
            raise ProtocolError("cooperative refund timeout has not been reached")
        if await self.lightning.payment_in_flight_or_paid(record.payment_hash):
            raise ProtocolError("swap Lightning payment is paid or in flight")

    def _record(
        self,
        contract: TaprootSwapContract,
        *,
        lightning_amount: int,
        onchain_amount: int,
        preimage: str | None = None,
        invoice: str | None = None,
        miner_fee_invoice: str | None = None,
        miner_fee_hash: str | None = None,
    ) -> SwapRecord:
        return SwapRecord(
            id=contract.payment_hash.hex(),
            direction=contract.direction,
            payment_hash=contract.payment_hash.hex(),
            locktime=contract.locktime,
            provider_pubkey=contract.provider_pubkey.hex(),
            user_pubkey=contract.user_pubkey.hex(),
            local_seckey=self._provider_seckey.hex(),
            network=self.config.network,
            address=contract.address(self.config.network),
            output_script=contract.output_script.hex(),
            lightning_amount=lightning_amount,
            onchain_amount=onchain_amount,
            preimage=preimage,
            invoice=invoice,
            miner_fee_invoice=miner_fee_invoice,
            miner_fee_hash=miner_fee_hash,
        )

    def _require_record(self, swap_id: str, direction: SwapDirection | None = None) -> SwapRecord:
        record = self.store.load(swap_id)
        if record is None or (direction is not None and record.direction is not direction):
            raise ProtocolError("unknown provider Taproot swap")
        if record.provider_pubkey != self._provider_pubkey.hex():
            raise ProtocolError("swap is not owned by this provider")
        return record

    def _ensure_new(self, swap_id: str) -> None:
        if self.store.load(swap_id) is not None:
            raise ProtocolError("payment hash is already in use")
