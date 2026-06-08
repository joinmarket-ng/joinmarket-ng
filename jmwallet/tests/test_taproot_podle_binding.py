"""Regression tests for binding a Taproot PoDLE to its UTXO.

JMP-0001 Phase 3 binds the taker's PoDLE public key ``P`` to the UTXO
scriptPubKey by checking ``x_only(P) == program``. For a BIP86 key-path P2TR
UTXO the on-chain program is the *tweaked* output key, so the PoDLE MUST commit
to the tweaked output scalar (``resolve_p2tr_signing_key``), not the raw BIP32
internal key. Committing to the internal key makes binding fail for every
taproot UTXO (see JMP-0005 "Taproot PoDLE").
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from coincurve import PrivateKey
from jmcore.bitcoin import create_p2tr_scriptpubkey
from jmcore.podle import generate_podle, verify_podle, verify_podle_binding

from jmwallet.wallet.service import WalletService

UTXO = "aa" * 32 + ":0"


def _p2tr_wallet(test_mnemonic: str) -> WalletService:
    backend = AsyncMock()
    return WalletService(test_mnemonic, backend, network="regtest", address_type="p2tr")


def test_tweaked_key_binds_internal_key_does_not(test_mnemonic: str) -> None:
    wallet = _p2tr_wallet(test_mnemonic)
    addr = wallet.get_address(0, 0, 0, "p2tr")
    spk = create_p2tr_scriptpubkey(wallet.get_key_for_address(addr).get_p2tr_output_xonly())

    # The taker path: resolve_p2tr_signing_key returns the tweaked output scalar.
    resolved = wallet.resolve_p2tr_signing_key(addr)
    assert resolved is not None
    tweaked_scalar = resolved[0].secret

    # The buggy path: the raw BIP32 internal key.
    internal_scalar = wallet.get_key_for_address(addr).get_private_key_bytes()
    assert tweaked_scalar != internal_scalar

    tweaked_podle = generate_podle(tweaked_scalar, UTXO, 0)
    internal_podle = generate_podle(internal_scalar, UTXO, 0)

    bound, err = verify_podle_binding(tweaked_podle.p, spk)
    assert bound, err
    bad_bound, _ = verify_podle_binding(internal_podle.p, spk)
    assert not bad_bound

    # The DLEQ proof itself is valid in both cases; only binding distinguishes.
    ok, verr = verify_podle(
        tweaked_podle.p,
        tweaked_podle.p2,
        tweaked_podle.sig,
        tweaked_podle.e,
        tweaked_podle.commitment,
    )
    assert ok, verr


def test_binding_is_parity_independent(test_mnemonic: str) -> None:
    """Binding compares only the x-only key, so odd-Y output keys still bind."""
    wallet = _p2tr_wallet(test_mnemonic)
    # Scan several indices to exercise both output-key parities.
    found_even = found_odd = False
    for index in range(12):
        addr = wallet.get_address(0, 0, index, "p2tr")
        key = wallet.get_key_for_address(addr)
        scalar = wallet.resolve_p2tr_signing_key(addr)[0].secret
        spk = create_p2tr_scriptpubkey(key.get_p2tr_output_xonly())
        podle = generate_podle(scalar, UTXO, 0)
        bound, err = verify_podle_binding(podle.p, spk)
        assert bound, err
        if podle.p[0] == 0x02:
            found_even = True
        elif podle.p[0] == 0x03:
            found_odd = True
    # At least one of each parity over 12 keys (probabilistic but ~1 - 2^-12).
    assert found_even and found_odd


def test_signing_key_matches_committed_key(test_mnemonic: str) -> None:
    """The PoDLE-committed key must be the same key that signs the input."""
    wallet = _p2tr_wallet(test_mnemonic)
    addr = wallet.get_address(0, 0, 0, "p2tr")
    priv, output_xonly = wallet.resolve_p2tr_signing_key(addr)
    podle = generate_podle(priv.secret, UTXO, 0)
    # x_only(committed P) == x_only(signing output key).
    assert podle.p[1:] == output_xonly
    assert PrivateKey(priv.secret).public_key.format(compressed=True)[1:] == output_xonly
