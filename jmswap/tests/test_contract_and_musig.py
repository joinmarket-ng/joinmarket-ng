from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from coincurve import PrivateKey, PublicKeyXOnly
from electrum_ecc import musig
from jmcore.bitcoin import sha256

from jmswap.contract import SwapDirection, SwapLeaf, TaprootSwapContract, taproot_tweak_hash
from jmswap.musig import MuSig2Session

PROVIDER_KEY = PrivateKey.from_int(1)
USER_KEY = PrivateKey.from_int(2)
PROVIDER_PUBKEY = PROVIDER_KEY.public_key.format(compressed=True)
USER_PUBKEY = USER_KEY.public_key.format(compressed=True)
PREIMAGE = b"\xaa" * 32
PAYMENT_HASH = sha256(PREIMAGE)


def contract(direction: SwapDirection = SwapDirection.FORWARD) -> TaprootSwapContract:
    return TaprootSwapContract(direction, PAYMENT_HASH, 800_000, PROVIDER_PUBKEY, USER_PUBKEY)


def test_electrum_and_boltz_contract_vectors() -> None:
    forward = contract()
    assert forward.claim_leaf_script.hex() == (
        "a914b3256e789b42b4e73b0954beb516ec7dfc032dd3882079be667ef9dcbb"
        "ac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798ac"
    )
    assert forward.refund_leaf_script.hex() == (
        "20c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5ad0300350cb1"
    )
    assert forward.merkle_root.hex() == (
        "75372846f2df145268cb58b82356acb491acb36f7de6af011edc06204fb0e6a6"
    )
    assert forward.internal_pubkey.hex() == (
        "3b46d262d2f610e9038b44beabdfe97ab5a0feb89870acc2264edfb7f63ec2ec"
    )
    assert forward.address() == "bc1pqq3wm8st9gsulhkf289sm547l63r8zl0zvsa6lul42yhh84eqeaqtw5767"

    reverse = contract(SwapDirection.REVERSE)
    assert reverse.claim_leaf_script.hex() == (
        "82012088a914b3256e789b42b4e73b0954beb516ec7dfc032dd38820c6047f"
        "9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5ac"
    )
    assert reverse.address() == "bc1p5cr57xdpjm8l7jwm2yyhrwuq4y2p7u808hqeau7alf0ug2lcn9kqqks077"


def test_control_blocks_and_provider_data_validation() -> None:
    swap = contract()
    script, control = swap.script_path(SwapLeaf.CLAIM)
    assert script == swap.claim_leaf_script
    assert control.hex() == (
        "c03b46d262d2f610e9038b44beabdfe97ab5a0feb89870acc2264edfb7f63"
        "ec2eceb0b7997538e555615e4c0460ef19a4045cc07d87897f75c5a1475"
        "165c9efbb4"
    )
    swap.validate_provider_data(serialized_tree=swap.serialized_tree(), address=swap.address())
    tampered = swap.serialized_tree()
    tampered["claimLeaf"]["output"] = "00"
    with pytest.raises(ValueError, match="claimLeaf"):
        swap.validate_provider_data(serialized_tree=tampered, address=swap.address())


@pytest.mark.parametrize("locktime", [0, 500_000_001, True])
def test_contract_rejects_non_block_locktimes(locktime: int) -> None:
    with pytest.raises((TypeError, ValueError)):
        TaprootSwapContract(
            SwapDirection.FORWARD, PAYMENT_HASH, locktime, PROVIDER_PUBKEY, USER_PUBKEY
        )


def test_contract_rejects_equal_xonly_keys() -> None:
    with pytest.raises(ValueError, match="distinct"):
        TaprootSwapContract(
            SwapDirection.FORWARD,
            PAYMENT_HASH,
            800_000,
            PROVIDER_PUBKEY,
            bytes([3]) + PROVIDER_PUBKEY[1:],
        )


def _direct_nonce(
    swap: TaprootSwapContract, seckey: bytes, msg: bytes, session_id: bytes, random: bytes
) -> tuple[Any, Any, Any]:
    pubkey = PrivateKey(seckey).public_key.format(compressed=True)
    cache = swap.keyagg_cache()
    cache.apply_xonly_tweak(taproot_tweak_hash(cache.aggregate_xonly_pubkey(), swap.merkle_root))
    with patch("electrum_ecc.musig.secrets.token_bytes", return_value=random):
        secnonce, pubnonce = musig.nonce_gen(
            pubkey=pubkey,
            seckey=seckey,
            msg32=msg,
            keyagg_cache=cache,
            extra_input32=session_id,
        )
    return cache, secnonce, pubnonce


def test_musig_matches_direct_electrum_ecc_reference() -> None:
    provider_seckey = bytes.fromhex(
        "b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
    )
    user_seckey = bytes.fromhex("c90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74020bbea63b14e5c9")
    provider_pubkey = PrivateKey(provider_seckey).public_key.format(compressed=True)
    user_pubkey = PrivateKey(user_seckey).public_key.format(compressed=True)
    swap = TaprootSwapContract(
        SwapDirection.REVERSE, PAYMENT_HASH, 800_000, provider_pubkey, user_pubkey
    )
    msg = sha256(b"BIP341 key-path sighash fixture")
    session_id = b"\x44" * 32

    with patch("electrum_ecc.musig.secrets.token_bytes", return_value=b"\x11" * 32):
        provider = MuSig2Session(
            contract=swap,
            local_seckey=provider_seckey,
            msg32=msg,
            session_id32=session_id,
        )
    with patch("electrum_ecc.musig.secrets.token_bytes", return_value=b"\x22" * 32):
        user = MuSig2Session(
            contract=swap,
            local_seckey=user_seckey,
            msg32=msg,
            session_id32=session_id,
        )

    provider_cache, provider_secret_nonce, provider_nonce = _direct_nonce(
        swap, provider_seckey, msg, session_id, b"\x11" * 32
    )
    user_cache, user_secret_nonce, user_nonce = _direct_nonce(
        swap, user_seckey, msg, session_id, b"\x22" * 32
    )
    assert provider.public_nonce == provider_nonce.to_bytes()
    assert user.public_nonce == user_nonce.to_bytes()

    aggregate_nonce = musig.nonce_agg([provider_nonce, user_nonce])
    direct_provider_session = musig.nonce_process(
        aggnonce=aggregate_nonce, msg32=msg, keyagg_cache=provider_cache
    )
    direct_user_session = musig.nonce_process(
        aggnonce=aggregate_nonce, msg32=msg, keyagg_cache=user_cache
    )
    direct_provider_partial = musig.partial_sign(
        secnonce=provider_secret_nonce,
        seckey=provider_seckey,
        keyagg_cache=provider_cache,
        session=direct_provider_session,
    ).to_bytes()
    direct_user_partial = musig.partial_sign(
        secnonce=user_secret_nonce,
        seckey=user_seckey,
        keyagg_cache=user_cache,
        session=direct_user_session,
    ).to_bytes()

    provider_partial = provider.sign_partial(user.public_nonce)
    user_partial = user.sign_partial(provider.public_nonce)
    assert provider_partial == direct_provider_partial
    assert user_partial == direct_user_partial
    signature = provider.aggregate(user_partial)
    assert signature == user.aggregate(provider_partial)
    assert len(signature) == 64
    assert PublicKeyXOnly(swap.output_script[2:]).verify(signature, msg)
    with pytest.raises(RuntimeError, match="already signed"):
        provider.sign_partial(user.public_nonce)
