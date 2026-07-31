from __future__ import annotations

import secrets
from typing import Any, Literal

from coincurve import PrivateKey

from jmswap.contract import SwapDirection, TaprootSwapContract, payment_hash
from jmswap.errors import ProtocolError
from jmswap.interfaces import LightningBackend, RpcAdapter
from jmswap.models import (
    TAPROOT_SWAP_PROTOCOL,
    AddSwapInvoiceRequest,
    CooperativeFinishResponse,
    CooperativeStartResponse,
    CreateNormalSwapRequest,
    CreateNormalSwapResponse,
    CreateSwapRequest,
    CreateSwapResponse,
    FundingOutput,
    SwapRecord,
)
from jmswap.musig import MuSig2Session
from jmswap.persistence import SwapStore
from jmswap.transaction import (
    add_keypath_witness,
    build_unsigned_cooperative_transaction,
    validate_cooperative_transaction,
    validate_final_cooperative_transaction,
)

MIN_LOCKTIME_DELTA = 60
MAX_LOCKTIME_DELTA = 100


class SwapClient:
    def __init__(
        self,
        adapter: RpcAdapter,
        *,
        network: Literal["mainnet", "testnet", "signet", "regtest"],
        lightning: LightningBackend | None = None,
        store: SwapStore | None = None,
    ) -> None:
        self.adapter = adapter
        self.network = network
        self.lightning = lightning
        self.store = store

    async def createnormalswap(
        self,
        *,
        lightning_amount: int,
        expected_onchain_amount: int,
        current_height: int,
        user_seckey: bytes | None = None,
    ) -> SwapRecord:
        seckey = user_seckey or secrets.token_bytes(32)
        user_pubkey = PrivateKey(seckey).public_key.format(compressed=True)
        request = CreateNormalSwapRequest(
            invoiceAmount=lightning_amount,
            refundPublicKey=user_pubkey.hex(),
            protocol=TAPROOT_SWAP_PROTOCOL,
        )
        raw = await self.adapter.request("createnormalswap", request.model_dump(by_alias=True))
        response = CreateNormalSwapResponse.model_validate(raw)
        contract = TaprootSwapContract(
            SwapDirection.FORWARD,
            bytes.fromhex(response.preimage_hash),
            response.timeout_block_height,
            bytes.fromhex(response.claim_public_key),
            user_pubkey,
        )
        contract.validate_provider_data(
            serialized_tree=response.swap_tree.model_dump(by_alias=True),
            address=response.address,
            network=self.network,
        )
        self._validate_semantics(
            response.expected_amount,
            expected_onchain_amount,
            response.timeout_block_height,
            current_height,
            forward=True,
        )
        record = self._record(
            contract,
            seckey,
            lightning_amount=lightning_amount,
            onchain_amount=response.expected_amount,
        )
        self._save(record)
        return record

    async def addswapinvoice(self, record: SwapRecord, invoice: str) -> None:
        if record.direction is not SwapDirection.FORWARD:
            raise ProtocolError("only forward swaps accept an invoice")
        request = AddSwapInvoiceRequest(invoice=invoice, refundPublicKey=record.user_pubkey)
        response = await self.adapter.request("addswapinvoice", request.model_dump(by_alias=True))
        if response != {}:
            raise ProtocolError("invalid addswapinvoice response")
        self._save(record.model_copy(update={"invoice": invoice}))

    async def createswap(
        self,
        *,
        lightning_amount: int,
        expected_onchain_amount: int,
        maximum_miner_fee: int,
        current_height: int,
        user_seckey: bytes | None = None,
        preimage: bytes | None = None,
    ) -> SwapRecord:
        seckey = user_seckey or secrets.token_bytes(32)
        swap_preimage = preimage or secrets.token_bytes(32)
        swap_hash = payment_hash(swap_preimage)
        user_pubkey = PrivateKey(seckey).public_key.format(compressed=True)
        request = CreateSwapRequest(
            type="reversesubmarine",
            pairId="BTC/BTC",
            invoiceAmount=lightning_amount,
            preimageHash=swap_hash.hex(),
            claimPublicKey=user_pubkey.hex(),
            protocol=TAPROOT_SWAP_PROTOCOL,
        )
        raw = await self.adapter.request("createswap", request.model_dump(by_alias=True))
        response = CreateSwapResponse.model_validate(raw)
        if response.id != swap_hash.hex():
            raise ProtocolError("swap ID does not match payment hash")
        contract = TaprootSwapContract(
            SwapDirection.REVERSE,
            swap_hash,
            response.timeout_block_height,
            bytes.fromhex(response.refund_public_key),
            user_pubkey,
        )
        contract.validate_provider_data(
            serialized_tree=response.swap_tree.model_dump(by_alias=True),
            address=response.lockup_address,
            network=self.network,
        )
        self._validate_semantics(
            response.onchain_amount,
            expected_onchain_amount,
            response.timeout_block_height,
            current_height,
            forward=False,
        )
        miner_hash: str | None = None
        if self.lightning is not None:
            main = await self.lightning.decode_invoice(response.invoice)
            miner = await self.lightning.decode_invoice(response.miner_fee_invoice)
            if main.payment_hash != swap_hash.hex() or miner.payment_hash == swap_hash.hex():
                raise ProtocolError("reverse swap invoice payment hashes are invalid")
            if miner.amount_sat > maximum_miner_fee:
                raise ProtocolError("miner fee invoice exceeds advertised fee")
            if main.amount_sat + miner.amount_sat != lightning_amount:
                raise ProtocolError("reverse swap invoice amounts do not match request")
            miner_hash = miner.payment_hash
        record = self._record(
            contract,
            seckey,
            lightning_amount=lightning_amount,
            onchain_amount=response.onchain_amount,
            preimage=swap_preimage.hex(),
            invoice=response.invoice,
            miner_fee_invoice=response.miner_fee_invoice,
            miner_fee_hash=miner_hash,
        )
        self._save(record)
        return record

    async def claimtaprootswap(
        self,
        record: SwapRecord,
        funding: FundingOutput,
        *,
        destination_script: bytes,
        output_value: int,
        locktime: int = 0,
    ) -> str:
        if record.direction is not SwapDirection.REVERSE or record.preimage is None:
            raise ProtocolError("claim requires a reverse swap preimage")
        return await self._cooperative(
            "claimtaprootswap",
            record,
            funding,
            destination_script=destination_script,
            output_value=output_value,
            locktime=locktime,
            preimage=record.preimage,
        )

    async def refundtaprootswap(
        self,
        record: SwapRecord,
        funding: FundingOutput,
        *,
        destination_script: bytes,
        output_value: int,
        current_height: int,
        locktime: int = 0,
    ) -> str:
        if record.direction is not SwapDirection.FORWARD or current_height < record.locktime:
            raise ProtocolError("refund requires an expired forward swap")
        return await self._cooperative(
            "refundtaprootswap",
            record,
            funding,
            destination_script=destination_script,
            output_value=output_value,
            locktime=locktime,
        )

    async def _cooperative(
        self,
        method: str,
        record: SwapRecord,
        funding: FundingOutput,
        *,
        destination_script: bytes,
        output_value: int,
        locktime: int,
        preimage: str | None = None,
    ) -> str:
        if (
            funding.value != record.onchain_amount
            or funding.script_pubkey != record.output_script
            or funding.confirmations < 1
        ):
            raise ProtocolError("cooperative funding output does not match the confirmed swap")
        if record.funding is not None and (
            funding.txid != record.funding.txid or funding.vout != record.funding.vout
        ):
            raise ProtocolError("cooperative funding outpoint changed after registration")
        unsigned = build_unsigned_cooperative_transaction(
            funding, destination_script=destination_script, value=output_value, locktime=locktime
        )
        checked = validate_cooperative_transaction(
            unsigned,
            funding=funding,
            network=record.network,
            expected_value=output_value,
            max_locktime=locktime,
        )
        session_id = secrets.token_bytes(32)
        signer = MuSig2Session(
            contract=record.contract,
            local_seckey=bytes.fromhex(record.local_seckey),
            msg32=checked.message,
            session_id32=session_id,
        )
        first: dict[str, Any] = {
            "id": record.id,
            "sessionId": session_id.hex(),
            "transaction": unsigned,
        }
        if preimage is not None:
            first["preimage"] = preimage
        nonce_response = CooperativeStartResponse.model_validate(
            await self.adapter.request(method, first)
        )
        if nonce_response.session_id != session_id.hex() or nonce_response.transaction != unsigned:
            raise ProtocolError("cooperative nonce response changed transaction identity")
        local_partial = signer.sign_partial(bytes.fromhex(nonce_response.pub_nonce))
        final_response = CooperativeFinishResponse.model_validate(
            await self.adapter.request(
                method,
                {
                    "id": record.id,
                    "sessionId": session_id.hex(),
                    "pubNonce": signer.public_nonce.hex(),
                    "partialSignature": local_partial.hex(),
                },
            )
        )
        provider_partial = bytes.fromhex(final_response.partial_signature)
        signature = signer.aggregate(provider_partial)
        expected = add_keypath_witness(unsigned, signature)
        if final_response.transaction != expected:
            raise ProtocolError("cooperative final transaction identity changed")
        txid = validate_final_cooperative_transaction(
            final_response.transaction,
            raw_unsigned=unsigned,
            contract=record.contract,
            message=checked.message,
            expected_signature=signature,
        )
        self._save(
            record.model_copy(
                update={
                    "funding": funding,
                    "cooperative_transaction": final_response.transaction,
                    "spending_txid": txid,
                }
            )
        )
        return final_response.transaction

    def _record(
        self,
        contract: TaprootSwapContract,
        seckey: bytes,
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
            local_seckey=seckey.hex(),
            network=self.network,
            address=contract.address(self.network),
            output_script=contract.output_script.hex(),
            lightning_amount=lightning_amount,
            onchain_amount=onchain_amount,
            preimage=preimage,
            invoice=invoice,
            miner_fee_invoice=miner_fee_invoice,
            miner_fee_hash=miner_fee_hash,
        )

    @staticmethod
    def _validate_semantics(
        actual_amount: int,
        expected_amount: int,
        locktime: int,
        current_height: int,
        *,
        forward: bool,
    ) -> None:
        if (forward and actual_amount > expected_amount) or (
            not forward and actual_amount < expected_amount
        ):
            raise ProtocolError("provider returned an unacceptable on-chain amount")
        delta = locktime - current_height
        if not MIN_LOCKTIME_DELTA < delta <= MAX_LOCKTIME_DELTA:
            raise ProtocolError("provider returned an unsafe locktime")

    def _save(self, record: SwapRecord) -> None:
        if self.store is not None:
            self.store.save(record)
