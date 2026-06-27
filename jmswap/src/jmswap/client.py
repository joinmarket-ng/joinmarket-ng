"""
Swap client: the user side of a bidirectional Lightning swap.

A client discovers a provider's terms, requests a swap, **verifies** the
returned funding address against the parameters it agreed to (so the provider
cannot pin the swap to a script the client did not accept), and then executes:

* **swap-in** (client funds on-chain, receives Lightning): create an invoice for
  the amount to receive, fund the HTLC, wait for the provider to pay the invoice.
* **swap-out** (client pays Lightning, receives on-chain): pay the hold invoice,
  wait for the provider to fund the HTLC, then claim it on-chain with the
  preimage (which lets the provider settle the hold invoice).

The provider is reached through an injected async ``rpc(method, params)``
callable, so the client is decoupled from the transport (Nostr or in-process).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from jmcore.bitcoin import sha256
from jmcore.taproot import TaprootTx
from loguru import logger

from jmswap.chain import ChainBackend
from jmswap.errors import SwapProtocolError, SwapVerificationError
from jmswap.lightning import InboundStatus, LightningBackend
from jmswap.models import (
    ProviderTerms,
    SwapContract,
    SwapDirection,
    SwapRequest,
    SwapResponse,
    SwapState,
)
from jmswap.spend import SwapOutpoint, build_claim_tx, estimate_fee
from jmswap.swap_tree import SwapTree, verify_swap_tree
from jmswap.wallet import SwapWallet

Rpc = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

POLL_INTERVAL = 0.05
MAX_POLLS = 2000


class SwapClient:
    """The user side of a swap, bound to Lightning, chain and wallet backends."""

    def __init__(
        self,
        lightning: LightningBackend,
        chain: ChainBackend,
        wallet: SwapWallet,
        *,
        feerate_sat_vb: float = 2.0,
        poll_interval: float = POLL_INTERVAL,
        min_confirmations: int = 1,
    ) -> None:
        self._ln = lightning
        self._chain = chain
        self._wallet = wallet
        self._feerate = feerate_sat_vb
        self._poll = poll_interval
        self._min_conf = max(min_confirmations, 1)

    async def swap_in(
        self, rpc: Rpc, terms: ProviderTerms, amount: int, *, whole_coin: bool = False
    ) -> SwapContract:
        """Receive ``amount`` sats on Lightning by funding an on-chain HTLC."""
        invoice, payment_hash = await self._ln.create_invoice(amount * 1000)
        priv, pub = await self._wallet.new_swap_keypair()
        request = SwapRequest(
            direction=SwapDirection.SWAP_IN,
            amount=amount,
            preimage_hash=payment_hash,
            client_pubkey=pub.hex(),
            invoice=invoice,
            whole_coin=whole_coin,
        )
        response = await self._request(rpc, request)
        tree = verify_swap_tree(
            is_reverse=False,
            preimage_hash=bytes.fromhex(payment_hash),
            server_pubkey=bytes.fromhex(response.server_pubkey),
            client_pubkey=pub,
            locktime=response.locktime,
            expected_address=response.funding_address,
            expected_swap_tree=response.swap_tree,
            network=terms.network,
        )
        contract = self._contract(response, payment_hash, pub, priv)
        contract.advance(SwapState.AWAITING_LOCKUP)

        outpoint = await self._wallet.fund_swap(response.funding_address, response.onchain_amount)
        contract.lockup_txid, contract.lockup_vout = outpoint.txid, outpoint.vout
        contract.advance(SwapState.LOCKUP_SEEN)
        contract.advance(SwapState.LOCKUP_CONFIRMED)

        # We are done once the provider has paid (settled) our invoice.
        await self._await_invoice_settled(payment_hash)
        contract.advance(SwapState.CLAIMED)
        logger.info("swap-in {} complete; received Lightning", contract.swap_id)

        # Best-effort: help the provider claim its coins via the cooperative
        # key-path spend (an ordinary P2TR signature, no preimage or script
        # on-chain), improving the on-chain privacy of the whole swap. If the
        # provider declines or anything fails, it falls back to its own
        # script-path claim, so we never block the completed swap on this.
        await self._offer_cooperative_swap_in(rpc, contract, tree, outpoint, priv)
        return contract

    async def _offer_cooperative_swap_in(
        self,
        rpc: Rpc,
        contract: SwapContract,
        tree: SwapTree,
        outpoint: SwapOutpoint,
        client_priv: bytes,
    ) -> None:
        """Co-sign and broadcast the provider's cooperative key-path claim."""
        from jmswap.cooperative import cooperative_claim_swap_in

        async def cosign(client_pubnonce_hex: str) -> tuple[str, str, str, int]:
            reply = await rpc(
                "coopclaim_in",
                {"swap_id": contract.swap_id, "client_pubnonce": client_pubnonce_hex},
            )
            return (
                reply["server_pubnonce"],
                reply["server_partial"],
                reply["destination_spk"],
                int(reply["fee"]),
            )

        # The provider only co-signs once it has recorded paying our invoice;
        # retry briefly to ride out the settle/pay ordering race.
        for attempt in range(20):
            try:
                tx = await cooperative_claim_swap_in(tree, outpoint, client_priv, cosign)
                contract.spend_txid = await self._chain.broadcast(tx.serialize().hex())
                logger.info("swap-in {} provider claim co-signed (key-path)", contract.swap_id)
                return
            except Exception as exc:  # noqa: BLE001 - provider falls back to script-path
                if "not yet paid" in str(exc) and attempt < 19:
                    await asyncio.sleep(self._poll)
                    continue
                logger.debug("cooperative swap-in claim skipped ({})", exc)
                return

    async def swap_out(self, rpc: Rpc, terms: ProviderTerms, amount: int) -> SwapContract:
        """Receive ``amount`` sats on-chain by paying a Lightning hold invoice."""
        preimage = secrets.token_bytes(32)
        payment_hash = sha256(preimage).hex()
        priv, pub = await self._wallet.new_swap_keypair()
        request = SwapRequest(
            direction=SwapDirection.SWAP_OUT,
            amount=amount,
            preimage_hash=payment_hash,
            client_pubkey=pub.hex(),
        )
        response = await self._request(rpc, request)
        if response.invoice is None:
            raise SwapProtocolError("swap-out response missing the hold invoice")
        tree = verify_swap_tree(
            is_reverse=True,
            preimage_hash=sha256(preimage),
            server_pubkey=bytes.fromhex(response.server_pubkey),
            client_pubkey=pub,
            locktime=response.locktime,
            expected_address=response.funding_address,
            expected_swap_tree=response.swap_tree,
            network=terms.network,
        )
        contract = self._contract(response, payment_hash, pub, priv)
        contract.preimage = preimage.hex()
        contract.advance(SwapState.AWAITING_LOCKUP)

        # Pay the hold invoice (held until we claim on-chain), then wait for funding.
        pay_task = asyncio.create_task(self._ln.pay_invoice(response.invoice))
        try:
            outpoint = await self._await_funding(response.funding_address, response.onchain_amount)
            contract.lockup_txid, contract.lockup_vout = outpoint.txid, outpoint.vout
            contract.advance(SwapState.LOCKUP_SEEN)
            contract.advance(SwapState.LOCKUP_CONFIRMED)

            dest = await self._wallet.destination_scriptpubkey()
            # Prefer the cooperative MuSig2 key-path claim: an ordinary P2TR
            # spend that reveals neither the preimage nor the swap script
            # on-chain. Fall back to the trustless script-path claim if the
            # provider does not cooperate.
            claim_tx = await self._claim_swap_out(
                rpc, contract, tree, outpoint, dest, preimage, priv
            )
            contract.spend_txid = await self._chain.broadcast(claim_tx.serialize().hex())
            contract.advance(SwapState.CLAIMED)

            result = await pay_task  # settles once the provider has our preimage
            if result.status.value != "succeeded":
                raise SwapProtocolError("hold invoice did not settle after claim")
        finally:
            if not pay_task.done():
                pay_task.cancel()
        logger.info("swap-out {} complete; received on-chain", contract.swap_id)
        return contract

    async def _claim_swap_out(
        self,
        rpc: Rpc,
        contract: SwapContract,
        tree: SwapTree,
        outpoint: SwapOutpoint,
        dest: bytes,
        preimage: bytes,
        priv: bytes,
    ) -> TaprootTx:
        """Claim a swap-out, preferring the cooperative key-path spend."""
        from jmswap.cooperative import cooperative_claim_client

        fee = estimate_fee(110, self._feerate)  # cooperative (key-path) is smaller

        async def cosign(client_pubnonce_hex: str) -> tuple[str, str]:
            reply = await rpc(
                "coopclaim",
                {
                    "swap_id": contract.swap_id,
                    "destination_spk": dest.hex(),
                    "fee": fee,
                    "client_pubnonce": client_pubnonce_hex,
                    "preimage": preimage.hex(),
                },
            )
            return reply["server_pubnonce"], reply["server_partial"]

        try:
            tx = await cooperative_claim_client(tree, outpoint, dest, fee, priv, cosign)
            logger.info("swap-out {} claimed cooperatively (key-path)", contract.swap_id)
            return tx
        except Exception as exc:  # noqa: BLE001 - fall back to the trustless script path
            logger.warning("cooperative claim failed ({}); using script-path", exc)
            return build_claim_tx(
                tree, outpoint, preimage, priv, dest, estimate_fee(150, self._feerate)
            )  # type: ignore[arg-type]

    # -- helpers --------------------------------------------------------------

    async def _request(self, rpc: Rpc, request: SwapRequest) -> SwapResponse:
        raw = await rpc("createswap", request.model_dump(mode="json"))
        response = SwapResponse.model_validate(raw)
        if response.direction != request.direction:
            raise SwapVerificationError("provider changed the swap direction")
        if response.onchain_amount <= 0:
            raise SwapVerificationError("provider returned a non-positive on-chain amount")
        return response

    def _contract(
        self, response: SwapResponse, payment_hash: str, pub: bytes, priv: bytes
    ) -> SwapContract:
        _ = priv
        return SwapContract(
            swap_id=response.swap_id,
            direction=response.direction,
            network="regtest" if response.funding_address.startswith("bcrt1") else "mainnet",
            server_pubkey=response.server_pubkey,
            client_pubkey=pub.hex(),
            preimage_hash=payment_hash,
            locktime=response.locktime,
            onchain_amount=response.onchain_amount,
            funding_address=response.funding_address,
            invoice=response.invoice,
        )

    async def _await_funding(self, address: str, min_amount: int) -> SwapOutpoint:
        for _ in range(MAX_POLLS):
            for utxo in await self._chain.get_address_utxos(address):
                if utxo.value >= min_amount and utxo.confirmations >= self._min_conf:
                    return SwapOutpoint(
                        txid=utxo.txid,
                        vout=utxo.vout,
                        value=utxo.value,
                        scriptpubkey=utxo.scriptpubkey,
                    )
            await asyncio.sleep(self._poll)
        raise SwapProtocolError("timed out waiting for the provider's funding transaction")

    async def _await_invoice_settled(self, payment_hash: str) -> None:
        for _ in range(MAX_POLLS):
            status = await self._ln.lookup_inbound(payment_hash)
            if status == InboundStatus.SETTLED:
                return
            if status == InboundStatus.FAILED:
                raise SwapProtocolError("provider failed to pay our invoice")
            await asyncio.sleep(self._poll)
        raise SwapProtocolError("timed out waiting for the provider's Lightning payment")
