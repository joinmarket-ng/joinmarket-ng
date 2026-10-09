"""Tests for the channel-buyout escrow transaction primitives.

Every signature in this module is produced and checked with real secp256k1
operations (MuSig2 for key-path spends, BIP340 for the claim leaf). Passing here
means the artifacts are internally consistent and cryptographically valid; it
does not prove consensus or policy acceptance by a Bitcoin node.
"""

from __future__ import annotations

import hashlib

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import parse_transaction_bytes, tagged_hash, taproot_tweak_pubkey
from jmcore.musig2 import nonce_agg, sign_partial
from jmcore.taproot import (
    LEAF_VERSION_TAPSCRIPT,
    TaprootTx,
    TxIn,
    TxOut,
    tapleaf_hash,
    to_xonly,
)

from jmswap.bitcoin_escrow import (
    MAX_CSV_DELAY,
    MIN_CSV_DELAY,
    MIN_SPLIT_OUTPUT_SATS,
    SWEEP_SEQUENCE,
    BuyoutEscrow,
    ChannelBuyoutEscrowError,
    EscrowOutpoint,
    build_claim,
    build_cooperative_sweep,
    build_split,
    escrow_nonce,
    escrow_session,
    finalize_key_path_spend,
    verify_signed_claim,
    verify_signed_cooperative_sweep,
    verify_signed_split,
)

BUYER_ESCROW_SECRET = bytes.fromhex(
    "1111111111111111111111111111111111111111111111111111111111111111"
)
COUNTERPARTY_ESCROW_SECRET = bytes.fromhex(
    "2222222222222222222222222222222222222222222222222222222222222222"
)
BUYER_CLAIM_SECRET = bytes.fromhex(
    "3333333333333333333333333333333333333333333333333333333333333333"
)
PREIMAGE = bytes.fromhex("4444444444444444444444444444444444444444444444444444444444444444")
PAYMENT_HASH = hashlib.sha256(PREIMAGE).digest()

PARENT_TXID = "a" * 64
ESCROW_VALUE = 2_000_000
COUNTERPARTY_VALUE = 700_000
SPLIT_FEE = 1_000
CSV_DELAY = 432


def _pubkey(secret: bytes) -> bytes:
    return bytes(CKey(secret).pub)


def _p2tr(secret: bytes) -> bytes:
    """A destination P2TR script derived from a throwaway key-path-only key."""
    _, output_key = taproot_tweak_pubkey(bytes(CKey(secret).xonly_pub))
    return bytes([0x51, 0x20]) + output_key


BUYER_SPLIT_SCRIPT = _p2tr(b"\x51" * 32)
COUNTERPARTY_SPLIT_SCRIPT = _p2tr(b"\x52" * 32)
BUYER_SWEEP_SCRIPT = _p2tr(b"\x53" * 32)


@pytest.fixture
def escrow() -> BuyoutEscrow:
    return BuyoutEscrow(
        buyer_escrow_pubkey=_pubkey(BUYER_ESCROW_SECRET),
        counterparty_escrow_pubkey=_pubkey(COUNTERPARTY_ESCROW_SECRET),
        buyer_claim_pubkey=_pubkey(BUYER_CLAIM_SECRET),
        payment_hash=PAYMENT_HASH,
    )


@pytest.fixture
def outpoint(escrow: BuyoutEscrow) -> EscrowOutpoint:
    return EscrowOutpoint(
        txid=PARENT_TXID, vout=1, value=ESCROW_VALUE, scriptpubkey=escrow.output_script()
    )


def _sign_key_path(escrow: BuyoutEscrow, spend, secrets: tuple[bytes, bytes]) -> bytes:
    """Run a real two-party MuSig2 session and return the serialized signed spend."""
    buyer_secret, counterparty_secret = secrets
    buyer_secnonce, buyer_pubnonce = escrow_nonce(
        escrow.buyer_escrow_pubkey, b"\x0a" * 32, privkey=buyer_secret, sighash=spend.sighash
    )
    cp_secnonce, cp_pubnonce = escrow_nonce(
        escrow.counterparty_escrow_pubkey,
        b"\x0b" * 32,
        privkey=counterparty_secret,
        sighash=spend.sighash,
    )
    session = escrow_session(escrow, nonce_agg([buyer_pubnonce, cp_pubnonce]), spend.sighash)
    buyer_partial = sign_partial(buyer_secnonce, buyer_secret, session)
    cp_partial = sign_partial(cp_secnonce, counterparty_secret, session)
    return finalize_key_path_spend(
        escrow, spend, buyer_pubnonce, cp_pubnonce, buyer_partial, cp_partial
    )


def _signed_split(escrow: BuyoutEscrow, outpoint: EscrowOutpoint) -> bytes:
    spend = build_split(
        escrow,
        outpoint,
        BUYER_SPLIT_SCRIPT,
        COUNTERPARTY_SPLIT_SCRIPT,
        COUNTERPARTY_VALUE,
        SPLIT_FEE,
        CSV_DELAY,
    )
    return _sign_key_path(escrow, spend, (BUYER_ESCROW_SECRET, COUNTERPARTY_ESCROW_SECRET))


def _verify_split(escrow: BuyoutEscrow, outpoint: EscrowOutpoint, signed_tx: bytes) -> bool:
    return verify_signed_split(
        escrow,
        outpoint,
        BUYER_SPLIT_SCRIPT,
        COUNTERPARTY_SPLIT_SCRIPT,
        COUNTERPARTY_VALUE,
        SPLIT_FEE,
        CSV_DELAY,
        signed_tx,
    )


def _reserialize(signed_tx: bytes, mutate) -> bytes:
    """Re-serialize a signed transaction after applying a structural mutation."""
    parsed = parse_transaction_bytes(signed_tx)
    tx = TaprootTx(
        inputs=[TxIn(txid=inp.txid, vout=inp.vout, sequence=inp.sequence) for inp in parsed.inputs],
        outputs=[TxOut(value=out.value, scriptpubkey=out.script) for out in parsed.outputs],
        version=parsed.version,
        locktime=parsed.locktime,
        witnesses=[list(stack) for stack in parsed.witnesses],
    )
    mutate(tx)
    return tx.serialize()


# --------------------------------------------------------------------------- #
# Escrow tree
# --------------------------------------------------------------------------- #


class TestEscrowTree:
    def test_claim_leaf_is_the_agreed_script(self, escrow: BuyoutEscrow) -> None:
        expected = (
            bytes([0xA8])
            + bytes([32])
            + PAYMENT_HASH
            + bytes([0x88])
            + bytes([32])
            + to_xonly(_pubkey(BUYER_CLAIM_SECRET))
            + bytes([0xAC])
        )
        assert escrow.claim_leaf == expected
        assert escrow.tap_tree == (LEAF_VERSION_TAPSCRIPT, expected)

    def test_output_key_is_the_taptweaked_musig2_aggregate(self, escrow: BuyoutEscrow) -> None:
        merkle_root = tapleaf_hash(escrow.claim_leaf)
        assert escrow.merkle_root() == merkle_root
        assert escrow.taptweak_scalar() == tagged_hash(
            "TapTweak", escrow.internal_xonly() + merkle_root
        )
        parity, output_key = taproot_tweak_pubkey(escrow.internal_xonly(), merkle_root)
        assert escrow.output_xonly() == output_key
        assert escrow.output_script() == bytes([0x51, 0x20]) + output_key
        leaf, control = escrow.claim_control_block()
        assert leaf == escrow.claim_leaf
        assert control == bytes([LEAF_VERSION_TAPSCRIPT | parity]) + escrow.internal_xonly()

    def test_key_order_is_role_bound(self, escrow: BuyoutEscrow) -> None:
        swapped = BuyoutEscrow(
            buyer_escrow_pubkey=escrow.counterparty_escrow_pubkey,
            counterparty_escrow_pubkey=escrow.buyer_escrow_pubkey,
            buyer_claim_pubkey=escrow.buyer_claim_pubkey,
            payment_hash=PAYMENT_HASH,
        )
        assert swapped.internal_xonly() != escrow.internal_xonly()

    @pytest.mark.parametrize(
        "bad_key",
        [
            b"",
            b"\x02" * 32,
            b"\x04" + b"\x11" * 32,
            bytes([0x02]) + b"\xff" * 32,
            bytearray(_pubkey(BUYER_ESCROW_SECRET)),
            _pubkey(BUYER_ESCROW_SECRET).hex(),
        ],
    )
    @pytest.mark.parametrize(
        "role", ["buyer_escrow_pubkey", "counterparty_escrow_pubkey", "buyer_claim_pubkey"]
    )
    def test_malformed_keys_are_rejected(self, role: str, bad_key: object) -> None:
        fields: dict[str, object] = {
            "buyer_escrow_pubkey": _pubkey(BUYER_ESCROW_SECRET),
            "counterparty_escrow_pubkey": _pubkey(COUNTERPARTY_ESCROW_SECRET),
            "buyer_claim_pubkey": _pubkey(BUYER_CLAIM_SECRET),
            "payment_hash": PAYMENT_HASH,
        }
        fields[role] = bad_key
        with pytest.raises(ChannelBuyoutEscrowError, match="key"):
            BuyoutEscrow(**fields)  # type: ignore[arg-type]

    def test_reused_keys_are_rejected(self) -> None:
        with pytest.raises(ChannelBuyoutEscrowError, match="distinct"):
            BuyoutEscrow(
                buyer_escrow_pubkey=_pubkey(BUYER_ESCROW_SECRET),
                counterparty_escrow_pubkey=_pubkey(COUNTERPARTY_ESCROW_SECRET),
                buyer_claim_pubkey=_pubkey(BUYER_ESCROW_SECRET),
                payment_hash=PAYMENT_HASH,
            )

    def test_opposite_parity_of_the_same_point_is_rejected(self) -> None:
        """A negated key shares its x-only form, so the claim leaf would reuse a signing key."""
        buyer = _pubkey(BUYER_ESCROW_SECRET)
        negated = bytes([0x05 - buyer[0]]) + buyer[1:]
        assert negated != buyer
        assert to_xonly(negated) == to_xonly(buyer)
        with pytest.raises(ChannelBuyoutEscrowError, match="x-only"):
            BuyoutEscrow(
                buyer_escrow_pubkey=buyer,
                counterparty_escrow_pubkey=_pubkey(COUNTERPARTY_ESCROW_SECRET),
                buyer_claim_pubkey=negated,
                payment_hash=PAYMENT_HASH,
            )
        with pytest.raises(ChannelBuyoutEscrowError, match="x-only"):
            BuyoutEscrow(
                buyer_escrow_pubkey=buyer,
                counterparty_escrow_pubkey=negated,
                buyer_claim_pubkey=_pubkey(BUYER_CLAIM_SECRET),
                payment_hash=PAYMENT_HASH,
            )

    @pytest.mark.parametrize("bad_hash", [b"", b"\x00" * 31, b"\x00" * 33, PAYMENT_HASH.hex()])
    def test_malformed_payment_hash_is_rejected(self, bad_hash: object) -> None:
        with pytest.raises(ChannelBuyoutEscrowError, match="payment hash"):
            BuyoutEscrow(
                buyer_escrow_pubkey=_pubkey(BUYER_ESCROW_SECRET),
                counterparty_escrow_pubkey=_pubkey(COUNTERPARTY_ESCROW_SECRET),
                buyer_claim_pubkey=_pubkey(BUYER_CLAIM_SECRET),
                payment_hash=bad_hash,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"txid": "zz" * 32},
            {"txid": "aa" * 31},
            {"txid": "aa" * 33},
            {"txid": PARENT_TXID.upper()},
            {"txid": "Aa" + PARENT_TXID[2:]},
            {"txid": " " + PARENT_TXID[1:]},
            {"txid": PARENT_TXID[:-1] + "\n"},
            {"txid": "0x" + PARENT_TXID[2:]},
            {"txid": bytes.fromhex(PARENT_TXID)},
            {"vout": -1},
            {"vout": True},
            {"value": 0},
            {"value": -1},
            {"value": True},
            {"scriptpubkey": b"\x00\x14" + b"\x11" * 20},
        ],
    )
    def test_malformed_outpoints_are_rejected(
        self, escrow: BuyoutEscrow, kwargs: dict[str, object]
    ) -> None:
        fields: dict[str, object] = {
            "txid": PARENT_TXID,
            "vout": 1,
            "value": ESCROW_VALUE,
            "scriptpubkey": escrow.output_script(),
        }
        fields.update(kwargs)
        with pytest.raises(ChannelBuyoutEscrowError):
            EscrowOutpoint(**fields)  # type: ignore[arg-type]

    def test_outpoint_must_pay_to_the_escrow_script(self, escrow: BuyoutEscrow) -> None:
        foreign = EscrowOutpoint(
            txid=PARENT_TXID, vout=1, value=ESCROW_VALUE, scriptpubkey=BUYER_SPLIT_SCRIPT
        )
        with pytest.raises(ChannelBuyoutEscrowError, match="agreed escrow script"):
            build_split(
                escrow,
                foreign,
                BUYER_SPLIT_SCRIPT,
                COUNTERPARTY_SPLIT_SCRIPT,
                COUNTERPARTY_VALUE,
                SPLIT_FEE,
                CSV_DELAY,
            )


# --------------------------------------------------------------------------- #
# Split construction
# --------------------------------------------------------------------------- #


class TestSplitConstruction:
    def test_split_shape_is_deterministic(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        spend = build_split(
            escrow,
            outpoint,
            BUYER_SPLIT_SCRIPT,
            COUNTERPARTY_SPLIT_SCRIPT,
            COUNTERPARTY_VALUE,
            SPLIT_FEE,
            CSV_DELAY,
        )
        tx = spend.tx
        assert tx.version == 2
        assert tx.locktime == 0
        assert len(tx.inputs) == 1
        assert tx.inputs[0].txid == PARENT_TXID
        assert tx.inputs[0].vout == 1
        assert tx.inputs[0].sequence == CSV_DELAY
        assert [out.scriptpubkey for out in tx.outputs] == [
            BUYER_SPLIT_SCRIPT,
            COUNTERPARTY_SPLIT_SCRIPT,
        ]
        assert tx.outputs[0].value == ESCROW_VALUE - COUNTERPARTY_VALUE - SPLIT_FEE
        assert tx.outputs[1].value == COUNTERPARTY_VALUE
        assert sum(out.value for out in tx.outputs) + SPLIT_FEE == ESCROW_VALUE
        assert len(spend.sighash) == 32
        assert spend.outpoint == outpoint
        assert (
            build_split(
                escrow,
                outpoint,
                BUYER_SPLIT_SCRIPT,
                COUNTERPARTY_SPLIT_SCRIPT,
                COUNTERPARTY_VALUE,
                SPLIT_FEE,
                CSV_DELAY,
            ).sighash
            == spend.sighash
        )

    @pytest.mark.parametrize("csv_delay", [MIN_CSV_DELAY, 432, MAX_CSV_DELAY])
    def test_accepted_relative_locks(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint, csv_delay: int
    ) -> None:
        spend = build_split(
            escrow,
            outpoint,
            BUYER_SPLIT_SCRIPT,
            COUNTERPARTY_SPLIT_SCRIPT,
            COUNTERPARTY_VALUE,
            SPLIT_FEE,
            csv_delay,
        )
        sequence = spend.tx.inputs[0].sequence
        assert sequence == csv_delay
        assert sequence & (1 << 31) == 0, "relative lock must stay enabled"
        assert sequence & (1 << 22) == 0, "relative lock must be block based"

    @pytest.mark.parametrize(
        "csv_delay", [0, 1, MIN_CSV_DELAY - 1, MAX_CSV_DELAY + 1, -144, True, 432.0, "432"]
    )
    def test_rejected_relative_locks(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint, csv_delay: object
    ) -> None:
        with pytest.raises(ChannelBuyoutEscrowError, match="csv delay"):
            build_split(
                escrow,
                outpoint,
                BUYER_SPLIT_SCRIPT,
                COUNTERPARTY_SPLIT_SCRIPT,
                COUNTERPARTY_VALUE,
                SPLIT_FEE,
                csv_delay,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize(
        ("counterparty_value", "fee", "match"),
        [
            (MIN_SPLIT_OUTPUT_SATS - 1, SPLIT_FEE, "at least"),
            (ESCROW_VALUE - SPLIT_FEE, SPLIT_FEE, "at least"),
            (ESCROW_VALUE, SPLIT_FEE, "at least"),
            (COUNTERPARTY_VALUE, -1, "between 0"),
            (-1, SPLIT_FEE, "between 0"),
            (True, SPLIT_FEE, "between 0"),
            (COUNTERPARTY_VALUE, True, "between 0"),
            (700_000.0, SPLIT_FEE, "between 0"),
            (COUNTERPARTY_VALUE, 21_000_000 * 100_000_000 + 1, "between 0"),
        ],
    )
    def test_rejected_amounts(
        self,
        escrow: BuyoutEscrow,
        outpoint: EscrowOutpoint,
        counterparty_value: object,
        fee: object,
        match: str,
    ) -> None:
        with pytest.raises(ChannelBuyoutEscrowError, match=match):
            build_split(
                escrow,
                outpoint,
                BUYER_SPLIT_SCRIPT,
                COUNTERPARTY_SPLIT_SCRIPT,
                counterparty_value,  # type: ignore[arg-type]
                fee,  # type: ignore[arg-type]
                CSV_DELAY,
            )

    @pytest.mark.parametrize(
        "script",
        [b"", b"\x00\x14" + b"\x11" * 20, b"\x51\x20" + b"\x11" * 31, b"\x52\x20" + b"\x11" * 32],
    )
    def test_non_p2tr_split_outputs_are_rejected(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint, script: bytes
    ) -> None:
        with pytest.raises(ChannelBuyoutEscrowError, match="P2TR"):
            build_split(
                escrow,
                outpoint,
                script,
                COUNTERPARTY_SPLIT_SCRIPT,
                COUNTERPARTY_VALUE,
                SPLIT_FEE,
                CSV_DELAY,
            )
        with pytest.raises(ChannelBuyoutEscrowError, match="P2TR"):
            build_split(
                escrow,
                outpoint,
                BUYER_SPLIT_SCRIPT,
                script,
                COUNTERPARTY_VALUE,
                SPLIT_FEE,
                CSV_DELAY,
            )


# --------------------------------------------------------------------------- #
# Split signing and verification
# --------------------------------------------------------------------------- #


class TestEscrowNonce:
    """The nonce must be bound to the signing key and to the message being signed."""

    def _split(self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint):
        return build_split(
            escrow,
            outpoint,
            BUYER_SPLIT_SCRIPT,
            COUNTERPARTY_SPLIT_SCRIPT,
            COUNTERPARTY_VALUE,
            SPLIT_FEE,
            CSV_DELAY,
        )

    def test_key_and_message_are_keyword_only_and_required(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        spend = self._split(escrow, outpoint)
        with pytest.raises(TypeError):
            escrow_nonce(escrow.buyer_escrow_pubkey, b"\x0a" * 32)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            escrow_nonce(  # type: ignore[call-arg]
                escrow.buyer_escrow_pubkey, b"\x0a" * 32, privkey=BUYER_ESCROW_SECRET
            )
        with pytest.raises(TypeError):
            escrow_nonce(  # type: ignore[misc]
                escrow.buyer_escrow_pubkey,
                b"\x0a" * 32,
                BUYER_ESCROW_SECRET,
                spend.sighash,
            )

    def test_same_seed_with_different_messages_gives_different_nonces(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        """Message binding is defense in depth against an accidentally repeated seed.

        It is never a licence to reuse ``rand``: a repeated seed with the same key
        and the same message still repeats the nonce, as the companion test below
        shows, and that leaks the private key.
        """
        split = self._split(escrow, outpoint)
        sweep = build_cooperative_sweep(escrow, outpoint, BUYER_SWEEP_SCRIPT, 400)
        assert split.sighash != sweep.sighash
        _, split_pubnonce = escrow_nonce(
            escrow.buyer_escrow_pubkey,
            b"\x0a" * 32,
            privkey=BUYER_ESCROW_SECRET,
            sighash=split.sighash,
        )
        _, sweep_pubnonce = escrow_nonce(
            escrow.buyer_escrow_pubkey,
            b"\x0a" * 32,
            privkey=BUYER_ESCROW_SECRET,
            sighash=sweep.sighash,
        )
        assert split_pubnonce != sweep_pubnonce

    def test_same_seed_key_and_message_repeats_the_nonce(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        """Documents the hazard: reusing ``rand`` for one key and message repeats the nonce."""
        spend = self._split(escrow, outpoint)
        _, first = escrow_nonce(
            escrow.buyer_escrow_pubkey,
            b"\x0a" * 32,
            privkey=BUYER_ESCROW_SECRET,
            sighash=spend.sighash,
        )
        _, second = escrow_nonce(
            escrow.buyer_escrow_pubkey,
            b"\x0a" * 32,
            privkey=BUYER_ESCROW_SECRET,
            sighash=spend.sighash,
        )
        assert first == second, "callers MUST supply fresh randomness for every nonce"

    def test_different_seeds_give_different_nonces(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        spend = self._split(escrow, outpoint)
        _, first = escrow_nonce(
            escrow.buyer_escrow_pubkey,
            b"\x0a" * 32,
            privkey=BUYER_ESCROW_SECRET,
            sighash=spend.sighash,
        )
        _, second = escrow_nonce(
            escrow.buyer_escrow_pubkey,
            b"\x0c" * 32,
            privkey=BUYER_ESCROW_SECRET,
            sighash=spend.sighash,
        )
        assert first != second

    def test_key_mismatch_is_rejected(self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint) -> None:
        spend = self._split(escrow, outpoint)
        with pytest.raises(ChannelBuyoutEscrowError, match="does not correspond"):
            escrow_nonce(
                escrow.buyer_escrow_pubkey,
                b"\x0a" * 32,
                privkey=COUNTERPARTY_ESCROW_SECRET,
                sighash=spend.sighash,
            )

    @pytest.mark.parametrize(
        "privkey",
        [b"", b"\x00" * 31, b"\x00" * 33, BUYER_ESCROW_SECRET.hex(), bytearray(b"\x11" * 32)],
    )
    def test_malformed_private_keys_are_rejected(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint, privkey: object
    ) -> None:
        spend = self._split(escrow, outpoint)
        with pytest.raises(ChannelBuyoutEscrowError, match="private key must be exactly 32 bytes"):
            escrow_nonce(
                escrow.buyer_escrow_pubkey,
                b"\x0a" * 32,
                privkey=privkey,  # type: ignore[arg-type]
                sighash=spend.sighash,
            )

    def test_invalid_scalar_is_rejected(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        spend = self._split(escrow, outpoint)
        with pytest.raises(ChannelBuyoutEscrowError, match="could not generate an escrow nonce"):
            escrow_nonce(
                escrow.buyer_escrow_pubkey,
                b"\x0a" * 32,
                privkey=b"\x00" * 32,
                sighash=spend.sighash,
            )

    @pytest.mark.parametrize("sighash", [b"", b"\x00" * 31, b"\x00" * 33, "aa" * 32])
    def test_malformed_messages_are_rejected(self, escrow: BuyoutEscrow, sighash: object) -> None:
        with pytest.raises(ChannelBuyoutEscrowError, match="sighash must be exactly 32 bytes"):
            escrow_nonce(
                escrow.buyer_escrow_pubkey,
                b"\x0a" * 32,
                privkey=BUYER_ESCROW_SECRET,
                sighash=sighash,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("rand", [b"", b"\x0a" * 31, b"\x0a" * 33, "0a" * 32])
    def test_malformed_randomness_is_rejected(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint, rand: object
    ) -> None:
        spend = self._split(escrow, outpoint)
        with pytest.raises(ChannelBuyoutEscrowError, match="nonce randomness"):
            escrow_nonce(
                escrow.buyer_escrow_pubkey,
                rand,  # type: ignore[arg-type]
                privkey=BUYER_ESCROW_SECRET,
                sighash=spend.sighash,
            )

    def test_omitted_randomness_still_produces_a_usable_nonce(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        spend = self._split(escrow, outpoint)
        _, first = escrow_nonce(
            escrow.buyer_escrow_pubkey, privkey=BUYER_ESCROW_SECRET, sighash=spend.sighash
        )
        _, second = escrow_nonce(
            escrow.buyer_escrow_pubkey, privkey=BUYER_ESCROW_SECRET, sighash=spend.sighash
        )
        assert len(first) == len(second) == 66
        assert first != second, "operating-system randomness must not repeat"


class TestSplitSigning:
    def test_musig2_split_round_trip(self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint) -> None:
        signed_tx = _signed_split(escrow, outpoint)
        assert _verify_split(escrow, outpoint, signed_tx) is True
        parsed = parse_transaction_bytes(signed_tx)
        assert parsed.has_witness
        assert len(parsed.witnesses[0]) == 1
        assert len(parsed.witnesses[0][0]) == 64
        assert parsed.inputs[0].sequence == CSV_DELAY
        assert parsed.locktime == 0
        assert parsed.version == 2

    def test_finalize_rejects_a_mutated_skeleton(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        spend = build_split(
            escrow,
            outpoint,
            BUYER_SPLIT_SCRIPT,
            COUNTERPARTY_SPLIT_SCRIPT,
            COUNTERPARTY_VALUE,
            SPLIT_FEE,
            CSV_DELAY,
        )
        buyer_secnonce, buyer_pubnonce = escrow_nonce(
            escrow.buyer_escrow_pubkey,
            b"\x0a" * 32,
            privkey=BUYER_ESCROW_SECRET,
            sighash=spend.sighash,
        )
        cp_secnonce, cp_pubnonce = escrow_nonce(
            escrow.counterparty_escrow_pubkey,
            b"\x0b" * 32,
            privkey=COUNTERPARTY_ESCROW_SECRET,
            sighash=spend.sighash,
        )
        session = escrow_session(escrow, nonce_agg([buyer_pubnonce, cp_pubnonce]), spend.sighash)
        buyer_partial = sign_partial(buyer_secnonce, BUYER_ESCROW_SECRET, session)
        cp_partial = sign_partial(cp_secnonce, COUNTERPARTY_ESCROW_SECRET, session)
        spend.tx.outputs[1] = TxOut(
            value=COUNTERPARTY_VALUE + 10_000, scriptpubkey=COUNTERPARTY_SPLIT_SCRIPT
        )
        with pytest.raises(ChannelBuyoutEscrowError, match="changed after its sighash"):
            finalize_key_path_spend(
                escrow, spend, buyer_pubnonce, cp_pubnonce, buyer_partial, cp_partial
            )

    def test_finalize_rejects_role_swapped_partials(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        spend = build_split(
            escrow,
            outpoint,
            BUYER_SPLIT_SCRIPT,
            COUNTERPARTY_SPLIT_SCRIPT,
            COUNTERPARTY_VALUE,
            SPLIT_FEE,
            CSV_DELAY,
        )
        buyer_secnonce, buyer_pubnonce = escrow_nonce(
            escrow.buyer_escrow_pubkey,
            b"\x0a" * 32,
            privkey=BUYER_ESCROW_SECRET,
            sighash=spend.sighash,
        )
        cp_secnonce, cp_pubnonce = escrow_nonce(
            escrow.counterparty_escrow_pubkey,
            b"\x0b" * 32,
            privkey=COUNTERPARTY_ESCROW_SECRET,
            sighash=spend.sighash,
        )
        session = escrow_session(escrow, nonce_agg([buyer_pubnonce, cp_pubnonce]), spend.sighash)
        buyer_partial = sign_partial(buyer_secnonce, BUYER_ESCROW_SECRET, session)
        cp_partial = sign_partial(cp_secnonce, COUNTERPARTY_ESCROW_SECRET, session)
        with pytest.raises(ChannelBuyoutEscrowError, match="invalid buyer partial"):
            finalize_key_path_spend(
                escrow, spend, buyer_pubnonce, cp_pubnonce, cp_partial, buyer_partial
            )

    @pytest.mark.parametrize("partial", [b"", b"\x00" * 32, b"\xff" * 32])
    def test_finalize_rejects_malformed_partials(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint, partial: bytes
    ) -> None:
        spend = build_split(
            escrow,
            outpoint,
            BUYER_SPLIT_SCRIPT,
            COUNTERPARTY_SPLIT_SCRIPT,
            COUNTERPARTY_VALUE,
            SPLIT_FEE,
            CSV_DELAY,
        )
        buyer_secnonce, buyer_pubnonce = escrow_nonce(
            escrow.buyer_escrow_pubkey,
            b"\x0a" * 32,
            privkey=BUYER_ESCROW_SECRET,
            sighash=spend.sighash,
        )
        _, cp_pubnonce = escrow_nonce(
            escrow.counterparty_escrow_pubkey,
            b"\x0b" * 32,
            privkey=COUNTERPARTY_ESCROW_SECRET,
            sighash=spend.sighash,
        )
        session = escrow_session(escrow, nonce_agg([buyer_pubnonce, cp_pubnonce]), spend.sighash)
        buyer_partial = sign_partial(buyer_secnonce, BUYER_ESCROW_SECRET, session)
        with pytest.raises(ChannelBuyoutEscrowError, match="invalid counterparty partial"):
            finalize_key_path_spend(
                escrow, spend, buyer_pubnonce, cp_pubnonce, buyer_partial, partial
            )

    def test_verify_rejects_a_flipped_signature_bit(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = _signed_split(escrow, outpoint)

        def flip(tx: TaprootTx) -> None:
            signature = bytearray(tx.witnesses[0][0])
            signature[-1] ^= 0x01
            tx.witnesses[0][0] = bytes(signature)

        with pytest.raises(ChannelBuyoutEscrowError, match="does not verify"):
            _verify_split(escrow, outpoint, _reserialize(signed_tx, flip))

    def test_verify_rejects_a_changed_outpoint(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = _signed_split(escrow, outpoint)

        def retarget(tx: TaprootTx) -> None:
            tx.inputs[0] = TxIn(txid="b" * 64, vout=1, sequence=CSV_DELAY)

        with pytest.raises(ChannelBuyoutEscrowError, match="exact escrow outpoint"):
            _verify_split(escrow, outpoint, _reserialize(signed_tx, retarget))

        def revout(tx: TaprootTx) -> None:
            tx.inputs[0] = TxIn(txid=PARENT_TXID, vout=2, sequence=CSV_DELAY)

        with pytest.raises(ChannelBuyoutEscrowError, match="exact escrow outpoint"):
            _verify_split(escrow, outpoint, _reserialize(signed_tx, revout))

    def test_verify_rejects_a_changed_sequence(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = _signed_split(escrow, outpoint)

        def unlock(tx: TaprootTx) -> None:
            tx.inputs[0] = TxIn(txid=PARENT_TXID, vout=1, sequence=0xFFFFFFFD)

        with pytest.raises(ChannelBuyoutEscrowError, match="nSequence"):
            _verify_split(escrow, outpoint, _reserialize(signed_tx, unlock))

    def test_verify_rejects_a_stolen_fee(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = _signed_split(escrow, outpoint)

        def inflate_fee(tx: TaprootTx) -> None:
            tx.outputs[0] = TxOut(
                value=tx.outputs[0].value - 5_000, scriptpubkey=BUYER_SPLIT_SCRIPT
            )

        with pytest.raises(ChannelBuyoutEscrowError, match="output 0 value"):
            _verify_split(escrow, outpoint, _reserialize(signed_tx, inflate_fee))

    def test_verify_rejects_a_redirected_output(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = _signed_split(escrow, outpoint)

        def redirect(tx: TaprootTx) -> None:
            tx.outputs[1] = TxOut(value=COUNTERPARTY_VALUE, scriptpubkey=_p2tr(b"\x54" * 32))

        with pytest.raises(ChannelBuyoutEscrowError, match="output 1 script"):
            _verify_split(escrow, outpoint, _reserialize(signed_tx, redirect))

    def test_verify_rejects_swapped_output_order(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = _signed_split(escrow, outpoint)

        def swap(tx: TaprootTx) -> None:
            tx.outputs.reverse()

        with pytest.raises(ChannelBuyoutEscrowError, match="output 0"):
            _verify_split(escrow, outpoint, _reserialize(signed_tx, swap))

    def test_verify_rejects_an_extra_witness_item(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = _signed_split(escrow, outpoint)

        def pad(tx: TaprootTx) -> None:
            tx.witnesses[0].append(b"\x00")

        with pytest.raises(ChannelBuyoutEscrowError, match="exactly one SIGHASH_DEFAULT"):
            _verify_split(escrow, outpoint, _reserialize(signed_tx, pad))

    def test_verify_rejects_disagreeing_parameters(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = _signed_split(escrow, outpoint)
        with pytest.raises(ChannelBuyoutEscrowError, match="nSequence"):
            verify_signed_split(
                escrow,
                outpoint,
                BUYER_SPLIT_SCRIPT,
                COUNTERPARTY_SPLIT_SCRIPT,
                COUNTERPARTY_VALUE,
                SPLIT_FEE,
                CSV_DELAY + 1,
                signed_tx,
            )
        with pytest.raises(ChannelBuyoutEscrowError, match="output 0 value"):
            verify_signed_split(
                escrow,
                outpoint,
                BUYER_SPLIT_SCRIPT,
                COUNTERPARTY_SPLIT_SCRIPT,
                COUNTERPARTY_VALUE + 1,
                SPLIT_FEE,
                CSV_DELAY,
                signed_tx,
            )

    def test_verify_rejects_a_foreign_escrow(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = _signed_split(escrow, outpoint)
        other = BuyoutEscrow(
            buyer_escrow_pubkey=escrow.buyer_escrow_pubkey,
            counterparty_escrow_pubkey=escrow.counterparty_escrow_pubkey,
            buyer_claim_pubkey=escrow.buyer_claim_pubkey,
            payment_hash=hashlib.sha256(b"other").digest(),
        )
        with pytest.raises(ChannelBuyoutEscrowError, match="agreed escrow script"):
            verify_signed_split(
                other,
                outpoint,
                BUYER_SPLIT_SCRIPT,
                COUNTERPARTY_SPLIT_SCRIPT,
                COUNTERPARTY_VALUE,
                SPLIT_FEE,
                CSV_DELAY,
                signed_tx,
            )

    @pytest.mark.parametrize("signed_tx", [b"", b"\x00\x01", "not bytes"])
    def test_verify_rejects_malformed_serializations(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint, signed_tx: object
    ) -> None:
        with pytest.raises(ChannelBuyoutEscrowError):
            _verify_split(escrow, outpoint, signed_tx)  # type: ignore[arg-type]

    def test_verify_rejects_a_non_segwit_serialization(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        spend = build_split(
            escrow,
            outpoint,
            BUYER_SPLIT_SCRIPT,
            COUNTERPARTY_SPLIT_SCRIPT,
            COUNTERPARTY_VALUE,
            SPLIT_FEE,
            CSV_DELAY,
        )
        with pytest.raises(ChannelBuyoutEscrowError, match="SegWit"):
            _verify_split(escrow, outpoint, spend.tx._serialize_no_witness())

    def test_verify_rejects_trailing_bytes(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = _signed_split(escrow, outpoint)
        with pytest.raises(ChannelBuyoutEscrowError):
            _verify_split(escrow, outpoint, signed_tx + b"\x00")

    def test_verify_rejects_a_non_minimal_serialization(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        """A transaction that parses identically but is not the agreed bytes fails."""
        signed_tx = _signed_split(escrow, outpoint)
        non_minimal = signed_tx[:4] + b"\x00\x01" + b"\xfd\x01\x00" + signed_tx[7:]
        assert non_minimal != signed_tx
        with pytest.raises(ChannelBuyoutEscrowError, match="byte for byte"):
            _verify_split(escrow, outpoint, non_minimal)


# --------------------------------------------------------------------------- #
# Unilateral preimage claim
# --------------------------------------------------------------------------- #


class TestClaim:
    def test_claim_round_trip(self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint) -> None:
        signed_tx = build_claim(
            escrow, outpoint, PREIMAGE, BUYER_CLAIM_SECRET, BUYER_SWEEP_SCRIPT, 500
        )
        assert verify_signed_claim(escrow, outpoint, BUYER_SWEEP_SCRIPT, 500, signed_tx) is True
        parsed = parse_transaction_bytes(signed_tx)
        assert parsed.version == 2
        assert parsed.locktime == 0
        assert parsed.inputs[0].sequence == SWEEP_SEQUENCE == 0xFFFFFFFD
        assert parsed.inputs[0].sequence & (1 << 31) != 0, "relative lock must be disabled"
        assert parsed.inputs[0].sequence < 0xFFFFFFFE, "claim must signal replaceability"
        assert parsed.outputs[0].value == ESCROW_VALUE - 500
        assert parsed.outputs[0].script == BUYER_SWEEP_SCRIPT
        signature, preimage, leaf, control = parsed.witnesses[0]
        assert len(signature) == 64
        assert preimage == PREIMAGE
        assert (leaf, control) == escrow.claim_control_block()

    def test_claim_needs_no_counterparty_secret(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        """The claim path is unilateral: only the preimage and claim key are used."""
        first = build_claim(escrow, outpoint, PREIMAGE, BUYER_CLAIM_SECRET, BUYER_SWEEP_SCRIPT, 500)
        second = build_claim(
            escrow, outpoint, PREIMAGE, BUYER_CLAIM_SECRET, BUYER_SWEEP_SCRIPT, 500
        )
        assert first == second

    @pytest.mark.parametrize(
        "preimage", [b"", b"\x00" * 31, b"\x00" * 32, PREIMAGE[:-1] + b"\x00", PREIMAGE.hex()]
    )
    def test_wrong_preimage_is_rejected(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint, preimage: object
    ) -> None:
        with pytest.raises(ChannelBuyoutEscrowError, match="preimage"):
            build_claim(
                escrow,
                outpoint,
                preimage,  # type: ignore[arg-type]
                BUYER_CLAIM_SECRET,
                BUYER_SWEEP_SCRIPT,
                500,
            )

    @pytest.mark.parametrize(
        "privkey", [b"", b"\x00" * 32, BUYER_ESCROW_SECRET, COUNTERPARTY_ESCROW_SECRET]
    )
    def test_wrong_claim_key_is_rejected(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint, privkey: bytes
    ) -> None:
        with pytest.raises(ChannelBuyoutEscrowError, match="claim private key"):
            build_claim(escrow, outpoint, PREIMAGE, privkey, BUYER_SWEEP_SCRIPT, 500)

    def test_claim_fee_bounds(self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint) -> None:
        with pytest.raises(ChannelBuyoutEscrowError, match="between 0"):
            build_claim(escrow, outpoint, PREIMAGE, BUYER_CLAIM_SECRET, BUYER_SWEEP_SCRIPT, -1)
        with pytest.raises(ChannelBuyoutEscrowError, match="at least"):
            build_claim(
                escrow,
                outpoint,
                PREIMAGE,
                BUYER_CLAIM_SECRET,
                BUYER_SWEEP_SCRIPT,
                ESCROW_VALUE - MIN_SPLIT_OUTPUT_SATS + 1,
            )

    def test_verify_rejects_a_substituted_preimage(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = build_claim(
            escrow, outpoint, PREIMAGE, BUYER_CLAIM_SECRET, BUYER_SWEEP_SCRIPT, 500
        )

        def substitute(tx: TaprootTx) -> None:
            tx.witnesses[0][1] = b"\x00" * 32

        with pytest.raises(ChannelBuyoutEscrowError, match="preimage"):
            verify_signed_claim(
                escrow,
                outpoint,
                BUYER_SWEEP_SCRIPT,
                500,
                _reserialize(signed_tx, substitute),
            )

    def test_verify_rejects_a_foreign_leaf_or_control_block(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = build_claim(
            escrow, outpoint, PREIMAGE, BUYER_CLAIM_SECRET, BUYER_SWEEP_SCRIPT, 500
        )

        def rewrite_leaf(tx: TaprootTx) -> None:
            tx.witnesses[0][2] = tx.witnesses[0][2] + b"\x75"

        with pytest.raises(ChannelBuyoutEscrowError, match="leaf or control block"):
            verify_signed_claim(
                escrow, outpoint, BUYER_SWEEP_SCRIPT, 500, _reserialize(signed_tx, rewrite_leaf)
            )

        def rewrite_control(tx: TaprootTx) -> None:
            control = bytearray(tx.witnesses[0][3])
            control[0] ^= 0x01
            tx.witnesses[0][3] = bytes(control)

        with pytest.raises(ChannelBuyoutEscrowError, match="leaf or control block"):
            verify_signed_claim(
                escrow,
                outpoint,
                BUYER_SWEEP_SCRIPT,
                500,
                _reserialize(signed_tx, rewrite_control),
            )

    def test_verify_rejects_a_tampered_claim_signature(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = build_claim(
            escrow, outpoint, PREIMAGE, BUYER_CLAIM_SECRET, BUYER_SWEEP_SCRIPT, 500
        )

        def flip(tx: TaprootTx) -> None:
            signature = bytearray(tx.witnesses[0][0])
            signature[0] ^= 0x01
            tx.witnesses[0][0] = bytes(signature)

        with pytest.raises(ChannelBuyoutEscrowError, match="does not verify"):
            verify_signed_claim(
                escrow, outpoint, BUYER_SWEEP_SCRIPT, 500, _reserialize(signed_tx, flip)
            )

    def test_verify_rejects_a_changed_fee_or_destination(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        signed_tx = build_claim(
            escrow, outpoint, PREIMAGE, BUYER_CLAIM_SECRET, BUYER_SWEEP_SCRIPT, 500
        )
        with pytest.raises(ChannelBuyoutEscrowError, match="output 0 value"):
            verify_signed_claim(escrow, outpoint, BUYER_SWEEP_SCRIPT, 600, signed_tx)
        with pytest.raises(ChannelBuyoutEscrowError, match="output 0 script"):
            verify_signed_claim(escrow, outpoint, _p2tr(b"\x55" * 32), 500, signed_tx)

    def test_verify_rejects_a_key_path_witness(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        spend = build_cooperative_sweep(escrow, outpoint, BUYER_SWEEP_SCRIPT, 500)
        signed_tx = _sign_key_path(escrow, spend, (BUYER_ESCROW_SECRET, COUNTERPARTY_ESCROW_SECRET))
        with pytest.raises(ChannelBuyoutEscrowError, match="claim witness must hold"):
            verify_signed_claim(escrow, outpoint, BUYER_SWEEP_SCRIPT, 500, signed_tx)


# --------------------------------------------------------------------------- #
# Cooperative sweep
# --------------------------------------------------------------------------- #


class TestCooperativeSweep:
    def test_sweep_round_trip(self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint) -> None:
        spend = build_cooperative_sweep(escrow, outpoint, BUYER_SWEEP_SCRIPT, 400)
        assert spend.tx.version == 2
        assert spend.tx.locktime == 0
        assert spend.tx.inputs[0].sequence == SWEEP_SEQUENCE
        assert spend.tx.outputs[0].value == ESCROW_VALUE - 400
        assert len(spend.sighash) == 32
        signed_tx = _sign_key_path(escrow, spend, (BUYER_ESCROW_SECRET, COUNTERPARTY_ESCROW_SECRET))
        assert (
            verify_signed_cooperative_sweep(escrow, outpoint, BUYER_SWEEP_SCRIPT, 400, signed_tx)
            is True
        )
        parsed = parse_transaction_bytes(signed_tx)
        assert len(parsed.witnesses[0]) == 1
        assert len(parsed.witnesses[0][0]) == 64

    def test_sweep_sighash_differs_from_the_split(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        sweep = build_cooperative_sweep(escrow, outpoint, BUYER_SWEEP_SCRIPT, 400)
        split = build_split(
            escrow,
            outpoint,
            BUYER_SPLIT_SCRIPT,
            COUNTERPARTY_SPLIT_SCRIPT,
            COUNTERPARTY_VALUE,
            SPLIT_FEE,
            CSV_DELAY,
        )
        assert sweep.sighash != split.sighash

    def test_split_signature_does_not_authorize_a_sweep(
        self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint
    ) -> None:
        split_tx = _signed_split(escrow, outpoint)
        split_signature = parse_transaction_bytes(split_tx).witnesses[0][0]
        sweep = build_cooperative_sweep(escrow, outpoint, BUYER_SWEEP_SCRIPT, 400)
        sweep.tx.witnesses = [[split_signature]]
        with pytest.raises(ChannelBuyoutEscrowError, match="does not verify"):
            verify_signed_cooperative_sweep(
                escrow, outpoint, BUYER_SWEEP_SCRIPT, 400, sweep.tx.serialize()
            )

    def test_sweep_amount_bounds(self, escrow: BuyoutEscrow, outpoint: EscrowOutpoint) -> None:
        with pytest.raises(ChannelBuyoutEscrowError, match="between 0"):
            build_cooperative_sweep(escrow, outpoint, BUYER_SWEEP_SCRIPT, True)  # type: ignore[arg-type]
        with pytest.raises(ChannelBuyoutEscrowError, match="at least"):
            build_cooperative_sweep(escrow, outpoint, BUYER_SWEEP_SCRIPT, ESCROW_VALUE)
        with pytest.raises(ChannelBuyoutEscrowError, match="P2TR"):
            build_cooperative_sweep(escrow, outpoint, b"\x00\x14" + b"\x11" * 20, 400)
