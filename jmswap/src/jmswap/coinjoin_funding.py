"""The private channel funding boundary of a CoinJoin round.

Channel funding outputs never become wallet UTXOs. This adapter supplies public
prevouts and the escrow destination to whichever CoinJoin role prepared the
buyout (taker or maker), then delegates channel signatures to the durable buyer
runtime after the complete CoinJoin has been validated.
"""

from __future__ import annotations

from typing import Any

from jmcore.bitcoin import parse_transaction_bytes, scriptpubkey_to_address

from jmswap.buyout_messages import Prevout
from jmswap.buyout_signing import BuyoutBuyer
from jmswap.buyout_terms import ProtocolError, validate_parent


class ChannelBuyout:
    def __init__(self, buyer: BuyoutBuyer, session_id: str) -> None:
        record = buyer.store.get(session_id)
        if record.role != "buyer" or record.state != "ACCEPTED":
            raise ProtocolError("a new CoinJoin requires an accepted, unused buyout")
        self.buyer, self.session_id = buyer, session_id
        self.terms = buyer.terms(session_id)
        self.change_address = scriptpubkey_to_address(
            self.terms.escrow.output_script(), self.terms.proposal.network
        )

    @property
    def inputs(self) -> list[dict[str, Any]]:
        return [
            {
                "txid": channel.point.txid,
                "vout": channel.point.vout,
                "value": channel.capacity_sat,
                "scriptpubkey": channel.funding_script.hex(),
            }
            for channel in self.terms.channels
        ]

    def begin_round(self) -> None:
        self.buyer.reserve_coinjoin(self.session_id)

    @property
    def total_value(self) -> int:
        return sum(channel.capacity_sat for channel in self.terms.channels)

    @property
    def minimum_change(self) -> int:
        proposal = self.terms.proposal
        return (
            self.terms.counterparty_split_sat
            + proposal.min_split_output
            + max(proposal.split_fee, proposal.sweep_fee_reserve)
        )

    def wallet_funding_required(self, total_required: int) -> int:
        """How much of ``total_required`` spendable wallet UTXOs must still cover.

        Channel value pays for the CoinJoin, but the escrow change output must
        keep at least :attr:`minimum_change`, so that reserve is added back. A
        buyout always needs an ordinary wallet input, hence never zero.
        """
        return max(1, total_required + self.minimum_change - self.total_value)

    def _parent_arguments(
        self,
        raw: bytes,
        prevout_map: dict[tuple[str, int], tuple[int, bytes]],
    ) -> tuple[list[Prevout], list[int], int]:
        parsed = parse_transaction_bytes(raw)
        keys = [(item.txid, item.vout) for item in parsed.inputs]
        if len(set(keys)) != len(keys):
            raise ProtocolError("parent repeats an input")
        try:
            prevouts = [
                Prevout(value=prevout_map[key][0], script_pubkey=prevout_map[key][1].hex())
                for key in keys
            ]
            indices = [
                keys.index((channel.point.txid, channel.point.vout))
                for channel in self.terms.channels
            ]
        except (KeyError, ValueError):
            raise ProtocolError("parent is missing buyout inputs or verified prevouts") from None
        outputs = [
            n
            for n, output in enumerate(parsed.outputs)
            if output.script == self.terms.escrow.output_script()
        ]
        if len(outputs) != 1:
            raise ProtocolError("parent must contain exactly one escrow output")
        if len(keys) <= len(indices):
            raise ProtocolError("buyout requires an ordinary wallet input")
        return prevouts, indices, outputs[0]

    def validate(
        self, raw: bytes, prevout_map: dict[tuple[str, int], tuple[int, bytes]], height: int
    ) -> None:
        prevouts, indices, escrow_index = self._parent_arguments(raw, prevout_map)
        validate_parent(self.terms, raw, prevouts, indices, escrow_index, height)

    async def sign(
        self, raw: bytes, prevout_map: dict[tuple[str, int], tuple[int, bytes]]
    ) -> list[dict[str, Any]]:
        prevouts, indices, escrow_index = self._parent_arguments(raw, prevout_map)
        signatures = await self.buyer.sign_parent(
            self.session_id, raw, prevouts, indices, escrow_index
        )
        expected = {(channel.point.txid, channel.point.vout) for channel in self.terms.channels}
        if set(signatures) != expected:
            raise ProtocolError("buyout returned signatures for an unexpected input set")
        return [
            {
                "txid": txid,
                "vout": vout,
                "signature": signature.hex(),
                "pubkey": "",
                "witness": [signature.hex()],
            }
            for (txid, vout), signature in signatures.items()
        ]
