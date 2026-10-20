"""Wallet ownership analysis and partial signing for BIP174 PSBTs."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

from jmcore.bitcoin import (
    create_p2wpkh_script_code,
    encode_varint,
    estimate_vsize,
    pubkey_to_p2wpkh_script,
    script_to_p2wsh_address,
    serialize_transaction,
)
from jmcore.btc_script import parse_freeze_script
from jmcore.timenumber import timestamp_to_timenumber

from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.psbt import (
    PSBT_GLOBAL_TX_MODIFIABLE,
    PSBT_IN_BIP32_DERIVATION,
    PSBT_IN_FINAL_SCRIPTSIG,
    PSBT_IN_FINAL_SCRIPTWITNESS,
    PSBT_IN_PARTIAL_SIG,
    PSBT_IN_SIGHASH_TYPE,
    PSBT_IN_WITNESS_SCRIPT,
    PSBT_IN_WITNESS_UTXO,
    TX_MODIFIABLE_INPUTS,
    TX_MODIFIABLE_OUTPUTS,
    ParsedPSBT,
    PSBTError,
    PSBTKeyValue,
    PSBTMap,
    WitnessUTXO,
    parse_bip32_derivation,
    parse_psbt,
    parse_witness_utxo,
)
from jmwallet.wallet.signing import (
    TransactionSigningError,
    verify_p2wpkh_signature,
    verify_p2wsh_signature,
)

if TYPE_CHECKING:
    from jmcore.bitcoin import ParsedTransaction

    from jmwallet.wallet.bip32 import HDKey
    from jmwallet.wallet.signer import SignedInput

HARDENED = 0x80000000
MAX_DERIVATION_INDEX = 1_000_000
MAX_MONEY = 21_000_000 * 100_000_000

PSBTInputType = Literal["p2wpkh", "p2wsh", "unsupported"]
WalletInputType = Literal["regular", "fidelity-bond"]


@dataclass(frozen=True)
class PSBTInputPlan:
    """Ownership and signing state for one PSBT input."""

    index: int
    witness_utxo: WitnessUTXO
    input_type: PSBTInputType
    utxo: UTXOInfo | None = None
    pubkey: bytes | None = None
    wallet_input_type: WalletInputType | None = None
    already_signed: bool = False
    finalized: bool = False

    @property
    def owned(self) -> bool:
        return self.utxo is not None

    @property
    def signable(self) -> bool:
        return self.owned and not self.already_signed and not self.finalized


@dataclass(frozen=True)
class PSBTSigningPlan:
    """A reviewed PSBT and the wallet inputs that can be signed."""

    psbt: ParsedPSBT
    inputs: tuple[PSBTInputPlan, ...]
    fee: int
    estimated_vsize: int
    source_psbt: bytes
    scan_range: int
    fee_rate_is_upper_bound: bool = False

    @property
    def estimated_fee_rate(self) -> float:
        if self.estimated_vsize == 0:
            return 0.0
        return self.fee / self.estimated_vsize

    @property
    def owned_count(self) -> int:
        return sum(input_plan.owned for input_plan in self.inputs)

    @property
    def signable_count(self) -> int:
        return sum(input_plan.signable for input_plan in self.inputs)


@dataclass(frozen=True)
class PSBTSigningResult:
    """The updated PSBT and indices signed by this invocation."""

    psbt: bytes
    signed_indices: tuple[int, ...]
    already_signed_indices: tuple[int, ...]


class WalletPSBTSigningMixin:
    """Mixin implementing safe, partial signing of wallet-owned PSBT inputs."""

    network: str
    mixdepth_count: int
    root_path: str
    master_key: HDKey
    address_cache: dict[str, tuple[int, int, int]]
    fidelity_bond_locktime_cache: dict[str, int]

    def get_address(self, mixdepth: int, change: int, index: int) -> str:  # pragma: no cover
        raise NotImplementedError

    def get_fidelity_bond_address(self, index: int, locktime: int) -> str:  # pragma: no cover
        raise NotImplementedError

    def get_fidelity_bond_path(self, index: int) -> str:  # pragma: no cover
        raise NotImplementedError

    def get_key_for_address(self, address: str) -> HDKey | None:  # pragma: no cover
        raise NotImplementedError

    def sign_input(
        self, tx: ParsedTransaction, input_index: int, utxo: UTXOInfo
    ) -> SignedInput:  # pragma: no cover
        raise NotImplementedError

    def prepare_psbt_signing(self, psbt_bytes: bytes, scan_range: int) -> PSBTSigningPlan:
        """Parse a PSBT, establish ownership, and validate it without signing."""
        if scan_range < 0 or scan_range > MAX_DERIVATION_INDEX:
            raise PSBTError(f"PSBT scan range must be between 0 and {MAX_DERIVATION_INDEX:,}")

        psbt = parse_psbt(psbt_bytes)
        transaction = psbt.transaction
        if not transaction.inputs or not transaction.outputs:
            raise PSBTError("PSBT signing requires at least one input and one output")
        outpoints = [(tx_input.txid, tx_input.vout) for tx_input in transaction.inputs]
        if len(set(outpoints)) != len(outpoints):
            raise PSBTError("PSBT unsigned transaction contains duplicate input outpoints")

        witness_utxos: list[WitnessUTXO] = []
        input_types: list[PSBTInputType] = []
        sighash_types: list[int] = []
        for index, input_map in enumerate(psbt.input_maps):
            witness_record = _record_for_type(input_map, PSBT_IN_WITNESS_UTXO)
            if witness_record is None:
                raise PSBTError(
                    f"PSBT input {index} is missing witness_utxo; all input amounts are required "
                    "for fee review"
                )
            witness_utxo = parse_witness_utxo(witness_record.value)
            if witness_utxo.value > MAX_MONEY:
                raise PSBTError(f"PSBT input {index} amount exceeds Bitcoin MAX_MONEY")
            input_type = _classify_input_script(witness_utxo.script_pubkey)
            if input_type == "unsupported":
                raise PSBTError(
                    f"PSBT input {index} has an unsupported prevout script; all inputs must be "
                    "native P2WPKH or P2WSH"
                )
            witness_utxos.append(witness_utxo)
            input_types.append(input_type)
            sighash_types.append(_parse_sighash_type(input_map, index))

            for record in _records_for_type(input_map, PSBT_IN_BIP32_DERIVATION):
                parse_bip32_derivation(record.key, record.value)

        output_total = 0
        for index, tx_output in enumerate(transaction.outputs):
            if tx_output.value < 0 or tx_output.value > MAX_MONEY:
                raise PSBTError(f"PSBT output {index} amount is outside Bitcoin's money range")
            output_total += tx_output.value
        input_total = sum(witness_utxo.value for witness_utxo in witness_utxos)
        if output_total > input_total:
            raise PSBTError(
                f"PSBT outputs exceed inputs by {output_total - input_total:,} satoshis"
            )

        plans: list[PSBTInputPlan] = []
        unresolved_regular: dict[bytes, list[int]] = {}
        for index, (input_map, witness_utxo, input_type) in enumerate(
            zip(psbt.input_maps, witness_utxos, input_types, strict=True)
        ):
            finalized = _is_finalized(input_map)
            plan: PSBTInputPlan | None = None
            if input_type == "p2wpkh":
                plan = self._plan_regular_input_from_origins(
                    psbt, index, input_map, witness_utxo, finalized
                )
                if plan is None:
                    unresolved_regular.setdefault(witness_utxo.script_pubkey, []).append(index)
            elif input_type == "p2wsh":
                plan = self._plan_fidelity_bond_input(
                    psbt, index, input_map, witness_utxo, finalized
                )

            plans.append(
                plan
                or PSBTInputPlan(
                    index=index,
                    witness_utxo=witness_utxo,
                    input_type=input_type,
                    finalized=finalized,
                )
            )

        if unresolved_regular and scan_range:
            self._resolve_regular_inputs_by_scan(psbt, plans, unresolved_regular, scan_range)

        for plan, sighash_type in zip(plans, sighash_types, strict=True):
            if plan.owned and not plan.finalized and sighash_type != 1:
                raise PSBTError(
                    f"PSBT input {plan.index} requests unsupported sighash type "
                    f"{sighash_type:#x}; only SIGHASH_ALL (0x01) is allowed"
                )

        output_types = [_classify_output_script(output.script) for output in transaction.outputs]
        if "unknown" in output_types:
            unknown_index = output_types.index("unknown")
            raise PSBTError(
                f"PSBT output {unknown_index} has an unsupported script type; cannot calculate "
                "a reliable fee rate"
            )
        estimated_vsize = estimate_vsize(
            ["p2wsh" if kind == "p2wsh" else "p2wpkh" for kind in input_types],
            output_types,
        )
        # estimate_vsize assumes one-byte input/output counts. Account for the
        # larger CompactSize encodings used at 253 and above.
        estimated_vsize += len(encode_varint(len(transaction.inputs))) - 1
        estimated_vsize += len(encode_varint(len(transaction.outputs))) - 1
        fee_rate_is_upper_bound = any(
            plan.input_type == "p2wsh" and not plan.owned for plan in plans
        )
        if fee_rate_is_upper_bound:
            # An arbitrary witness script does not reveal its eventual witness
            # size. The stripped transaction is a lower bound on final vsize,
            # so fee / this size is a conservative upper bound for the fee cap.
            estimated_vsize = len(psbt.unsigned_tx)
        return PSBTSigningPlan(
            psbt=psbt,
            inputs=tuple(plans),
            fee=input_total - output_total,
            estimated_vsize=estimated_vsize,
            source_psbt=bytes(psbt_bytes),
            scan_range=scan_range,
            fee_rate_is_upper_bound=fee_rate_is_upper_bound,
        )

    def sign_psbt(self, plan: PSBTSigningPlan) -> PSBTSigningResult:
        """Add partial signatures for every signable input in a reviewed plan."""
        current_unsigned_tx = serialize_transaction(
            plan.psbt.transaction.version,
            plan.psbt.transaction.inputs,
            plan.psbt.transaction.outputs,
            plan.psbt.transaction.locktime,
        )
        if (
            plan.psbt.serialize() != plan.source_psbt
            or current_unsigned_tx != plan.psbt.unsigned_tx
        ):
            raise TransactionSigningError("PSBT changed after review; refusing to sign")

        # Rebuild from immutable source bytes so nested plan objects cannot be
        # altered between review and private-key use.
        signing_plan = self.prepare_psbt_signing(plan.source_psbt, plan.scan_range)
        signed_indices: list[int] = []
        already_signed_indices: list[int] = []
        for input_plan in signing_plan.inputs:
            if input_plan.already_signed:
                already_signed_indices.append(input_plan.index)
                continue
            if not input_plan.signable:
                continue
            if input_plan.utxo is None or input_plan.pubkey is None:
                raise TransactionSigningError(
                    f"Incomplete signing plan for PSBT input {input_plan.index}"
                )
            signed = self.sign_input(
                signing_plan.psbt.transaction, input_plan.index, input_plan.utxo
            )
            if signed.pubkey != input_plan.pubkey:
                raise TransactionSigningError(
                    f"Signing key changed after reviewing PSBT input {input_plan.index}"
                )
            signing_plan.psbt.append_input_key_value(
                input_plan.index,
                bytes([PSBT_IN_PARTIAL_SIG]) + signed.pubkey,
                signed.signature,
            )
            signed_indices.append(input_plan.index)

        if signed_indices and signing_plan.psbt.version == 2:
            # All signatures produced here are SIGHASH_ALL without ANYONECANPAY.
            # BIP370 requires disabling input/output modification after signing.
            records = signing_plan.psbt.global_map.records
            for index, record in enumerate(records):
                if record.key == bytes([PSBT_GLOBAL_TX_MODIFIABLE]):
                    records[index] = PSBTKeyValue(
                        key=record.key,
                        value=bytes(
                            [record.value[0] & ~(TX_MODIFIABLE_INPUTS | TX_MODIFIABLE_OUTPUTS)]
                        ),
                    )

        return PSBTSigningResult(
            psbt=signing_plan.psbt.serialize(),
            signed_indices=tuple(signed_indices),
            already_signed_indices=tuple(already_signed_indices),
        )

    def _plan_regular_input_from_origins(
        self,
        psbt: ParsedPSBT,
        index: int,
        input_map: PSBTMap,
        witness_utxo: WitnessUTXO,
        finalized: bool,
    ) -> PSBTInputPlan | None:
        for record in _records_for_type(input_map, PSBT_IN_BIP32_DERIVATION):
            origin = parse_bip32_derivation(record.key, record.value)
            path = self._normal_wallet_path(origin.fingerprint, origin.path)
            if path is None:
                continue
            mixdepth, change, address_index = path
            address = self.get_address(mixdepth, change, address_index)
            key = self.get_key_for_address(address)
            if key is None:
                continue
            pubkey = key.get_public_key_bytes(compressed=True)
            if pubkey != origin.pubkey:
                continue
            if pubkey_to_p2wpkh_script(pubkey) != witness_utxo.script_pubkey:
                continue
            return self._owned_regular_plan(
                psbt,
                index,
                input_map,
                witness_utxo,
                address,
                mixdepth,
                change,
                address_index,
                finalized,
            )
        return None

    def _plan_fidelity_bond_input(
        self,
        psbt: ParsedPSBT,
        index: int,
        input_map: PSBTMap,
        witness_utxo: WitnessUTXO,
        finalized: bool,
    ) -> PSBTInputPlan | None:
        witness_script_record = _record_for_type(input_map, PSBT_IN_WITNESS_SCRIPT)
        if witness_script_record is None:
            return None
        witness_script = witness_script_record.value
        if b"\x00\x20" + sha256(witness_script).digest() != witness_utxo.script_pubkey:
            raise PSBTError(
                f"PSBT input {index} witness_script does not match its P2WSH witness_utxo"
            )
        try:
            locktime, script_pubkey = parse_freeze_script(witness_script)
            timenumber = timestamp_to_timenumber(locktime)
        except ValueError:
            return None

        address = script_to_p2wsh_address(witness_script, self.network)
        path = self.get_fidelity_bond_path(timenumber)
        key = self.master_key.derive(path)
        pubkey = key.get_public_key_bytes(compressed=True)
        if pubkey != script_pubkey:
            return None
        self.address_cache[address] = (0, 2, timenumber)
        self.fidelity_bond_locktime_cache[address] = locktime

        tx_input = psbt.transaction.inputs[index]
        if psbt.transaction.locktime < locktime:
            raise PSBTError(
                f"PSBT transaction locktime {psbt.transaction.locktime} is below fidelity bond "
                f"input {index} locktime {locktime}"
            )
        if tx_input.sequence == 0xFFFFFFFF:
            raise PSBTError(
                f"PSBT fidelity bond input {index} has a final sequence and cannot satisfy CLTV"
            )

        utxo = UTXOInfo(
            txid=tx_input.txid,
            vout=tx_input.vout,
            value=witness_utxo.value,
            address=address,
            confirmations=0,
            scriptpubkey=witness_utxo.script_pubkey.hex(),
            path=path,
            mixdepth=0,
            locktime=locktime,
        )
        already_signed = _validate_existing_signature(
            psbt,
            index,
            input_map,
            witness_utxo,
            pubkey,
            witness_script=witness_script,
        )
        return PSBTInputPlan(
            index=index,
            witness_utxo=witness_utxo,
            input_type="p2wsh",
            utxo=utxo,
            pubkey=pubkey,
            wallet_input_type="fidelity-bond",
            already_signed=already_signed,
            finalized=finalized,
        )

    def _resolve_regular_inputs_by_scan(
        self,
        psbt: ParsedPSBT,
        plans: list[PSBTInputPlan],
        unresolved: dict[bytes, list[int]],
        scan_range: int,
    ) -> None:
        for mixdepth in range(self.mixdepth_count):
            for change in (0, 1):
                for address_index in range(scan_range):
                    address = self.get_address(mixdepth, change, address_index)
                    key = self.get_key_for_address(address)
                    if key is None:
                        continue
                    pubkey = key.get_public_key_bytes(compressed=True)
                    script_pubkey = pubkey_to_p2wpkh_script(pubkey)
                    indices = unresolved.pop(script_pubkey, None)
                    if indices is None:
                        continue
                    for index in indices:
                        input_map = psbt.input_maps[index]
                        plans[index] = self._owned_regular_plan(
                            psbt,
                            index,
                            input_map,
                            plans[index].witness_utxo,
                            address,
                            mixdepth,
                            change,
                            address_index,
                            plans[index].finalized,
                        )
                    if not unresolved:
                        return

    def _owned_regular_plan(
        self,
        psbt: ParsedPSBT,
        index: int,
        input_map: PSBTMap,
        witness_utxo: WitnessUTXO,
        address: str,
        mixdepth: int,
        change: int,
        address_index: int,
        finalized: bool,
    ) -> PSBTInputPlan:
        key = self.get_key_for_address(address)
        if key is None:
            raise PSBTError(f"Wallet key disappeared while reviewing PSBT input {index}")
        pubkey = key.get_public_key_bytes(compressed=True)
        tx_input = psbt.transaction.inputs[index]
        utxo = UTXOInfo(
            txid=tx_input.txid,
            vout=tx_input.vout,
            value=witness_utxo.value,
            address=address,
            confirmations=0,
            scriptpubkey=witness_utxo.script_pubkey.hex(),
            path=f"{self.root_path}/{mixdepth}'/{change}/{address_index}",
            mixdepth=mixdepth,
        )
        already_signed = _validate_existing_signature(psbt, index, input_map, witness_utxo, pubkey)
        return PSBTInputPlan(
            index=index,
            witness_utxo=witness_utxo,
            input_type="p2wpkh",
            utxo=utxo,
            pubkey=pubkey,
            wallet_input_type="regular",
            already_signed=already_signed,
            finalized=finalized,
        )

    def _normal_wallet_path(
        self, fingerprint: bytes, path: tuple[int, ...]
    ) -> tuple[int, int, int] | None:
        coin_type = 0 if self.network == "mainnet" else 1
        if fingerprint != self.master_key.fingerprint or len(path) != 5:
            return None
        purpose, coin, account, change, address_index = path
        if purpose != 84 | HARDENED or coin != coin_type | HARDENED:
            return None
        if account < HARDENED or account - HARDENED >= self.mixdepth_count:
            return None
        if change not in (0, 1) or address_index >= MAX_DERIVATION_INDEX:
            return None
        return account - HARDENED, change, address_index


def _record_for_type(psbt_map: PSBTMap, key_type: int) -> PSBTKeyValue | None:
    records = _records_for_type(psbt_map, key_type)
    if not records:
        return None
    if len(records) != 1:
        raise PSBTError(f"PSBT map contains multiple records for singleton type {key_type:#x}")
    return records[0]


def _records_for_type(psbt_map: PSBTMap, key_type: int) -> list[PSBTKeyValue]:
    return [record for record in psbt_map.records if record.key[0] == key_type]


def _parse_sighash_type(input_map: PSBTMap, index: int) -> int:
    record = _record_for_type(input_map, PSBT_IN_SIGHASH_TYPE)
    if record is None:
        return 1
    if len(record.value) != 4:
        raise PSBTError(f"PSBT input {index} sighash type must be a 4-byte uint32")
    return int.from_bytes(record.value, "little")


def _is_finalized(input_map: PSBTMap) -> bool:
    return bool(
        _records_for_type(input_map, PSBT_IN_FINAL_SCRIPTSIG)
        or _records_for_type(input_map, PSBT_IN_FINAL_SCRIPTWITNESS)
    )


def _classify_input_script(script: bytes) -> PSBTInputType:
    if len(script) == 22 and script.startswith(b"\x00\x14"):
        return "p2wpkh"
    if len(script) == 34 and script.startswith(b"\x00\x20"):
        return "p2wsh"
    return "unsupported"


def _classify_output_script(script: bytes) -> str:
    if len(script) == 22 and script.startswith(b"\x00\x14"):
        return "p2wpkh"
    if len(script) == 34 and script.startswith(b"\x00\x20"):
        return "p2wsh"
    if len(script) == 34 and script.startswith(b"\x51\x20"):
        return "p2tr"
    if len(script) == 25 and script.startswith(b"\x76\xa9\x14") and script.endswith(b"\x88\xac"):
        return "p2pkh"
    if len(script) == 23 and script.startswith(b"\xa9\x14") and script.endswith(b"\x87"):
        return "p2sh"
    return "unknown"


def _validate_existing_signature(
    psbt: ParsedPSBT,
    index: int,
    input_map: PSBTMap,
    witness_utxo: WitnessUTXO,
    pubkey: bytes,
    *,
    witness_script: bytes | None = None,
) -> bool:
    signature_record = next(
        (
            record
            for record in _records_for_type(input_map, PSBT_IN_PARTIAL_SIG)
            if record.key == bytes([PSBT_IN_PARTIAL_SIG]) + pubkey
        ),
        None,
    )
    if signature_record is None:
        return False
    signature = signature_record.value
    if not signature or signature[-1] != 1:
        raise PSBTError(
            f"PSBT input {index} contains an invalid existing signature for this wallet"
        )
    if witness_script is None:
        valid = verify_p2wpkh_signature(
            psbt.transaction,
            index,
            create_p2wpkh_script_code(pubkey),
            witness_utxo.value,
            signature,
            pubkey,
        )
    else:
        valid = verify_p2wsh_signature(
            psbt.transaction,
            index,
            witness_script,
            witness_utxo.value,
            signature,
            pubkey,
        )
    if not valid:
        raise PSBTError(
            f"PSBT input {index} contains an invalid existing signature for this wallet"
        )
    return True
