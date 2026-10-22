"""Required advertisement history cannot disappear into a legacy admission path."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from bitcointx.core.key import CKey

from jmcore.bond_advertisements import (
    advertisement_record_path,
    initialize_fresh_advertisements,
    locked_advertisements,
    record_advertisement,
)
from jmcore.credential_market import BondCredential, BondReference, bond_resource, sign_bond_lease
from jmcore.crypto import bitcoin_message_hash_bytes, get_cert_msg
from jmcore.market_store import (
    MarketStore,
    MarketStoreConflictError,
    MarketStoreCorruptError,
    MarketStoreError,
    MarketStoreUnavailableError,
    blocked_bond_outpoints,
)
from jmcore.paths import get_market_store_path, get_used_commitments_path

WALLET = "a1" * 32
NETWORK = "regtest"


def bond(network: str = NETWORK) -> BondReference:
    return BondReference.model_validate(
        {
            "network": network,
            "outpoint": {"txid": "ab" * 32, "vout": 0},
            "pubkey": bytes(CKey(bytes.fromhex("11" * 32)).pub).hex(),
            "locktime": 4_102_444_800,
        }
    )


@pytest.fixture
def ready(tmp_path: Path) -> Iterator[MarketStore]:
    path = get_market_store_path(tmp_path)
    initialize_fresh_advertisements(
        advertisement_record_path(tmp_path), WALLET, NETWORK, history_confirmed=True
    )
    store = MarketStore(path, wallet_id=WALLET)
    commitments = get_used_commitments_path(tmp_path, create=False)
    commitments.parent.mkdir(mode=0o700, exist_ok=True)
    commitments.write_text(json.dumps({"used": [], "external_v1": {}}))
    store.activate_wallet(
        commitments,
        history_confirmed=True,
        bound_advertisements_network=NETWORK,
    )
    try:
        yield store
    finally:
        store.close()


def test_required_mode_stock_is_typed_and_period_zero_is_not_globally_delayed(
    ready: MarketStore,
    tmp_path: Path,
) -> None:
    authority = bond()
    assert ready.is_wallet_ledger and ready.wallet_state() == "ready"
    assert ready.advertisement_binding == (WALLET, NETWORK)
    with pytest.raises(MarketStoreUnavailableError, match="typed"):
        ready.add_inventory("bond", bond_resource(authority, 0), None)
    owner, certificate = CKey(bytes.fromhex("11" * 32)), CKey(bytes.fromhex("22" * 32))
    public = bytes(certificate.pub)
    credential = BondCredential(
        bond=authority,
        cert_pubkey=public.hex(),
        cert_expiry=1,
        cert_signature=owner.sign(
            bitcoin_message_hash_bytes(get_cert_msg(public, 1)), _ecdsa_sig_grind_low_r=False
        ).hex(),
        lease=sign_bond_lease(authority, 0, public.hex(), owner),
    ).model_dump(mode="json")
    with pytest.raises(MarketStoreUnavailableError, match="typed"):
        ready.add_inventory("bond", bond_resource(authority, 0), credential)
    ready.add_bond_inventory(authority, 0, credential)
    assert blocked_bond_outpoints(
        ready.path, [authority], 1, advertisement_binding=(WALLET, NETWORK)
    ) == frozenset({("ab" * 32, 0)})
    with MarketStore(ready.path, wallet_id=WALLET, create=False) as restarted:
        assert restarted.advertisement_binding == (WALLET, NETWORK)
    with sqlite3.connect(ready.path) as connection:
        # The previous decoder's deliberate format boundary, not a universal
        # claim about arbitrary older binaries.
        mode = connection.execute("SELECT value FROM metadata WHERE key='ledger_mode'").fetchone()[
            0
        ]
        assert mode not in {"standalone", "wallet"}


@pytest.mark.parametrize(
    "replacement", ["missing", "v1", "corrupt", "wrong-wallet", "wrong-network"]
)
def test_required_history_failure_blocks_new_stock_but_not_reconciliation_open(
    ready: MarketStore,
    tmp_path: Path,
    replacement: str,
) -> None:
    path = advertisement_record_path(tmp_path)
    path.unlink()
    if replacement == "v1":
        record_advertisement(path, 0)
    elif replacement == "corrupt":
        path.write_bytes(b'{"version":2,')
    elif replacement.startswith("wrong"):
        initialize_fresh_advertisements(
            path,
            "b2" * 32 if replacement == "wrong-wallet" else WALLET,
            "signet" if replacement == "wrong-network" else NETWORK,
            history_confirmed=True,
        )
    before = path.read_bytes() if path.exists() else None
    with MarketStore(ready.path, wallet_id=WALLET, create=False) as restarted:
        assert restarted.wallet_state() == "ready"
        assert restarted.hold_invoices(1) == []
        with pytest.raises(MarketStoreError):
            restarted.add_bond_inventory(bond(), 0)
        with pytest.raises(MarketStoreUnavailableError, match="typed"):
            restarted.add_inventory("bond", bond_resource(bond(), 0), None)
    assert (path.read_bytes() if path.exists() else None) == before


def test_proof_first_excludes_stock_and_stock_first_excludes_proof(
    ready: MarketStore, tmp_path: Path
) -> None:
    authority = bond()
    outpoint = (authority.outpoint.txid, authority.outpoint.vout)
    record_advertisement(
        advertisement_record_path(tmp_path),
        0,
        outpoint,
        wallet_id=WALLET,
        network=NETWORK,
        require_bound=True,
    )
    with pytest.raises(MarketStoreConflictError, match="advertised"):
        ready.add_bond_inventory(authority, 0)
    assert not blocked_bond_outpoints(
        ready.path, [authority], 1, advertisement_binding=(WALLET, NETWORK)
    )
    # The original proof no longer overlaps period 2. Stocking that future
    # authority first is durable evidence blocking proof in that period.
    ready.add_bond_inventory(authority, 2)
    assert blocked_bond_outpoints(
        ready.path, [authority], 2 * 2016, advertisement_binding=(WALLET, NETWORK)
    ) == frozenset({outpoint})


def test_busy_admission_never_waits_or_commits_and_wrong_network_refuses(
    ready: MarketStore,
    tmp_path: Path,
) -> None:
    with (
        locked_advertisements(
            advertisement_record_path(tmp_path), wallet_id=WALLET, network=NETWORK
        ),
        pytest.raises(MarketStoreUnavailableError, match="unavailable"),
    ):
        ready.add_bond_inventory(bond(), 0)
    with pytest.raises(MarketStoreError):
        ready.add_bond_inventory(bond("signet"), 0)
    with sqlite3.connect(ready.path) as connection:
        assert connection.execute("SELECT count(*) FROM inventory").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["recover", "rebind"])
def test_required_mode_maintenance_refuses_without_downgrading(
    ready: MarketStore,
    tmp_path: Path,
    operation: str,
) -> None:
    commitments = get_used_commitments_path(tmp_path, create=False)
    target = tmp_path / "moved/cmtdata/commitments.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(commitments.read_bytes())
    before = ready.path.read_bytes()
    with pytest.raises(MarketStoreUnavailableError, match="coupled recovery"):
        if operation == "recover":
            ready.recover_wallet(commitments, history_confirmed=True)
        else:
            MarketStore.rebind_wallet(
                ready.path,
                wallet_id=WALLET,
                previous_commitments_path=commitments,
                commitments_path=target,
                history_confirmed=True,
                writers_stopped=True,
            )
    assert ready.path.read_bytes() == before
    assert ready.advertisement_binding == (WALLET, NETWORK)


@pytest.mark.parametrize(
    "mutation", ["remove-network", "bad-wallet", "legacy-with-binding", "complete-downgrade"]
)
def test_required_metadata_is_indivisible_and_running_binding_is_sticky(
    ready: MarketStore,
    mutation: str,
) -> None:
    assert ready.advertisement_binding == (WALLET, NETWORK)
    with sqlite3.connect(ready.path) as connection:
        if mutation == "remove-network":
            connection.execute("DELETE FROM metadata WHERE key='bond_advertisement_network'")
        elif mutation == "bad-wallet":
            connection.execute(
                "UPDATE metadata SET value='bad' WHERE key='bond_advertisement_wallet_id'"
            )
        else:
            connection.execute("UPDATE metadata SET value='wallet' WHERE key='ledger_mode'")
            if mutation == "complete-downgrade":
                connection.execute("DELETE FROM metadata WHERE key LIKE 'bond_advertisement_%'")
    with pytest.raises(MarketStoreCorruptError):
        ready.add_inventory("bond", bond_resource(bond(), 0), None)
    with pytest.raises(MarketStoreCorruptError):
        _ = ready.advertisement_binding


def test_file_first_partial_activation_and_relocation_never_authorize_issuance(
    tmp_path: Path,
) -> None:
    path = get_market_store_path(tmp_path / "original")
    history = advertisement_record_path(tmp_path / "original")
    initialize_fresh_advertisements(history, WALLET, NETWORK, history_confirmed=True)
    with MarketStore(path, wallet_id=WALLET) as store:
        with pytest.raises(MarketStoreUnavailableError):
            store.add_bond_inventory(bond(), 0)
        store.activate_wallet(
            get_used_commitments_path(tmp_path / "original", create=False),
            history_confirmed=True,
            bound_advertisements_network=NETWORK,
        )
    shutil.copytree(tmp_path / "original", tmp_path / "copied")
    with pytest.raises(MarketStoreCorruptError, match="data-directory binding"):
        MarketStore(get_market_store_path(tmp_path / "copied"), wallet_id=WALLET, create=False)


def test_legacy_generic_insertion_and_conservative_typed_admission_keep_distinct_contracts(
    tmp_path: Path,
) -> None:
    path = get_market_store_path(tmp_path)
    record_advertisement(advertisement_record_path(tmp_path), 0)
    with MarketStore(path) as store:
        with pytest.raises(MarketStoreConflictError, match="advertised"):
            store.add_bond_inventory(bond(), 0)
        store.add_inventory("bond", bond_resource(bond(), 0), None)
        assert store.advertisement_binding is None
    assert json.loads(advertisement_record_path(tmp_path).read_bytes())["version"] == 1
