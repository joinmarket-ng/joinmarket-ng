"""
Swap provider (server): the maker side of a bidirectional Lightning swap.

The provider advertises terms over Nostr, answers ``getterms`` / ``createswap``
/ ``swapstatus`` RPCs, and drives each swap to completion against its Lightning
and chain backends:

* **swap-in** (provider receives on-chain, pays Lightning): wait for the client
  to fund the HTLC, pay the client's invoice (learning the preimage on success),
  then claim the on-chain HTLC.
* **swap-out** (provider pays on-chain, receives Lightning): issue a hold
  invoice, wait for the client to pay it, fund the on-chain HTLC to the client's
  claim key, watch for the client's claim (which reveals the preimage), then
  settle the hold invoice.

The provider never pays a swap fee; it earns one on every swap.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from jmswap.chain import ChainBackend
from jmswap.errors import (
    InsufficientLiquidityError,
    SwapProtocolError,
    SwapVerificationError,
)
from jmswap.lightning import InboundStatus, LightningBackend, PaymentStatus
from jmswap.models import (
    ProviderTerms,
    SwapContract,
    SwapDirection,
    SwapRequest,
    SwapResponse,
    SwapState,
)
from jmswap.persistence import SwapStore
from jmswap.recovery import is_refundable, refund
from jmswap.spend import (
    SwapOutpoint,
    build_claim_tx,
    estimate_fee,
    extract_preimage_from_claim,
)
from jmswap.swap_tree import SwapTree
from jmswap.wallet import SwapWallet

# Background polling cadence for confirmations / inbound payments.
POLL_INTERVAL = 0.05
MAX_POLLS = 2000


class SwapServer:
    """A bidirectional swap provider bound to Lightning, chain and wallet backends."""

    def __init__(
        self,
        terms: ProviderTerms,
        lightning: LightningBackend,
        chain: ChainBackend,
        wallet: SwapWallet,
        *,
        feerate_sat_vb: float = 2.0,
        poll_interval: float = POLL_INTERVAL,
        store: SwapStore | None = None,
        min_confirmations: int = 1,
    ) -> None:
        self.terms = terms
        self._ln = lightning
        self._chain = chain
        self._wallet = wallet
        self._feerate = feerate_sat_vb
        self._poll = poll_interval
        self._store = store
        self._min_conf = max(min_confirmations, 1)
        self.contracts: dict[str, SwapContract] = {}
        self._trees: dict[str, SwapTree] = {}
        self._privkeys: dict[str, bytes] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._counter = 0

    def _persist(self, contract: SwapContract) -> None:
        if self._store is not None:
            self._store.save(contract)

    # -- Nostr RPC dispatch ---------------------------------------------------

    async def rpc_handler(self, method: str, params: dict[str, Any], sender: str) -> dict[str, Any]:
        """Dispatch a swap RPC. Plug into :meth:`SwapNostrNode.serve`."""
        if method == "getterms":
            return self.terms.model_dump(mode="json")
        if method == "createswap":
            return await self.create_swap(SwapRequest.model_validate(params))
        if method == "swapstatus":
            swap_id = params["swap_id"]
            contract = self.contracts.get(swap_id)
            if contract is None:
                raise SwapProtocolError("unknown swap_id")
            return {"swap_id": swap_id, "state": contract.state.value}
        if method == "coopclaim":
            return await self._cooperative_claim(params)
        raise SwapProtocolError(f"unknown method {method!r}")

    async def _cooperative_claim(self, params: dict[str, Any]) -> dict[str, Any]:
        """Co-sign a swap-out cooperative (key-path) claim and settle the invoice.

        The client reveals the preimage here (so the provider settles its hold
        invoice off-chain), then both co-sign the key-path spend; nothing is
        revealed on-chain. Falls back to the script path if the client never
        calls this.
        """
        from jmcore.bitcoin import sha256

        from jmswap.cooperative import server_cosign_claim

        swap_id = params["swap_id"]
        contract = self.contracts.get(swap_id)
        tree = self._trees.get(swap_id)
        server_priv = self._privkeys.get(swap_id)
        if contract is None or tree is None or server_priv is None:
            raise SwapProtocolError("unknown swap_id")
        if contract.direction != SwapDirection.SWAP_OUT:
            raise SwapProtocolError("cooperative claim is only for swap-out")
        if contract.lockup_txid is None or contract.lockup_vout is None:
            raise SwapProtocolError("swap funding not yet on-chain")

        preimage = bytes.fromhex(params["preimage"])
        if sha256(preimage).hex() != contract.preimage_hash:
            raise SwapVerificationError("preimage does not match the swap")

        # Settle the hold invoice off-chain with the revealed preimage.
        if contract.preimage is None:
            await self._ln.settle_hold_invoice(contract.preimage_hash, params["preimage"])
            contract.preimage = params["preimage"]
        outpoint = SwapOutpoint(
            txid=contract.lockup_txid,
            vout=contract.lockup_vout,
            value=contract.onchain_amount,
            scriptpubkey=tree.funding_output_script(),
        )
        server_pubnonce, server_partial = server_cosign_claim(
            tree,
            outpoint,
            bytes.fromhex(params["destination_spk"]),
            int(params["fee"]),
            server_priv,
            bytes.fromhex(params["client_pubnonce"]),
        )
        if not contract.state.is_terminal:
            contract.advance(SwapState.CLAIMED)
        self._persist(contract)
        return {
            "server_pubnonce": server_pubnonce.hex(),
            "server_partial": server_partial.hex(),
        }

    # -- Swap creation --------------------------------------------------------

    async def create_swap(self, request: SwapRequest, *, autostart: bool = True) -> dict[str, Any]:
        """Validate and open a swap, returning the :class:`SwapResponse` payload."""
        if not self.terms.supports(request.direction):
            raise SwapProtocolError(f"direction {request.direction} not offered")
        if request.whole_coin and not self.terms.accepts_whole_coin:
            raise SwapProtocolError("provider does not accept whole-coin swaps")
        if not self.terms.within_limits(request.amount):
            raise SwapProtocolError("amount outside provider limits")

        fee = self.terms.base_fee if request.whole_coin else self.terms.fee_for(request.amount)
        height = await self._chain.block_height()
        locktime = height + self.terms.cltv_delta
        server_priv, server_pub = await self._wallet.new_swap_keypair()

        tree = SwapTree.build(
            is_reverse=request.direction.is_reverse,
            preimage_hash=bytes.fromhex(request.preimage_hash),
            server_pubkey=server_pub,
            client_pubkey=bytes.fromhex(request.client_pubkey),
            locktime=locktime,
        )
        funding_address = tree.funding_address(self.terms.network)

        self._counter += 1
        swap_id = f"swap-{self._counter:08d}-{request.preimage_hash[:8]}"

        invoice: str | None = None
        if request.direction == SwapDirection.SWAP_IN:
            # The client funds amount+fee on-chain; the provider pays `amount` on LN.
            if request.invoice is None:
                raise SwapProtocolError("swap-in requires the client's invoice")
            onchain_amount = request.amount + fee
            invoice_in = request.invoice
            decoded = await self._ln.decode_invoice(invoice_in)
            if decoded.payment_hash != request.preimage_hash:
                raise SwapVerificationError("invoice payment hash does not match preimage hash")
            contract = self._make_contract(
                swap_id, request, server_pub, locktime, onchain_amount, funding_address
            )
            contract.invoice = invoice_in
        else:  # SWAP_OUT
            # The provider funds `amount` on-chain; the client pays amount+fee on LN.
            onchain_amount = request.amount
            invoice = await self._ln.create_hold_invoice(
                (request.amount + fee) * 1000, request.preimage_hash
            )
            contract = self._make_contract(
                swap_id, request, server_pub, locktime, onchain_amount, funding_address
            )
            contract.invoice = invoice

        self.contracts[swap_id] = contract
        self._trees[swap_id] = tree
        self._privkeys[swap_id] = server_priv
        self._persist(contract)

        if autostart:
            self._tasks[swap_id] = asyncio.create_task(self.run_swap(swap_id))

        response = SwapResponse(
            swap_id=swap_id,
            direction=request.direction,
            server_pubkey=server_pub.hex(),
            funding_address=funding_address,
            onchain_amount=onchain_amount,
            locktime=locktime,
            swap_tree=tree.serialize(),
            invoice=invoice,
        )
        return response.model_dump(mode="json")

    def _make_contract(
        self,
        swap_id: str,
        request: SwapRequest,
        server_pub: bytes,
        locktime: int,
        onchain_amount: int,
        funding_address: str,
    ) -> SwapContract:
        return SwapContract(
            swap_id=swap_id,
            direction=request.direction,
            network=self.terms.network,
            server_pubkey=server_pub.hex(),
            client_pubkey=request.client_pubkey,
            preimage_hash=request.preimage_hash,
            locktime=locktime,
            onchain_amount=onchain_amount,
            funding_address=funding_address,
        )

    # -- Swap execution -------------------------------------------------------

    async def run_swap(self, swap_id: str) -> None:
        """Drive a swap to completion (called as a background task)."""
        contract = self.contracts[swap_id]
        try:
            if contract.direction == SwapDirection.SWAP_IN:
                await self._run_swap_in(swap_id)
            else:
                await self._run_swap_out(swap_id)
        except Exception as exc:  # noqa: BLE001 - record and surface the failure
            logger.warning("swap {} failed: {}", swap_id, exc)
            if not contract.state.is_terminal:
                contract.advance(SwapState.FAILED)
        finally:
            self._persist(contract)

    async def _run_swap_in(self, swap_id: str) -> None:
        contract = self.contracts[swap_id]
        tree = self._trees[swap_id]
        contract.advance(SwapState.AWAITING_LOCKUP)
        outpoint = await self._await_funding(contract)
        contract.lockup_txid, contract.lockup_vout = outpoint.txid, outpoint.vout
        contract.advance(SwapState.LOCKUP_SEEN)
        contract.advance(SwapState.LOCKUP_CONFIRMED)

        # Pay the client's invoice; success reveals the preimage.
        if contract.invoice is None:
            raise SwapProtocolError("missing client invoice")
        result = await self._ln.pay_invoice(contract.invoice)
        if result.status != PaymentStatus.SUCCEEDED or result.preimage is None:
            raise InsufficientLiquidityError("could not pay client's invoice; client may refund")
        contract.preimage = result.preimage
        contract.advance(SwapState.INVOICE_PAID)

        dest = await self._wallet.destination_scriptpubkey()
        fee = estimate_fee(150, self._feerate)
        claim_tx = build_claim_tx(
            tree, outpoint, bytes.fromhex(result.preimage), self._privkeys[swap_id], dest, fee
        )
        contract.spend_txid = await self._chain.broadcast(claim_tx.serialize().hex())
        contract.advance(SwapState.CLAIMED)
        self._persist(contract)
        logger.info("swap-in {} claimed on-chain", swap_id)

    async def _run_swap_out(self, swap_id: str) -> None:
        contract = self.contracts[swap_id]
        contract.advance(SwapState.AWAITING_LOCKUP)

        # Wait for the client to pay (hold) the invoice before risking on-chain funds.
        await self._await_inbound_held(contract)

        # Fund the on-chain HTLC to the client's claim key.
        outpoint = await self._wallet.fund_swap(contract.funding_address, contract.onchain_amount)
        contract.lockup_txid, contract.lockup_vout = outpoint.txid, outpoint.vout
        contract.advance(SwapState.LOCKUP_SEEN)
        contract.advance(SwapState.LOCKUP_CONFIRMED)

        # The client either claims cooperatively (key-path; it reveals the
        # preimage via the coopclaim RPC, which settles the invoice and advances
        # to CLAIMED), or falls back to a script-path on-chain claim that we
        # watch for here and settle from.
        cooperative = await self._await_claim_preimage(outpoint, contract)
        if cooperative is None:
            return  # the cooperative RPC already settled and advanced the swap
        contract.preimage = cooperative.hex()
        contract.advance(SwapState.CLAIMED)
        await self._ln.settle_hold_invoice(contract.preimage_hash, cooperative.hex())
        self._persist(contract)
        logger.info("swap-out {} settled on Lightning (script-path claim)", swap_id)

    async def refund_expired(self) -> list[str]:
        """Refund any swap-out funding outputs whose timelock has expired.

        The provider funds the HTLC on a swap-out; if the client never claims it,
        the provider reclaims via the refund leaf once ``locktime`` is reached.
        Returns the list of refund txids broadcast.
        """
        height = await self._chain.block_height()
        refunded: list[str] = []
        for swap_id, contract in self.contracts.items():
            if contract.direction != SwapDirection.SWAP_OUT:
                continue
            if not is_refundable(contract, height):
                continue
            tree = self._trees.get(swap_id)
            privkey = self._privkeys.get(swap_id)
            if tree is None or privkey is None:
                continue
            dest = await self._wallet.destination_scriptpubkey()
            txid = await refund(contract, tree, privkey, dest, self._chain)
            if txid is not None:
                self._persist(contract)
                refunded.append(txid)
                logger.info("refunded expired swap-out {} via {}", swap_id, txid)
        return refunded

    # -- Watchers -------------------------------------------------------------

    async def _await_funding(self, contract: SwapContract) -> SwapOutpoint:
        for _ in range(MAX_POLLS):
            utxos = await self._chain.get_address_utxos(contract.funding_address)
            for utxo in utxos:
                if utxo.value >= contract.onchain_amount and utxo.confirmations >= self._min_conf:
                    return SwapOutpoint(
                        txid=utxo.txid,
                        vout=utxo.vout,
                        value=utxo.value,
                        scriptpubkey=utxo.scriptpubkey,
                    )
            await asyncio.sleep(self._poll)
        raise SwapProtocolError("timed out waiting for the client's funding transaction")

    async def _await_inbound_held(self, contract: SwapContract) -> None:
        for _ in range(MAX_POLLS):
            status = await self._ln.lookup_inbound(contract.preimage_hash)
            if status == InboundStatus.HELD:
                return
            if status == InboundStatus.FAILED:
                raise InsufficientLiquidityError("hold invoice failed before funding")
            await asyncio.sleep(self._poll)
        raise SwapProtocolError("timed out waiting for the client's Lightning payment")

    async def _await_claim_preimage(
        self, outpoint: SwapOutpoint, contract: SwapContract
    ) -> bytes | None:
        """Preimage from a script-path on-chain claim, or ``None`` if the swap was
        already settled cooperatively (key-path) via the coopclaim RPC."""
        for _ in range(MAX_POLLS):
            if contract.preimage is not None or contract.state.is_terminal:
                return None
            raw = await self._chain.find_spending_tx(outpoint.txid, outpoint.vout)
            if raw is not None:
                preimage = extract_preimage_from_claim(raw)
                if preimage is not None:
                    return preimage
            await asyncio.sleep(self._poll)
        raise SwapProtocolError("timed out waiting for the client's claim")
