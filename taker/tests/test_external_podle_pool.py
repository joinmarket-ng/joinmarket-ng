"""External PoDLE import, durable storage, and opt-in taker routing tests."""

from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from _taker_test_helpers import make_taker_config, make_utxo
from jmcore.bitcoin import hash160
from jmcore.external_podle import ExternalPoDLE
from jmcore.models import Offer, OfferType
from jmcore.podle import generate_podle
from jmwallet.backends.base import UTXO, BlockchainBackend, UTXOVerificationResult

from taker.podle_manager import ExternalPoDLEPoolError, PoDLEManager
from taker.taker import MakerSession, PhaseResult, Taker


class _EmptyBlacklist:
    def is_blacklisted(self, _commitment: str) -> bool:
        return False


class _Blacklisted:
    def is_blacklisted(self, _commitment: str) -> bool:
        return True


def _record(*, index: int = 0, network: str = "regtest", txid: str = "cd" * 32) -> ExternalPoDLE:
    private_key = b"\x12" * 32
    proof = generate_podle(private_key, f"{txid}:2", index)
    return ExternalPoDLE(
        version=1,
        network=network,  # type: ignore[arg-type]
        outpoint={"txid": txid, "vout": 2},
        P=proof.p.hex(),
        P2=proof.p2.hex(),
        sig=proof.sig.hex(),
        e=proof.e.hex(),
        commitment=proof.commitment.hex(),
        index=index,
        scriptpubkey=(b"\x00\x14" + hash160(proof.p)).hex(),
        blockheight=100,
    )


def _backend(record: ExternalPoDLE, *, value: int = 1_000_000, confirmations: int = 6) -> AsyncMock:
    backend = AsyncMock()
    backend.requires_neutrino_metadata = Mock(return_value=False)
    backend.get_utxo = AsyncMock(
        return_value=UTXO(
            txid=record.outpoint.txid,
            vout=record.outpoint.vout,
            value=value,
            address="bcrt1qexternal",
            confirmations=confirmations,
            scriptpubkey=record.scriptpubkey,
            height=record.blockheight,
        )
    )
    return backend


class _ProcessBackend:
    """Minimal picklable backend for competing-consumer coverage."""

    def __init__(self, record: ExternalPoDLE):
        self.record = record

    def requires_neutrino_metadata(self) -> bool:
        return False

    async def get_utxo(self, txid: str, vout: int) -> UTXO | None:
        if (txid, vout) != (self.record.outpoint.txid, self.record.outpoint.vout):
            return None
        return UTXO(
            txid=txid,
            vout=vout,
            value=1_000_000,
            address="bcrt1qexternal",
            confirmations=6,
            scriptpubkey=self.record.scriptpubkey,
            height=self.record.blockheight,
        )


def _consume_in_process(
    data_dir: str,
    record_data: dict[str, object],
    ready: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    """Consume the same credential in a separate interpreter process."""
    record = ExternalPoDLE.model_validate(record_data)
    ready.wait(timeout=10)
    result = asyncio.run(_consume(PoDLEManager(Path(data_dir)), _ProcessBackend(record)))
    results.put(result is not None)


async def _consume(
    manager: PoDLEManager, backend: AsyncMock | _ProcessBackend, *, retries: int = 3
):
    return await manager.consume_external(
        backend=cast(BlockchainBackend, backend),
        network="regtest",
        cj_amount=1_000_000,
        min_confirmations=5,
        min_percent=20,
        max_retries=retries,
    )


@pytest.mark.asyncio
async def test_external_record_consumes_once_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    record = _record()
    manager = PoDLEManager(tmp_path)
    assert manager.import_external(record) is True

    consumed = await _consume(manager, _backend(record))
    assert consumed is not None
    assert consumed.utxo == record.outpoint.to_string()
    assert manager.external_count() == 0

    restarted = PoDLEManager(tmp_path)
    assert restarted.external_count() == 0
    assert record.commitment in restarted.used_commitments


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["wrong_network", "spent", "stale", "wrong_script", "blacklisted"])
async def test_external_consumption_fails_closed_for_invalid_backing_utxo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    record = _record()
    manager = PoDLEManager(tmp_path)
    manager.import_external(record)
    backend = _backend(record)
    if case == "wrong_network":
        network = "signet"
        monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    else:
        network = "regtest"
        monkeypatch.setattr(
            "taker.podle_manager.get_blacklist",
            lambda: _Blacklisted() if case == "blacklisted" else _EmptyBlacklist(),
        )
    if case == "spent":
        backend.get_utxo = AsyncMock(return_value=None)
    elif case == "stale":
        backend.get_utxo.return_value.confirmations = 4
    elif case == "wrong_script":
        backend.get_utxo.return_value.scriptpubkey = "0014" + "00" * 20

    result = await manager.consume_external(backend, network, 1_000_000, 5, 20, 3)

    assert result is None
    assert manager.external_count() == 1
    if case == "wrong_network":
        backend.get_utxo.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_consumption_honors_index_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    record = _record(index=2)
    manager = PoDLEManager(tmp_path)
    manager.import_external(record)
    backend = _backend(record)

    assert await _consume(manager, backend, retries=2) is None
    assert await _consume(manager, backend, retries=3) is not None


@pytest.mark.asyncio
async def test_external_consumption_uses_authoritative_metadata_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    record = _record()
    manager = PoDLEManager(tmp_path)
    manager.import_external(record)
    backend = AsyncMock()
    backend.requires_neutrino_metadata = Mock(return_value=True)
    backend.verify_utxo_with_metadata = AsyncMock(
        return_value=UTXOVerificationResult(
            valid=True,
            value=1_000_000,
            confirmations=5,
            scriptpubkey_matches=True,
        )
    )

    assert await _consume(manager, backend) is not None
    backend.verify_utxo_with_metadata.assert_awaited_once_with(
        txid=record.outpoint.txid,
        vout=record.outpoint.vout,
        scriptpubkey=record.scriptpubkey,
        blockheight=record.blockheight,
    )
    backend.get_utxo.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_node_wallet_utxo_without_height_remains_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    record = _record()
    manager = PoDLEManager(tmp_path)
    manager.import_external(record)
    backend = _backend(record)
    backend.get_utxo.return_value.height = None

    assert await _consume(manager, backend) is not None


def test_external_pool_fails_closed_on_corruption(tmp_path: Path) -> None:
    manager = PoDLEManager(tmp_path)
    manager.filepath.write_text("{not-json", encoding="utf-8")
    corrupted = PoDLEManager(tmp_path)

    assert corrupted.external_count() == 0
    with pytest.raises(ExternalPoDLEPoolError):
        corrupted.import_external(_record())


def test_external_pool_fails_closed_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PoDLEManager(tmp_path)
    monkeypatch.setattr("taker.podle_manager.os.replace", Mock(side_effect=OSError("disk failure")))

    with pytest.raises(ExternalPoDLEPoolError):
        manager.import_external(_record())
    assert manager.external_count() == 0


@pytest.mark.asyncio
async def test_concurrent_consumers_cannot_claim_the_same_external_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    record = _record()
    importer = PoDLEManager(tmp_path)
    importer.import_external(record)
    first = PoDLEManager(tmp_path)
    second = PoDLEManager(tmp_path)
    backend = _backend(record)

    consumed = await asyncio.gather(_consume(first, backend), _consume(second, backend))

    assert sum(item is not None for item in consumed) == 1
    assert PoDLEManager(tmp_path).external_count() == 0


def test_process_consumers_cannot_claim_the_same_external_record(tmp_path: Path) -> None:
    record = _record()
    PoDLEManager(tmp_path).import_external(record)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    results = context.Queue()
    worker_args = (str(tmp_path), record.model_dump(mode="json"), ready, results)
    first = context.Process(target=_consume_in_process, args=worker_args)
    second = context.Process(target=_consume_in_process, args=worker_args)

    first.start()
    second.start()
    ready.set()
    first.join(timeout=15)
    second.join(timeout=15)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert sorted([results.get(timeout=2), results.get(timeout=2)]) == [False, True]
    assert PoDLEManager(tmp_path).external_count() == 0


@pytest.mark.asyncio
async def test_external_candidate_cursor_reaches_record_after_invalid_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    manager = PoDLEManager(tmp_path)
    candidate_count = 33
    records = [_record(index=index, txid=f"{index + 1:064x}") for index in range(candidate_count)]
    for record in records:
        manager.import_external(record)

    valid = max(records, key=lambda record: record.commitment)
    backend = _backend(valid)
    backend.get_utxo.side_effect = lambda txid, vout: (
        _backend(valid).get_utxo.return_value
        if (txid, vout) == (valid.outpoint.txid, valid.outpoint.vout)
        else None
    )

    assert await _consume(manager, backend, retries=candidate_count) is None
    assert await _consume(manager, backend, retries=candidate_count) is not None
    assert manager.external_count() == candidate_count - 1


@pytest.mark.asyncio
async def test_external_transient_backend_failure_preserves_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    record = _record()
    manager = PoDLEManager(tmp_path)
    manager.import_external(record)
    backend = _backend(record)
    backend.get_utxo = AsyncMock(side_effect=OSError("backend unavailable"))

    assert await _consume(manager, backend) is None
    assert manager.external_count() == 1


def _external_only_taker(tmp_path: Path) -> Taker:
    wallet = AsyncMock()
    wallet.wallet_fingerprint = "deadbeef"
    wallet.get_locked_input_outpoints = Mock(return_value=set())
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=False)
    backend.requires_neutrino_metadata = Mock(return_value=False)
    return Taker(
        wallet,
        backend,
        make_taker_config(data_dir=tmp_path, external_podle_mode="only"),
    )


@pytest.mark.asyncio
async def test_external_only_never_uses_funding_input_for_podle(tmp_path: Path) -> None:
    taker = _external_only_taker(tmp_path)
    funding = make_utxo(value=1_000_000)
    taker.wallet.select_utxos = Mock(return_value=[funding])
    taker.wallet.get_all_utxos = Mock()
    taker.podle_manager = Mock()
    taker.podle_manager.consume_external = AsyncMock(return_value="external-commitment")
    taker.podle_manager.generate_fresh_commitment = Mock()
    taker._session.cj_amount = 500_000

    selected = taker._select_coinjoin_utxos_with_podle(
        0,
        500_000,
        lambda _address: (_ for _ in ()).throw(AssertionError("wallet key requested")),
        set(),
    )
    commitment = await taker._allocate_podle_commitment(
        selected,
        lambda _address: (_ for _ in ()).throw(AssertionError("wallet key requested")),
    )

    assert selected == [funding]
    assert commitment == "external-commitment"
    taker.wallet.get_all_utxos.assert_not_called()
    taker.wallet.select_utxos.assert_called_once_with(0, 500_000, 5, exclude=set())
    taker.podle_manager.consume_external.assert_awaited_once()
    taker.podle_manager.generate_fresh_commitment.assert_not_called()


@pytest.mark.asyncio
async def test_external_only_auth_replacement_consumes_without_local_fallback(
    tmp_path: Path,
) -> None:
    taker = _external_only_taker(tmp_path)
    taker._activate_coinjoin_log_context = Mock()
    taker._session.preselected_utxos = [make_utxo()]
    taker._session.cj_amount = 500_000
    taker._session.maker_sessions = {"J5maker": Mock(responded_auth=True)}
    taker.podle_manager = Mock()
    replacement_commitment = Mock()
    replacement_commitment.commitment.commitment.hex.return_value = "ab" * 32
    taker.podle_manager.consume_external = AsyncMock(return_value=replacement_commitment)
    taker.podle_manager.generate_fresh_commitment = Mock()

    rotated = await taker._rotate_commitment_for_auth_replacement(
        lambda _address: (_ for _ in ()).throw(AssertionError("wallet key requested"))
    )

    assert rotated is True
    assert taker._session.podle_commitment is replacement_commitment
    taker.podle_manager.consume_external.assert_awaited_once()
    taker.podle_manager.generate_fresh_commitment.assert_not_called()


@pytest.mark.asyncio
async def test_external_only_auth_replacement_exhaustion_never_uses_local_fallback(
    tmp_path: Path,
) -> None:
    taker = _external_only_taker(tmp_path)
    taker._session.preselected_utxos = [make_utxo()]
    taker._session.cj_amount = 500_000
    taker._session.maker_sessions = {"J5maker": Mock(responded_auth=True)}
    taker.podle_manager = Mock()
    taker.podle_manager.consume_external = AsyncMock(return_value=None)
    taker.podle_manager.generate_fresh_commitment = Mock()

    rotated = await taker._rotate_commitment_for_auth_replacement(Mock())

    assert rotated is False
    taker.podle_manager.consume_external.assert_awaited_once()
    taker.podle_manager.generate_fresh_commitment.assert_not_called()


def _offer(nick: str) -> Offer:
    return Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=100_000,
        maxsize=100_000_000,
        txfee=500,
        cjfee="0.00025",
        counterparty=nick,
    )


@pytest.mark.asyncio
async def test_external_only_blacklist_rotation_never_expands_or_uses_local_fallback(
    tmp_path: Path,
) -> None:
    taker = _external_only_taker(tmp_path)
    taker.config.minimum_makers = 2
    taker.config.taker_utxo_retries = 2
    taker._session.cj_amount = 500_000
    taker._session.maker_target_count = 2
    selected_offers = {nick: _offer(nick) for nick in ("J5maker1", "J5maker2")}
    taker._session.maker_sessions = {
        nick: MakerSession(nick=nick, offer=offer) for nick, offer in selected_offers.items()
    }
    taker._session.preselected_utxos = [make_utxo()]
    taker._session.podle_commitment = Mock()
    taker._session.podle_commitment.commitment.commitment.hex.return_value = "ab" * 32
    taker._session._expand_preselected_utxos_same_mixdepth = Mock(return_value=1)
    taker._session._phase_fill = AsyncMock(
        side_effect=[
            PhaseResult(
                success=True,
                failed_makers=["J5maker1"],
                blacklist_error=True,
                blacklist_makers=["J5maker1"],
            ),
            PhaseResult(success=True),
        ]
    )
    replacement_commitment = Mock()
    taker.podle_manager = Mock()
    taker.podle_manager.consume_external = AsyncMock(return_value=replacement_commitment)
    taker.podle_manager.generate_fresh_commitment = Mock()
    taker._activate_coinjoin_log_context = Mock()
    notifier = Mock()
    notifier.notify_coinjoin_start = AsyncMock()

    with (
        patch("taker.taker.get_notifier", return_value=notifier),
        patch("jmcore.commitment_blacklist.add_commitment"),
    ):
        succeeded = await taker._run_fill_with_replacements(
            destination="bcrt1qdestination",
            selected_offers=selected_offers,
            required_features=None,
            mixdepth=0,
            get_private_key=Mock(),
            max_replacement_attempts=0,
        )

    assert succeeded is True
    taker.podle_manager.consume_external.assert_awaited_once()
    taker.podle_manager.generate_fresh_commitment.assert_not_called()
    taker._session._expand_preselected_utxos_same_mixdepth.assert_not_called()
