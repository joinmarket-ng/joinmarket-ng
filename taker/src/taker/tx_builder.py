"""
Transaction builder for CoinJoin transactions.

Builds the unsigned CoinJoin transaction from:
- Taker's UTXOs and change address
- Maker UTXOs, CJ addresses, and change addresses
- CoinJoin amount and fees
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from jmcore import transaction_policy
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    address_to_scriptpubkey,
    encode_varint,
    hash256,
    parse_transaction_bytes,
    scriptpubkey_to_address,
    serialize_transaction,
)
from jmcore.constants import BITCOIN_DUST_THRESHOLD, DUST_THRESHOLD, MAX_MONEY
from jmcore.randomness import secure_random
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.dataclasses import dataclass

logger = logging.getLogger(__name__)

LOCKTIME_SEQUENCE = transaction_policy.NON_RBF_LOCKTIME_SEQUENCE
MAX_LOCKTIME = transaction_policy.MAX_LOCKTIME
compute_tx_locktime = transaction_policy.compute_tx_locktime


# Alias for backward compatibility
varint = encode_varint


class RingTransactionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ChannelEndpointContribution(RingTransactionModel):
    """One endpoint's private contribution to a shared funding output."""

    participant_id: str = Field(min_length=1, max_length=128)
    amount: int = Field(ge=1, le=MAX_MONEY)


class FinalizedRingChannelOutput(RingTransactionModel):
    """Exact negotiated shared output and both endpoint contributions."""

    edge_id: str = Field(min_length=1, max_length=128)
    script_pubkey: str = Field(min_length=2, max_length=10_000, pattern=r"^[0-9a-f]+$")
    address: str = Field(min_length=1, max_length=320)
    capacity: int = Field(ge=1, le=MAX_MONEY)
    opener: ChannelEndpointContribution
    fundee: ChannelEndpointContribution

    @model_validator(mode="after")
    def validate_contributions(self) -> FinalizedRingChannelOutput:
        if len(self.script_pubkey) % 2:
            raise ValueError("channel script_pubkey must have an even number of hex characters")
        if len(self.script_pubkey) != 68 or not self.script_pubkey.startswith("5120"):
            raise ValueError("channel script_pubkey must be P2TR")
        if self.opener.participant_id == self.fundee.participant_id:
            raise ValueError("channel endpoints must be distinct participants")
        if self.opener.amount + self.fundee.amount != self.capacity:
            raise ValueError("endpoint contributions must equal channel capacity")
        return self


class FinalizedRingTransactionPlan(RingTransactionModel):
    """Final ring outputs and exact private residual accounting for one transaction."""

    network: str = Field(pattern=r"^(mainnet|testnet|signet|regtest)$")
    taker_id: str = "taker"
    participant_ids: list[str] = Field(min_length=4, max_length=32)
    residuals: dict[str, int] = Field(min_length=4, max_length=32)
    channel_outputs: list[FinalizedRingChannelOutput] = Field(min_length=4, max_length=32)

    @model_validator(mode="after")
    def validate_plan(self) -> FinalizedRingTransactionPlan:
        if len(set(self.participant_ids)) != len(self.participant_ids):
            raise ValueError("ring transaction plan contains duplicate participants")
        if self.participant_ids.count(self.taker_id) != 1:
            raise ValueError("ring transaction plan must contain the taker exactly once")
        if set(self.residuals) != set(self.participant_ids):
            raise ValueError("ring residuals do not cover exactly all participants")
        if any(
            type(value) is not int or not 1 <= value <= MAX_MONEY
            for value in self.residuals.values()
        ):
            raise ValueError("ring residual is outside Bitcoin money bounds")
        if len(self.channel_outputs) != len(self.participant_ids):
            raise ValueError("ring transaction plan requires one channel output per participant")
        edge_ids = [output.edge_id for output in self.channel_outputs]
        scripts = [output.script_pubkey for output in self.channel_outputs]
        if len(set(edge_ids)) != len(edge_ids):
            raise ValueError("ring transaction plan contains duplicate edge IDs")
        if len(set(scripts)) != len(scripts):
            raise ValueError("ring transaction plan contains duplicate channel scripts")

        outgoing: dict[str, int] = {}
        incoming: dict[str, int] = {}
        for index, output in enumerate(self.channel_outputs):
            try:
                script = bytes.fromhex(output.script_pubkey)
                decoded_script = address_to_scriptpubkey(output.address)
                canonical_address = scriptpubkey_to_address(script, self.network)
            except ValueError as exc:
                raise ValueError(f"invalid channel output {output.edge_id}: {exc}") from exc
            if decoded_script != script or output.address.lower() != canonical_address:
                raise ValueError("channel address and script_pubkey do not match the plan network")
            if output.opener.participant_id in outgoing:
                raise ValueError("participant opens more than one ring channel")
            if output.fundee.participant_id in incoming:
                raise ValueError("participant accepts more than one ring channel")
            if (
                output.opener.participant_id != self.participant_ids[index]
                or output.fundee.participant_id
                != self.participant_ids[(index + 1) % len(self.participant_ids)]
            ):
                raise ValueError("ring channel outputs do not form the declared directed cycle")
            outgoing[output.opener.participant_id] = output.opener.amount
            incoming[output.fundee.participant_id] = output.fundee.amount

        participants = set(self.participant_ids)
        if set(outgoing) != participants or set(incoming) != participants:
            raise ValueError("every participant must open one and accept one ring channel")
        for participant_id, residual in self.residuals.items():
            if outgoing[participant_id] + incoming[participant_id] != residual:
                raise ValueError(
                    f"channel contributions do not equal residual for {participant_id!r}"
                )
        if sum(output.capacity for output in self.channel_outputs) != sum(self.residuals.values()):
            raise ValueError("channel output sum does not equal finalized residual sum")
        return self


@dataclass
class CoinJoinTxData:
    """Data for building a CoinJoin transaction."""

    # Taker data
    taker_inputs: list[TxInput]
    taker_cj_output: TxOutput
    taker_change_output: TxOutput | None

    # Maker data (by nick)
    maker_inputs: dict[str, list[TxInput]]
    maker_cj_outputs: dict[str, TxOutput]
    maker_change_outputs: dict[str, TxOutput]

    # Amounts
    cj_amount: int
    total_maker_fee: int
    tx_fee: int
    ring_plan: FinalizedRingTransactionPlan | None = None


class CoinJoinTxBuilder:
    """
    Builds CoinJoin transactions.

    The transaction structure:
    - Inputs: Taker inputs + Maker inputs (shuffled)
    - Outputs: Equal CJ outputs + Change outputs (shuffled)
    """

    def __init__(self, network: str = "mainnet", locktime: int = 0):
        if (
            not isinstance(locktime, int)
            or isinstance(locktime, bool)
            or not 0 <= locktime <= MAX_LOCKTIME
        ):
            raise ValueError(f"Invalid transaction locktime: {locktime!r}")
        self.network = network
        self.locktime = locktime

    def build_unsigned_tx(self, tx_data: CoinJoinTxData) -> tuple[bytes, dict[str, Any]]:
        """
        Build an unsigned CoinJoin transaction.

        Args:
            tx_data: Transaction data with all inputs and outputs

        Returns:
            (tx_bytes, metadata) where metadata maps inputs/outputs to owners
        """
        # Collect all inputs with owner info
        all_inputs: list[tuple[TxInput, str]] = []

        for inp in tx_data.taker_inputs:
            all_inputs.append((inp, "taker"))

        for nick, inputs in tx_data.maker_inputs.items():
            for inp in inputs:
                all_inputs.append((inp, nick))

        # Collect all outputs with owner info
        all_outputs: list[tuple[TxOutput, str | None, str]] = []  # (output, owner, type)

        # CJ outputs (equal amounts)
        all_outputs.append((tx_data.taker_cj_output, "taker", "cj"))
        for nick, out in tx_data.maker_cj_outputs.items():
            all_outputs.append((out, nick, "cj"))

        if tx_data.ring_plan is None:
            # Change outputs
            if tx_data.taker_change_output:
                all_outputs.append((tx_data.taker_change_output, "taker", "change"))
            for nick, out in tx_data.maker_change_outputs.items():
                all_outputs.append((out, nick, "change"))
        else:
            if tx_data.taker_change_output is not None or tx_data.maker_change_outputs:
                raise ValueError("ring transactions cannot contain plain change outputs")
            expected_participants = {"taker", *tx_data.maker_cj_outputs}
            if set(tx_data.ring_plan.participant_ids) != expected_participants:
                raise ValueError("ring transaction participants do not match CoinJoin participants")
            equal_outputs = [tx_data.taker_cj_output, *tx_data.maker_cj_outputs.values()]
            if any(
                len(output.script) != 34 or output.script[:2] != b"\x51\x20"
                for output in equal_outputs
            ):
                raise ValueError("ring transactions require tr0 P2TR equal outputs")
            all_ring_scripts = [output.scriptpubkey for output in equal_outputs] + [
                channel.script_pubkey for channel in tx_data.ring_plan.channel_outputs
            ]
            if len(set(all_ring_scripts)) != len(all_ring_scripts):
                raise ValueError("ring transaction contains duplicate output scripts")
            for channel in tx_data.ring_plan.channel_outputs:
                all_outputs.append(
                    (TxOutput.from_hex(channel.script_pubkey, channel.capacity), None, "channel")
                )

        # Ring positions are public manifest commitments, and ordinary CoinJoin
        # positions are privacy-sensitive, so both use OS-backed randomness.
        secure_random.shuffle(all_inputs)
        secure_random.shuffle(all_outputs)

        # Build metadata
        metadata = {
            "input_owners": [owner for _, owner in all_inputs],
            "output_owners": [(owner, out_type) for _, owner, out_type in all_outputs],
            "input_values": [inp.value for inp, _ in all_inputs],
            "fee": tx_data.tx_fee,
        }
        if tx_data.ring_plan is not None:
            channel_by_script = {
                channel.script_pubkey: channel for channel in tx_data.ring_plan.channel_outputs
            }
            metadata["channel_edges"] = [
                channel_by_script[output.scriptpubkey].edge_id if out_type == "channel" else None
                for output, _, out_type in all_outputs
            ]
            metadata["channel_endpoints"] = [
                (
                    channel_by_script[output.scriptpubkey].opener.model_dump(),
                    channel_by_script[output.scriptpubkey].fundee.model_dump(),
                )
                if out_type == "channel"
                else None
                for output, _, out_type in all_outputs
            ]

        # Serialize transaction
        tx_bytes = self._serialize_tx(
            inputs=[inp for inp, _ in all_inputs],
            outputs=[out for out, _, _ in all_outputs],
        )

        return tx_bytes, metadata

    def _serialize_tx(self, inputs: list[TxInput], outputs: list[TxOutput]) -> bytes:
        """Serialize transaction to bytes.

        For unsigned transactions, we use non-SegWit format (no marker/flag/witness).
        The SegWit marker (0x00, 0x01) is only added when witnesses are present.
        """
        serialized_inputs = (
            [replace(tx_input, sequence=LOCKTIME_SEQUENCE) for tx_input in inputs]
            if self.locktime != 0
            else inputs
        )
        return serialize_transaction(
            version=2,
            inputs=serialized_inputs,
            outputs=outputs,
            locktime=self.locktime,
            witnesses=None,
        )

    def add_signatures(
        self,
        tx_bytes: bytes,
        signatures: dict[str, list[dict[str, Any]]],
        metadata: dict[str, Any],
    ) -> bytes:
        """
        Add signatures to transaction.

        Every input must have a matching signature. A CoinJoin transaction with
        any unsigned input is invalid and must never be broadcast.

        Args:
            tx_bytes: Unsigned transaction
            signatures: Dict of nick -> list of signature info
            metadata: Transaction metadata with input owners

        Returns:
            Signed transaction bytes

        Raises:
            ValueError: If any input is missing a signature
        """
        from loguru import logger as log

        # Parse unsigned tx using jmcore
        parsed = parse_transaction_bytes(tx_bytes)

        log.debug(f"add_signatures: {len(parsed.inputs)} inputs, {len(parsed.outputs)} outputs")
        log.debug(f"input_owners: {metadata.get('input_owners', [])}")
        log.debug(f"signatures keys: {list(signatures.keys())}")

        # Build witness data
        new_witnesses: list[list[bytes]] = []
        input_owners = metadata["input_owners"]
        unsigned_inputs: list[str] = []

        for i, owner in enumerate(input_owners):
            inp = parsed.inputs[i]
            log.debug(f"Input {i}: owner={owner}, txid={inp.txid[:16]}..., vout={inp.vout}")

            if owner in signatures:
                # Find matching signature
                for sig_info in signatures[owner]:
                    if sig_info.get("txid") == inp.txid and sig_info.get("vout") == inp.vout:
                        witness = sig_info.get("witness", [])
                        new_witnesses.append([bytes.fromhex(w) for w in witness])
                        log.debug(f"  -> Found matching signature, witness len={len(witness)}")
                        break
                else:
                    unsigned_inputs.append(
                        f"input {i} (owner={owner}, txid={inp.txid[:16]}...:{inp.vout})"
                    )
                    new_witnesses.append([])
            else:
                unsigned_inputs.append(
                    f"input {i} (owner={owner}, txid={inp.txid[:16]}...:{inp.vout})"
                )
                new_witnesses.append([])

        if unsigned_inputs:
            raise ValueError(
                f"Cannot assemble transaction: {len(unsigned_inputs)} input(s) missing "
                f"signatures: {', '.join(unsigned_inputs)}. "
                f"All inputs must be signed for a valid transaction."
            )

        # Reserialize with witnesses using jmcore
        return serialize_transaction(
            version=parsed.version,
            inputs=parsed.inputs,
            outputs=parsed.outputs,
            locktime=parsed.locktime,
            witnesses=new_witnesses,
        )

    def get_txid(self, tx_bytes: bytes) -> str:
        """Calculate txid (double SHA256 of non-witness data)."""
        parsed = parse_transaction_bytes(tx_bytes)

        # Serialize without witness for txid calculation
        data = serialize_transaction(
            version=parsed.version,
            inputs=parsed.inputs,
            outputs=parsed.outputs,
            locktime=parsed.locktime,
            witnesses=None,
        )

        return hash256(data)[::-1].hex()


def calculate_tx_fee(
    num_taker_inputs: int,
    num_maker_inputs: int,
    num_outputs: int,
    fee_rate: float,
    script_type: str = "p2wpkh",
) -> int:
    """
    Calculate transaction fee based on estimated vsize.

    Native segwit (p2wpkh) inputs are ~68 vbytes and outputs 31 vbytes;
    taproot (p2tr) key-path inputs are ~57.5 vbytes and outputs 43 vbytes.

    Args:
        fee_rate: Fee rate in sat/vB (can be fractional, e.g. 0.5)
        script_type: CoinJoin script type ("p2wpkh" or "p2tr") used to size
            the regular inputs and outputs (rigid pit, JMP-0010).

    Returns:
        Fee in satoshis (rounded up to ensure minimum relay fee)
    """
    import math

    from jmcore.bitcoin import estimate_vsize

    num_inputs = num_taker_inputs + num_maker_inputs
    vsize = estimate_vsize([script_type] * num_inputs, [script_type] * num_outputs)

    # Round up to ensure we pay at least the minimum
    return math.ceil(vsize * fee_rate)


def build_coinjoin_tx(
    # Taker data
    taker_utxos: list[dict[str, Any]],
    taker_cj_address: str,
    taker_change_address: str,
    taker_total_input: int,
    # Maker data
    maker_data: dict[str, dict[str, Any]],  # nick -> {utxos, cj_addr, change_addr, cjfee, txfee}
    # Amounts
    cj_amount: int,
    tx_fee: int,
    network: str = "mainnet",
    dust_threshold: int = DUST_THRESHOLD,
    locktime: int = 0,
    ring_plan: FinalizedRingTransactionPlan | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """
    Build a complete CoinJoin transaction.

    Args:
        taker_utxos: List of taker's UTXOs
        taker_cj_address: Taker's CJ output address
        taker_change_address: Taker's change address (empty string if no change needed)
        taker_total_input: Total value of taker's inputs
        maker_data: Dict of maker nick -> {utxos, cj_addr, change_addr, cjfee, txfee}
        cj_amount: Equal CoinJoin output amount
        tx_fee: Total transaction fee
        network: Network name
        dust_threshold: Backward-compatible maker threshold argument. JoinMarket
            coordination requires this to remain 27300.
        locktime: Current-height anti-fee-sniping locktime. Nonzero values use
            non-final, non-RBF input sequences.
        ring_plan: Finalized co-funded channel ring plan. When set, every
            participant residual is spent into a shared channel output instead
            of a plain change output.

    Returns:
        (tx_bytes, metadata)
    """
    if dust_threshold != DUST_THRESHOLD:
        raise ValueError(
            f"Maker change threshold is fixed at {DUST_THRESHOLD} sats for peer compatibility"
        )

    builder = CoinJoinTxBuilder(network, locktime=locktime)

    # Build taker inputs
    taker_inputs = [
        TxInput.from_hex(
            txid=u["txid"],
            vout=u["vout"],
            value=u["value"],
            scriptpubkey=u.get("scriptpubkey", ""),
        )
        for u in taker_utxos
    ]

    # Calculate taker's fees paid to makers
    total_maker_fee = sum(m["cjfee"] for m in maker_data.values())

    # Taker's change = total_input - cj_amount - maker_fees - tx_fee
    taker_change = taker_total_input - cj_amount - total_maker_fee - tx_fee

    # Taker CJ output
    taker_cj_output = TxOutput.from_address(taker_cj_address, cj_amount)

    if ring_plan is not None and ring_plan.network != network:
        raise ValueError("ring transaction plan network does not match transaction network")

    # Taker change output (if any)
    taker_change_output = None
    if ring_plan is None and taker_change > BITCOIN_DUST_THRESHOLD and taker_change_address:
        taker_change_output = TxOutput.from_address(taker_change_address, taker_change)
    elif ring_plan is None and taker_change > 0:
        logger.warning(
            f"Taker change {taker_change} sats "
            + (
                "has no change address"
                if not taker_change_address
                else f"is at or below the taker change threshold ({BITCOIN_DUST_THRESHOLD})"
            )
            + ", no change output will be created (added to the mining fee)"
        )

    # Build maker data
    maker_inputs: dict[str, list[TxInput]] = {}
    maker_cj_outputs: dict[str, TxOutput] = {}
    maker_change_outputs: dict[str, TxOutput] = {}
    finalized_residuals = {"taker": taker_change}

    for nick, data in maker_data.items():
        # Maker inputs
        maker_inputs[nick] = [
            TxInput.from_hex(
                txid=u["txid"],
                vout=u["vout"],
                value=u["value"],
                scriptpubkey=u.get("scriptpubkey", ""),
            )
            for u in data["utxos"]
        ]

        # Maker CJ output (cj_amount)
        maker_cj_outputs[nick] = TxOutput.from_address(data["cj_addr"], cj_amount)

        # Maker change output
        # Formula: change = inputs - cj_amount - txfee + cjfee
        # (Maker pays txfee, receives cjfee from taker)
        maker_total_input = sum(u["value"] for u in data["utxos"])
        maker_txfee = data.get("txfee", 0)
        maker_change = maker_total_input - cj_amount - maker_txfee + data["cjfee"]
        finalized_residuals[nick] = maker_change

        logger.debug(
            f"Maker {nick} change calculation: "
            f"inputs={maker_total_input}, cj_amount={cj_amount}, "
            f"cjfee={data['cjfee']}, txfee={maker_txfee}, change={maker_change}, "
            f"dust_threshold={DUST_THRESHOLD}"
        )

        if maker_change < 0:
            # Negative change means maker's UTXOs are insufficient
            # This can happen if UTXO verification failed (value=0) or if UTXOs were spent
            raise ValueError(
                f"Maker {nick} has insufficient funds: inputs={maker_total_input} sats, "
                f"required={cj_amount + maker_txfee - data['cjfee']} sats, "
                f"change={maker_change} sats. Maker's UTXOs may have been spent."
            )
        elif ring_plan is None and maker_change >= DUST_THRESHOLD:
            maker_change_outputs[nick] = TxOutput.from_address(data["change_addr"], maker_change)
        elif ring_plan is None:
            logger.warning(
                f"Maker {nick} change {maker_change} sats is below dust threshold "
                f"({DUST_THRESHOLD}), "
                "no change output will be created"
            )

    if ring_plan is not None:
        expected_participants = {"taker", *maker_data}
        if set(ring_plan.participant_ids) != expected_participants:
            raise ValueError("ring transaction participants do not match taker and makers")
        if ring_plan.residuals != finalized_residuals:
            raise ValueError("ring transaction contributions do not match finalized residuals")

    tx_data = CoinJoinTxData(
        taker_inputs=taker_inputs,
        taker_cj_output=taker_cj_output,
        taker_change_output=taker_change_output,
        maker_inputs=maker_inputs,
        maker_cj_outputs=maker_cj_outputs,
        maker_change_outputs=maker_change_outputs,
        cj_amount=cj_amount,
        total_maker_fee=total_maker_fee,
        tx_fee=tx_fee,
        ring_plan=ring_plan,
    )

    return builder.build_unsigned_tx(tx_data)
