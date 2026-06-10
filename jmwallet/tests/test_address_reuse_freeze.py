"""Tests for the forced-address-reuse auto-freeze (issue #529).

When a UTXO lands on an address the wallet has already used, it is frozen
automatically so it is never co-spent in a CoinJoin (which would link the
wallet's coins via the common-input-ownership heuristic). Mirrors legacy
joinmarket-clientserver's ``POLICY.max_sats_freeze_reuse`` behavior:

* ``-1`` (default) freezes all reuse UTXOs, whatever the value.
* ``N`` (positive) freezes only reuse UTXOs with value <= N sats.
* ``0`` disables the behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.utxo_metadata import (
    AUTO_FREEZE_REUSE_LABEL,
    UTXOMetadataStore,
)

# BIP-39 test vector -- never use on mainnet.
MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)

REUSED_ADDRESS = "bcrt1qreusedaddr00000000000000000000000000000"
FRESH_ADDRESS = "bcrt1qfreshaddr000000000000000000000000000000"


def _make_wallet(tmp_path: Path, *, max_sats_freeze_reuse: int = -1) -> WalletService:
    backend = DescriptorWalletBackend(wallet_name="test_wallet")
    backend._wallet_loaded = True
    ws = WalletService(
        mnemonic=MNEMONIC,
        backend=backend,
        network="regtest",
        data_dir=tmp_path,
        max_sats_freeze_reuse=max_sats_freeze_reuse,
    )
    # Use an isolated metadata store path for the test.
    ws.metadata_store = UTXOMetadataStore(path=tmp_path / "wallet_metadata_test.jsonl")
    return ws


def _utxo(
    *,
    txid: str,
    address: str,
    value: int,
    vout: int = 0,
    locktime: int | None = None,
) -> UTXOInfo:
    return UTXOInfo(
        txid=txid,
        vout=vout,
        value=value,
        address=address,
        confirmations=3,
        scriptpubkey="0014" + "ab" * 20,
        path="m/84'/1'/0'/0/0",
        mixdepth=0,
        locktime=locktime,
    )


def test_freezes_utxo_on_already_used_address(tmp_path: Path) -> None:
    ws = _make_wallet(tmp_path, max_sats_freeze_reuse=-1)
    utxo = _utxo(txid="aa" * 32, address=REUSED_ADDRESS, value=10_000)
    ws.utxo_cache = {0: [utxo]}

    # The address was used by a prior transaction.
    frozen = ws._auto_freeze_reused_address_utxos({REUSED_ADDRESS}, set(), set())

    assert frozen == 1
    assert utxo.frozen is True
    assert ws.metadata_store.is_frozen(utxo.outpoint)
    # The freeze is labeled so it survives a later unfreeze without re-freezing.
    record = ws.metadata_store.records[utxo.outpoint]
    assert record.label == AUTO_FREEZE_REUSE_LABEL


def test_does_not_freeze_reuse_when_address_still_holds_funds(tmp_path: Path) -> None:
    """Coins arriving on an address that still holds funds are not frozen.

    Per https://en.bitcoin.it/wiki/Privacy#Forced_address_reuse, when the
    address is *not empty* the privacy-correct action is to fully spend all
    coins on it together, not to freeze. So neither the original deposit nor the
    new arrival is auto-frozen while the address still has a balance.
    """
    ws = _make_wallet(tmp_path, max_sats_freeze_reuse=-1)
    original = _utxo(txid="01" * 32, address=REUSED_ADDRESS, value=50_000, vout=0)
    reuse = _utxo(txid="02" * 32, address=REUSED_ADDRESS, value=600, vout=1)
    ws.utxo_cache = {0: [original, reuse]}

    # The address is used AND still funded (original is pre-existing), so the
    # new arrival is left spendable.
    frozen = ws._auto_freeze_reused_address_utxos(
        {REUSED_ADDRESS},
        prior_known_outpoints={original.outpoint},
        prior_funded_addresses={REUSED_ADDRESS},
    )

    assert frozen == 0
    assert original.frozen is False
    assert reuse.frozen is False


def test_freezes_reuse_on_spent_empty_used_address(tmp_path: Path) -> None:
    """A new arrival on a used address that was emptied is auto-frozen.

    This is the forced-reuse case: the address was funded then spent to empty,
    and now receives a new (dust) payment, which must be frozen.
    """
    ws = _make_wallet(tmp_path, max_sats_freeze_reuse=-1)
    reuse = _utxo(txid="03" * 32, address=REUSED_ADDRESS, value=600, vout=0)
    ws.utxo_cache = {0: [reuse]}

    # Address was used before but is now empty (no prior funded UTXO).
    frozen = ws._auto_freeze_reused_address_utxos(
        {REUSED_ADDRESS},
        prior_known_outpoints=set(),
        prior_funded_addresses=set(),
    )

    assert frozen == 1
    assert reuse.frozen is True


def test_first_sight_of_multiple_utxos_freezes_nothing(tmp_path: Path) -> None:
    """Two UTXOs appearing together on a not-yet-used address are not frozen.

    On the first sync the address is not yet in the used set, so neither is
    flagged (matching legacy, which only freezes a reuse seen after the address
    was already recorded).
    """
    ws = _make_wallet(tmp_path, max_sats_freeze_reuse=-1)
    a = _utxo(txid="0a" * 32, address=REUSED_ADDRESS, value=10_000, vout=0)
    b = _utxo(txid="0b" * 32, address=REUSED_ADDRESS, value=10_000, vout=1)
    ws.utxo_cache = {0: [a, b]}

    # Empty prior_used set -> nothing is reuse yet.
    assert ws._auto_freeze_reused_address_utxos(set(), set(), set()) == 0
    assert a.frozen is False
    assert b.frozen is False


def test_does_not_freeze_utxo_on_fresh_address(tmp_path: Path) -> None:
    ws = _make_wallet(tmp_path, max_sats_freeze_reuse=-1)
    utxo = _utxo(txid="bb" * 32, address=FRESH_ADDRESS, value=10_000)
    ws.utxo_cache = {0: [utxo]}

    # Only REUSED_ADDRESS was used before; the UTXO is on a fresh address.
    frozen = ws._auto_freeze_reused_address_utxos({REUSED_ADDRESS}, set(), set())

    assert frozen == 0
    assert utxo.frozen is False
    assert not ws.metadata_store.is_frozen(utxo.outpoint)


def test_threshold_freezes_only_small_reuse_utxos(tmp_path: Path) -> None:
    ws = _make_wallet(tmp_path, max_sats_freeze_reuse=5_000)
    small = _utxo(txid="cc" * 32, address=REUSED_ADDRESS, value=5_000, vout=0)
    large = _utxo(txid="dd" * 32, address=REUSED_ADDRESS, value=5_001, vout=1)
    ws.utxo_cache = {0: [small, large]}

    frozen = ws._auto_freeze_reused_address_utxos({REUSED_ADDRESS}, set(), set())

    # value <= threshold is frozen; value above is left spendable.
    assert frozen == 1
    assert small.frozen is True
    assert large.frozen is False


def test_threshold_zero_disables_autofreeze(tmp_path: Path) -> None:
    ws = _make_wallet(tmp_path, max_sats_freeze_reuse=0)
    utxo = _utxo(txid="ee" * 32, address=REUSED_ADDRESS, value=1)
    ws.utxo_cache = {0: [utxo]}

    frozen = ws._auto_freeze_reused_address_utxos({REUSED_ADDRESS}, set(), set())

    assert frozen == 0
    assert utxo.frozen is False


def test_skips_fidelity_bond_utxos(tmp_path: Path) -> None:
    ws = _make_wallet(tmp_path, max_sats_freeze_reuse=-1)
    # A timelocked (fidelity bond) UTXO on a "reused" address must NOT be frozen
    # by this defense: bonds live on a dedicated branch and are handled
    # separately.
    bond = _utxo(
        txid="ff" * 32,
        address=REUSED_ADDRESS,
        value=10_000,
        locktime=1_893_456_000,
    )
    ws.utxo_cache = {0: [bond]}

    frozen = ws._auto_freeze_reused_address_utxos({REUSED_ADDRESS}, set(), set())

    assert frozen == 0
    assert bond.frozen is False


def test_does_not_override_user_unfrozen_reuse_utxo(tmp_path: Path) -> None:
    """A reuse UTXO the user explicitly unfroze must not be re-frozen.

    The auto-freeze labels the record; unfreeze keeps the labeled record, so a
    subsequent sync sees an existing record and leaves the UTXO alone.
    """
    ws = _make_wallet(tmp_path, max_sats_freeze_reuse=-1)
    utxo = _utxo(txid="a1" * 32, address=REUSED_ADDRESS, value=10_000)
    ws.utxo_cache = {0: [utxo]}

    # First sync auto-freezes it.
    assert ws._auto_freeze_reused_address_utxos({REUSED_ADDRESS}, set(), set()) == 1
    assert utxo.frozen is True

    # User deliberately unfreezes it (record is kept because it has a label).
    ws.metadata_store.unfreeze(utxo.outpoint)
    assert ws.metadata_store.has_record(utxo.outpoint)
    utxo.frozen = False

    # A later sync must not re-freeze it.
    assert ws._auto_freeze_reused_address_utxos({REUSED_ADDRESS}, set(), set()) == 0
    assert utxo.frozen is False


def test_no_op_without_metadata_store(tmp_path: Path) -> None:
    backend = DescriptorWalletBackend(wallet_name="test_wallet")
    backend._wallet_loaded = True
    ws = WalletService(mnemonic=MNEMONIC, backend=backend, network="regtest")
    ws.metadata_store = None  # type: ignore[assignment]
    utxo = _utxo(txid="b2" * 32, address=REUSED_ADDRESS, value=10_000)
    ws.utxo_cache = {0: [utxo]}

    assert ws._auto_freeze_reused_address_utxos({REUSED_ADDRESS}, set(), set()) == 0
    assert utxo.frozen is False


def test_empty_prior_used_set_is_noop(tmp_path: Path) -> None:
    ws = _make_wallet(tmp_path, max_sats_freeze_reuse=-1)
    utxo = _utxo(txid="c3" * 32, address=REUSED_ADDRESS, value=10_000)
    ws.utxo_cache = {0: [utxo]}

    assert ws._auto_freeze_reused_address_utxos(set(), set(), set()) == 0
    assert utxo.frozen is False


@pytest.mark.parametrize("max_sats_freeze_reuse", [-1, 0, 1000])
def test_constructor_stores_threshold(tmp_path: Path, max_sats_freeze_reuse: int) -> None:
    ws = _make_wallet(tmp_path, max_sats_freeze_reuse=max_sats_freeze_reuse)
    assert ws.max_sats_freeze_reuse == max_sats_freeze_reuse


def test_default_threshold_is_freeze_all(tmp_path: Path) -> None:
    backend = DescriptorWalletBackend(wallet_name="test_wallet")
    backend._wallet_loaded = True
    ws = WalletService(mnemonic=MNEMONIC, backend=backend, network="regtest")
    # Matches legacy default: -1 freezes all reuse.
    assert ws.max_sats_freeze_reuse == -1
