"""Tests for the shared swap HTLC script primitive (jmcore.swap_script).

These live in jmcore (not taker) because the swap HTLC script is a shared
Bitcoin protocol primitive used by both the taker and the wallet. The jmcore
test suite must be able to exercise it without depending on taker.
"""

from __future__ import annotations

import hashlib
import secrets

import pytest

from jmcore.swap_script import (
    MAX_LOCKTIME_DELTA,
    MIN_LOCKTIME_DELTA,
    SwapScript,
    _push_data,
    _push_int,
)


def _material() -> tuple[bytes, bytes, bytes, int]:
    preimage = secrets.token_bytes(32)
    preimage_hash = hashlib.sha256(preimage).digest()
    claim_pubkey = b"\x02" + secrets.token_bytes(32)
    refund_pubkey = b"\x03" + secrets.token_bytes(32)
    return preimage_hash, claim_pubkey, refund_pubkey, 800_100


def test_locktime_bounds_have_expected_values() -> None:
    assert MIN_LOCKTIME_DELTA == 60
    assert MAX_LOCKTIME_DELTA == 100
    assert MIN_LOCKTIME_DELTA < MAX_LOCKTIME_DELTA


def test_push_int_uses_opcodes_for_small_values() -> None:
    assert _push_int(0) == b"\x00"
    assert _push_int(1) == bytes([0x51])
    assert _push_int(16) == bytes([0x60])


def test_push_data_roundtrips_via_witness_script() -> None:
    # A 33-byte pubkey is a direct push (length byte + data).
    pubkey = b"\x02" + secrets.token_bytes(32)
    assert _push_data(pubkey) == bytes([33]) + pubkey


def test_witness_script_parse_roundtrip() -> None:
    preimage_hash, claim_pubkey, refund_pubkey, timeout = _material()
    script = SwapScript(preimage_hash, claim_pubkey, refund_pubkey, timeout)
    witness = script.witness_script()

    parsed = SwapScript.from_redeem_script(witness.hex())
    assert parsed.claim_pubkey == claim_pubkey
    assert parsed.refund_pubkey == refund_pubkey
    assert parsed.timeout_blockheight == timeout
    # The parsed script must reproduce the exact same bytes.
    assert parsed.witness_script() == witness
    assert parsed.verify_preimage_hash(preimage_hash)


def test_p2wsh_scriptpubkey_is_v0_32_bytes() -> None:
    preimage_hash, claim_pubkey, refund_pubkey, timeout = _material()
    script = SwapScript(preimage_hash, claim_pubkey, refund_pubkey, timeout)
    spk = script.p2wsh_scriptpubkey()
    assert spk[0] == 0x00  # OP_0
    assert spk[1] == 0x20  # push 32 bytes
    assert len(spk) == 34


def test_verify_against_provider_accepts_valid_locktime() -> None:
    preimage_hash, claim_pubkey, refund_pubkey, _ = _material()
    current_height = 1_000
    timeout = current_height + MIN_LOCKTIME_DELTA + 5
    script = SwapScript(preimage_hash, claim_pubkey, refund_pubkey, timeout)
    parsed = SwapScript.from_redeem_script(script.witness_script().hex())
    parsed.verify_against_provider(
        expected_preimage_hash=preimage_hash,
        expected_claim_pubkey=claim_pubkey,
        timeout_blockheight=timeout,
        current_block_height=current_height,
    )


def test_verify_against_provider_rejects_timeout_too_soon() -> None:
    preimage_hash, claim_pubkey, refund_pubkey, _ = _material()
    current_height = 1_000
    timeout = current_height + MIN_LOCKTIME_DELTA - 1
    script = SwapScript(preimage_hash, claim_pubkey, refund_pubkey, timeout)
    parsed = SwapScript.from_redeem_script(script.witness_script().hex())
    with pytest.raises(ValueError, match="Timeout too soon"):
        parsed.verify_against_provider(
            expected_preimage_hash=preimage_hash,
            expected_claim_pubkey=claim_pubkey,
            timeout_blockheight=timeout,
            current_block_height=current_height,
        )


def test_verify_against_provider_rejects_timeout_too_far() -> None:
    preimage_hash, claim_pubkey, refund_pubkey, _ = _material()
    current_height = 1_000
    timeout = current_height + MAX_LOCKTIME_DELTA + 1
    script = SwapScript(preimage_hash, claim_pubkey, refund_pubkey, timeout)
    parsed = SwapScript.from_redeem_script(script.witness_script().hex())
    with pytest.raises(ValueError, match="Timeout too far"):
        parsed.verify_against_provider(
            expected_preimage_hash=preimage_hash,
            expected_claim_pubkey=claim_pubkey,
            timeout_blockheight=timeout,
            current_block_height=current_height,
        )
