"""A credential seller must never be a maker in the round that uses its credential.

A purchased external PoDLE commitment is known to the seller that issued it.
If that same entity also participates as a maker in the CoinJoin where the
commitment is revealed, it can link the commitment it sold to the transaction
it joined. Exclusion is therefore hard (never relaxed for liquidity), applies
to every selection pass of the round, and only matches makers whose complete
fidelity bond tuple was independently verified.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import make_taker_config, make_utxo
from jmcore.bitcoin import hash160
from jmcore.credential_market import BondReference
from jmcore.external_podle import ExternalPoDLE
from jmcore.models import Offer, OfferType
from jmcore.podle import generate_podle
from jmwallet.backends.base import UTXO, BlockchainBackend

from taker.models import MakerSession, PhaseResult
from taker.podle_manager import ExternalPoDLEPreview, PoDLEManager
from taker.taker import Taker, TakerState, bond_reference_key, nicks_proving_bonds

_SELLER_TXID = "ab" * 32
_SELLER_VOUT = 1
_SELLER_LOCKTIME = 1735689600


def test_reset_does_not_carry_seller_history_into_another_round(tmp_path: Path) -> None:
    record = _record()
    bond = _bond()
    taker = _external_taker(tmp_path, record, [])
    session = taker._session
    session.external_podle_preview = ExternalPoDLEPreview(record, bond)
    session.podle_seller_bonds = [bond]
    session.round_maker_bond_keys.add(bond_reference_key(bond))

    session.reset()

    assert session.external_podle_preview is None
    assert session.podle_seller_bonds == []
    assert session.round_maker_bond_keys == set()


class _EmptyBlacklist:
    def is_blacklisted(self, _commitment: str) -> bool:
        return False


def _seller_pubkey(seed: int = 0x11) -> str:
    """Return a valid secp256k1 point usable as a fidelity bond public key."""
    return generate_podle(bytes([seed]) * 32, f"{_SELLER_TXID}:0", 0).p.hex()


def _bond(
    *,
    pubkey: str | None = None,
    txid: str = _SELLER_TXID,
    vout: int = _SELLER_VOUT,
    locktime: int = _SELLER_LOCKTIME,
    network: str = "regtest",
) -> BondReference:
    return BondReference(
        network=network,  # type: ignore[arg-type]
        outpoint={"txid": txid, "vout": vout},  # type: ignore[arg-type]
        pubkey=pubkey if pubkey is not None else _seller_pubkey(),
        locktime=locktime,
    )


def _record(
    *, index: int = 0, txid: str = "cd" * 32, network: str = "regtest", key: int = 0x12
) -> ExternalPoDLE:
    # The commitment is derived from the key and NUMS index only, so distinct
    # credentials need distinct keys.
    proof = generate_podle(bytes([key]) * 32, f"{txid}:2", index)
    return ExternalPoDLE(
        version=1,
        network=network,  # type: ignore[arg-type]
        outpoint={"txid": txid, "vout": 2},  # type: ignore[arg-type]
        P=proof.p.hex(),
        P2=proof.p2.hex(),
        sig=proof.sig.hex(),
        e=proof.e.hex(),
        commitment=proof.commitment.hex(),
        index=index,
        scriptpubkey=(b"\x00\x14" + hash160(proof.p)).hex(),
        blockheight=100,
    )


def _bond_data(bond: BondReference, *, cert_expiry: int = 500) -> dict[str, Any]:
    return {
        "utxo_txid": bond.outpoint.txid,
        "utxo_vout": bond.outpoint.vout,
        "utxo_pub": bond.pubkey,
        "locktime": bond.locktime,
        "cert_expiry": cert_expiry,
    }


def _offer(
    nick: str,
    *,
    bond: BondReference | None = None,
    verified: bool | None = None,
    bond_data: dict[str, Any] | None = None,
) -> Offer:
    # Bondless offers must quote a zero fee to stay selectable under the
    # default bondless allowance policy.
    offer = Offer(
        counterparty=nick,
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=1_000,
        maxsize=100_000_000,
        txfee=0,
        cjfee=0,
    )
    if bond is not None:
        offer.fidelity_bond_data = _bond_data(bond)
    if bond_data is not None:
        offer.fidelity_bond_data = bond_data
    offer.fidelity_bond_verified = verified
    return offer


def _backend_for(record: ExternalPoDLE) -> AsyncMock:
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=False)
    backend.requires_neutrino_metadata = Mock(return_value=False)
    backend.can_estimate_fee = Mock(return_value=False)
    backend.get_mempool_min_fee = AsyncMock(return_value=None)
    backend.get_utxo = AsyncMock(
        return_value=UTXO(
            txid=record.outpoint.txid,
            vout=record.outpoint.vout,
            value=100_000_000,
            address="bcrt1qexternal",
            confirmations=10,
            scriptpubkey=record.scriptpubkey,
            height=record.blockheight,
        )
    )
    return backend


def _wallet(utxos: list) -> AsyncMock:
    wallet = AsyncMock()
    wallet.mixdepth_count = 5
    wallet.get_utxos = AsyncMock(return_value=utxos)
    wallet.get_all_utxos = Mock(return_value=list(utxos))
    wallet.get_locked_input_outpoints = Mock(return_value=set())
    wallet.select_utxos = Mock(return_value=list(utxos))
    wallet.reserve_coinjoin_inputs = Mock(return_value=True)
    wallet.renew_coinjoin_inputs = Mock(return_value=True)
    wallet.release_coinjoin_inputs = Mock()
    return wallet


def _external_taker(
    tmp_path: Path,
    record: ExternalPoDLE,
    offers: list[Offer],
    *,
    counterparty_count: int = 2,
    minimum_makers: int = 2,
) -> Taker:
    utxo = make_utxo(txid_char="a", value=25_000_000, confirmations=10)
    wallet = _wallet([utxo])
    config = make_taker_config(
        data_dir=tmp_path,
        counterparty_count=counterparty_count,
        minimum_makers=minimum_makers,
        taker_utxo_age=5,
        taker_utxo_amtpercent=20,
        fee_rate=1.0,
        external_podle_mode="only",
    )
    taker = Taker(wallet, _backend_for(record), config)
    taker.directory_client.fetch_orderbook = AsyncMock(return_value=offers)  # type: ignore[method-assign]
    taker._update_offers_with_bond_values = AsyncMock(return_value=None)  # type: ignore[method-assign]
    # The round stops right after maker selection and PoDLE allocation.
    taker._run_fill_with_replacements = AsyncMock(return_value=False)  # type: ignore[method-assign]
    return taker


@pytest.fixture(autouse=True)
def _no_blacklist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())


def test_only_a_completely_verified_bond_tuple_identifies_the_seller() -> None:
    bond = _bond()
    other_key = _seller_pubkey(0x22)
    offers = [
        _offer("J5exact", bond=bond, verified=True),
        _offer("J5unverified", bond=bond, verified=None),
        _offer("J5rejected", bond=bond, verified=False),
        _offer("J5sameoutpoint", bond=_bond(pubkey=other_key), verified=True),
        _offer("J5otherlocktime", bond=_bond(locktime=_SELLER_LOCKTIME + 1), verified=True),
        _offer("J5otheroutpoint", bond=_bond(txid="ef" * 32), verified=True),
        _offer("J5bondless", verified=True),
        _offer(
            "J5partialdata",
            verified=True,
            bond_data={"utxo_txid": bond.outpoint.txid, "utxo_pub": bond.pubkey},
        ),
    ]

    keys = {bond_reference_key(bond)}
    assert nicks_proving_bonds(offers, keys, "regtest") == {"J5exact"}
    # A bond verified on another network is a different identity.
    assert nicks_proving_bonds(offers, keys, "signet") == set()
    assert nicks_proving_bonds(offers, set(), "regtest") == set()


@pytest.mark.asyncio
async def test_initial_selection_hard_excludes_the_credential_seller(tmp_path: Path) -> None:
    record = _record()
    seller_bond = _bond()
    offers = [
        _offer("J5seller", bond=seller_bond, verified=True),
        _offer("J5maker1"),
        _offer("J5maker2"),
    ]
    taker = _external_taker(tmp_path, record, offers)
    assert taker.podle_manager.import_external(record, seller_bond=seller_bond) is True

    result = await taker.do_coinjoin(
        amount=5_000_000,
        destination="bcrt1qxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        mixdepth=0,
    )

    assert result is None  # the mocked fill phase ends the round
    assert set(taker._session.maker_sessions) == {"J5maker1", "J5maker2"}
    assert taker._session.podle_seller_bonds == [seller_bond]
    assert taker._seller_separation.excluded_nicks() == {"J5seller"}
    # The credential was claimed exactly once, after maker selection.
    assert taker.podle_manager.external_count() == 0
    assert taker._session.podle_commitment is not None


@pytest.mark.asyncio
async def test_seller_exclusion_is_never_relaxed_for_liquidity(tmp_path: Path) -> None:
    record = _record()
    seller_bond = _bond()
    offers = [
        _offer("J5seller", bond=seller_bond, verified=True),
        _offer("J5maker1"),
    ]
    taker = _external_taker(tmp_path, record, offers)
    taker.podle_manager.import_external(record, seller_bond=seller_bond)

    result = await taker.do_coinjoin(
        amount=5_000_000,
        destination="bcrt1qxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        mixdepth=0,
    )

    assert result is None
    assert taker.state == TakerState.FAILED
    assert "J5seller" not in taker._session.maker_sessions
    # The round failed before revealing anything, so the credential survives.
    assert taker.podle_manager.external_count() == 1


@pytest.mark.asyncio
async def test_sweep_selection_hard_excludes_the_credential_seller(tmp_path: Path) -> None:
    record = _record()
    seller_bond = _bond()
    offers = [
        _offer("J5seller", bond=seller_bond, verified=True),
        _offer("J5maker1"),
        _offer("J5maker2"),
    ]
    taker = _external_taker(tmp_path, record, offers)
    taker.podle_manager.import_external(record, seller_bond=seller_bond)

    result = await taker.do_coinjoin(
        amount=0,
        destination="bcrt1qxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        mixdepth=0,
    )

    assert result is None
    assert set(taker._session.maker_sessions) == {"J5maker1", "J5maker2"}
    assert taker._session.podle_seller_bonds == [seller_bond]
    assert taker._session.podle_commitment is not None


async def _run_auth_replacement(tmp_path: Path, *, seller_excluded: bool) -> AsyncMock:
    """Drive one auth-stage replacement pass and report the offers it picked."""
    record = _record()
    seller_bond = _bond()
    offers = [
        _offer("J5seller", bond=seller_bond, verified=True),
        _offer("J5maker1"),
        _offer("J5maker2"),
    ]
    taker = _external_taker(tmp_path, record, offers, counterparty_count=2, minimum_makers=1)
    taker.podle_manager.import_external(record, seller_bond=seller_bond)
    taker.orderbook_manager.update_offers(offers)
    taker._session.cj_amount = 5_000_000
    taker._session.maker_target_count = 2
    taker._session.podle_seller_bonds = [seller_bond] if seller_excluded else []
    taker._session.maker_sessions = {
        "J5maker1": MakerSession(nick="J5maker1", offer=offers[1], responded_auth=True)
    }
    # One maker dropped out, so the round tries to restore its target. The
    # seller is the only remaining candidate.
    taker._session._phase_auth = AsyncMock(  # type: ignore[method-assign]
        return_value=PhaseResult(success=True, failed_makers=["J5maker2"])
    )
    fill_replacements = AsyncMock(return_value=True)
    taker._fill_replacement_makers = fill_replacements  # type: ignore[method-assign]

    succeeded = await taker._run_auth_with_replacements(
        required_features=None,
        get_private_key=Mock(),
        max_replacement_attempts=1,
    )

    assert succeeded is True
    assert "J5seller" not in taker._session.maker_sessions
    return fill_replacements


@pytest.mark.asyncio
async def test_replacement_selection_still_excludes_the_seller(tmp_path: Path) -> None:
    fill_replacements = await _run_auth_replacement(tmp_path, seller_excluded=True)

    # The round continues short-handed instead of replacing with the seller.
    fill_replacements.assert_not_awaited()


@pytest.mark.asyncio
async def test_replacement_selection_would_pick_that_maker_without_the_exclusion(
    tmp_path: Path,
) -> None:
    fill_replacements = await _run_auth_replacement(tmp_path, seller_excluded=False)

    fill_replacements.assert_awaited_once()
    assert fill_replacements.await_args is not None
    assert set(fill_replacements.await_args.args[0]) == {"J5seller"}


@pytest.mark.asyncio
async def test_rotation_refuses_a_credential_sold_by_a_participating_maker(
    tmp_path: Path,
) -> None:
    record = _record()
    seller_bond = _bond()
    offers = [_offer("J5seller", bond=seller_bond, verified=True), _offer("J5maker1")]
    taker = _external_taker(tmp_path, record, offers)
    taker.podle_manager.import_external(record, seller_bond=seller_bond)
    taker.orderbook_manager.update_offers(offers)
    taker._session.cj_amount = 5_000_000
    taker._session.preselected_utxos = [make_utxo()]
    taker._session.maker_sessions = {
        "J5seller": MakerSession(nick="J5seller", offer=offers[0], responded_auth=True)
    }

    rotated = await taker._rotate_commitment_for_auth_replacement(Mock())

    assert rotated is False
    # The only pool credential belongs to a maker already in the round, so it
    # must stay unused rather than be revealed to its own seller.
    assert taker.podle_manager.external_count() == 1


async def _run_auth_rotation_with_candidate(
    tmp_path: Path, *, credential_seller: BondReference
) -> tuple[Taker, AsyncMock]:
    """Drive the real auth replacement flow that rotates before filling.

    The round has already revealed its commitment, so the only replacement
    candidate (``J5candidate``) can be contacted solely after a fresh
    credential is allocated.
    """
    record = _record()
    candidate_bond = _bond()
    offers = [
        _offer("J5candidate", bond=candidate_bond, verified=True),
        _offer("J5maker1"),
    ]
    taker = _external_taker(tmp_path, record, offers, counterparty_count=2, minimum_makers=1)
    taker._activate_coinjoin_log_context = Mock()  # type: ignore[method-assign]
    taker.podle_manager.import_external(record, seller_bond=credential_seller)
    taker.orderbook_manager.update_offers(offers)
    taker._session.cj_amount = 5_000_000
    taker._session.maker_target_count = 2
    taker._session.preselected_utxos = [make_utxo()]
    taker._session.maker_sessions = {
        "J5maker1": MakerSession(nick="J5maker1", offer=offers[1], responded_auth=True)
    }
    taker._session._phase_auth = AsyncMock(  # type: ignore[method-assign]
        return_value=PhaseResult(success=True, failed_makers=["J5gone"], podle_revealed=True)
    )
    fill_replacements = AsyncMock(return_value=True)
    taker._fill_replacement_makers = fill_replacements  # type: ignore[method-assign]

    succeeded = await taker._run_auth_with_replacements(
        required_features=None,
        get_private_key=Mock(),
        max_replacement_attempts=1,
    )

    assert succeeded is True
    return taker, fill_replacements


@pytest.mark.asyncio
async def test_auth_rotation_refuses_a_credential_sold_by_a_pending_replacement(
    tmp_path: Path,
) -> None:
    # The seller of the pool credential is the maker the round is about to
    # contact: rotating into it would reveal the commitment to its own seller.
    taker, fill_replacements = await _run_auth_rotation_with_candidate(
        tmp_path, credential_seller=_bond()
    )

    fill_replacements.assert_not_awaited()
    assert taker.podle_manager.external_count() == 1
    assert "J5candidate" not in taker._session.maker_sessions


@pytest.mark.asyncio
async def test_auth_rotation_proceeds_when_the_candidate_is_not_the_seller(
    tmp_path: Path,
) -> None:
    # Control for the test above: the same flow reaches rotation and fills the
    # candidate when an unrelated seller issued the credential.
    taker, fill_replacements = await _run_auth_rotation_with_candidate(
        tmp_path, credential_seller=_bond(txid="ef" * 32, pubkey=_seller_pubkey(0x55))
    )

    fill_replacements.assert_awaited_once()
    assert fill_replacements.await_args is not None
    assert set(fill_replacements.await_args.args[0]) == {"J5candidate"}
    assert taker.podle_manager.external_count() == 0


@pytest.mark.asyncio
async def test_a_nick_advertising_the_seller_bond_later_is_still_excluded(
    tmp_path: Path,
) -> None:
    record = _record()
    seller_bond = _bond()
    # Nobody advertises the seller's bond while the credential is claimed.
    offers = [_offer("J5maker1"), _offer("J5maker2")]
    taker = _external_taker(tmp_path, record, offers, counterparty_count=2, minimum_makers=2)
    taker.podle_manager.import_external(record, seller_bond=seller_bond)
    taker._activate_coinjoin_log_context = Mock()  # type: ignore[method-assign]

    await taker.do_coinjoin(
        amount=5_000_000,
        destination="bcrt1qxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        mixdepth=0,
    )

    assert taker._session.podle_seller_bonds == [seller_bond]
    # The seller's bond now shows up under a nick the round never saw.
    refreshed = [*offers, _offer("J5newnick", bond=seller_bond, verified=True)]
    taker.orderbook_manager.update_offers(refreshed)
    assert taker._seller_separation.excluded_nicks() == {"J5newnick"}

    taker._session.maker_target_count = 3
    taker._session._phase_auth = AsyncMock(return_value=PhaseResult(success=True))  # type: ignore[method-assign]
    fill_replacements = AsyncMock(return_value=True)
    taker._fill_replacement_makers = fill_replacements  # type: ignore[method-assign]

    succeeded = await taker._run_auth_with_replacements(
        required_features=None,
        get_private_key=Mock(),
        max_replacement_attempts=1,
    )

    assert succeeded is True
    fill_replacements.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_maker_that_left_the_round_still_blocks_its_own_credential(
    tmp_path: Path,
) -> None:
    record = _record()
    seller_bond = _bond()
    offers = [_offer("J5seller", bond=seller_bond, verified=True), _offer("J5maker1")]
    taker = _external_taker(tmp_path, record, offers, counterparty_count=2, minimum_makers=1)
    taker.podle_manager.import_external(record, seller_bond=seller_bond)
    taker.orderbook_manager.update_offers(offers)
    # The round selected and contacted the seller's maker, which then dropped
    # out and stopped advertising entirely.
    taker._seller_separation.record_offered_makers(offers)
    taker._session.cj_amount = 5_000_000
    taker._session.preselected_utxos = [make_utxo()]
    taker._session.maker_sessions = {
        "J5maker1": MakerSession(nick="J5maker1", offer=offers[1], responded_auth=True)
    }
    taker.orderbook_manager.update_offers([offers[1]])

    rotated = await taker._rotate_commitment_for_auth_replacement(Mock())

    assert rotated is False
    assert taker.podle_manager.external_count() == 1


@pytest.mark.asyncio
async def test_rewritten_seller_provenance_cannot_be_claimed(tmp_path: Path) -> None:
    record = _record()
    manager = PoDLEManager(tmp_path)
    manager.import_external(record, seller_bond=_bond())
    preview = await manager.preview_external(
        backend=cast(BlockchainBackend, _backend_for(record)),
        network="regtest",
        cj_amount=5_000_000,
        min_confirmations=5,
        min_percent=20,
        max_retries=3,
    )
    assert preview is not None

    # Another writer swaps the stored provenance after the exclusion was made.
    state = json.loads(manager.filepath.read_text(encoding="utf-8"))
    state["external_v1_sellers"][record.commitment] = _bond(pubkey=_seller_pubkey(0x66)).model_dump(
        mode="json"
    )
    manager.filepath.write_text(json.dumps(state), encoding="utf-8")

    assert manager.claim_previewed_external(preview) is None
    assert PoDLEManager(tmp_path).external_count() == 1


@pytest.mark.asyncio
async def test_lost_preview_race_fails_instead_of_substituting_another_seller(
    tmp_path: Path,
) -> None:
    mine = _record(txid="11" * 32, key=0x12)
    other = _record(txid="22" * 32, key=0x13)
    seller_bond = _bond()
    taker = _external_taker(tmp_path, mine, [_offer("J5maker1"), _offer("J5maker2")])
    taker.podle_manager.import_external(mine, seller_bond=seller_bond)
    taker.podle_manager.import_external(other, seller_bond=_bond(pubkey=_seller_pubkey(0x33)))

    preview = await taker.podle_manager.preview_external(
        backend=cast(BlockchainBackend, taker.backend),
        network="regtest",
        cj_amount=5_000_000,
        min_confirmations=5,
        min_percent=20,
        max_retries=3,
    )
    assert preview is not None
    taker._session.external_podle_preview = preview

    # A concurrent round on the same wallet burns exactly that record first.
    competitor = PoDLEManager(tmp_path)
    assert (
        await competitor.consume_external(
            backend=cast(BlockchainBackend, _backend_for(preview.record)),
            network="regtest",
            cj_amount=5_000_000,
            min_confirmations=5,
            min_percent=20,
            max_retries=3,
        )
        is not None
    )

    claimed = await taker._allocate_podle_commitment([], Mock())

    assert claimed is None
    # The other seller's credential is still untouched: no silent substitution.
    assert PoDLEManager(tmp_path).external_count() == 1


def test_seller_provenance_survives_restart_and_is_not_rewritten(tmp_path: Path) -> None:
    record = _record()
    seller_bond = _bond()
    manager = PoDLEManager(tmp_path)
    assert manager.import_external(record, seller_bond=seller_bond) is True

    restarted = PoDLEManager(tmp_path)
    assert restarted.external_v1_sellers[record.commitment] == seller_bond
    # Re-importing the same credential neither duplicates it nor rewrites the
    # provenance that the round-time exclusion depends on.
    assert (
        restarted.import_external(record, seller_bond=_bond(pubkey=_seller_pubkey(0x44))) is False
    )
    assert PoDLEManager(tmp_path).external_v1_sellers[record.commitment] == seller_bond


def test_seller_bond_must_match_the_credential_network(tmp_path: Path) -> None:
    manager = PoDLEManager(tmp_path)
    with pytest.raises(ValueError):
        manager.import_external(_record(), seller_bond=_bond(network="signet"))
    assert manager.external_count() == 0


@pytest.mark.asyncio
async def test_pool_without_provenance_keeps_working_and_excludes_nobody(
    tmp_path: Path,
) -> None:
    record = _record()
    seeded = PoDLEManager(tmp_path)
    seeded.import_external(record)
    # Rewrite the sidecar exactly as an older version left it: no seller map.
    state = json.loads(seeded.filepath.read_text(encoding="utf-8"))
    del state["external_v1_sellers"]
    seeded.filepath.write_text(json.dumps(state), encoding="utf-8")

    offers = [_offer("J5maker1"), _offer("J5maker2")]
    taker = _external_taker(tmp_path, record, offers)
    assert taker.podle_manager.external_count() == 1

    result = await taker.do_coinjoin(
        amount=5_000_000,
        destination="bcrt1qxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        mixdepth=0,
    )

    assert result is None  # the mocked fill phase ends the round
    assert set(taker._session.maker_sessions) == {"J5maker1", "J5maker2"}
    assert taker._session.podle_seller_bonds == []
    assert taker._seller_separation.excluded_nicks() == set()
    assert taker.podle_manager.external_count() == 0
