from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import taker.channel_input_store as channel_input_store
from taker.channel_input_negotiation import (
    TakerChannelInputNegotiation,
    TakerChannelInputSnapshot,
    TakerChannelInputStage,
)
from taker.channel_input_store import (
    TakerChannelInputStore,
    TakerChannelInputStoreError,
    TakerChannelInputStoreLimitError,
)


def _snapshot(session_byte: str = "1") -> TakerChannelInputSnapshot:
    return TakerChannelInputNegotiation.create(
        network="regtest",
        local_actor="taker",
        selected_nicks=("maker",),
        secret_key=(1).to_bytes(32, "big"),
        session_id=session_byte * 64,
        attempt=1,
        expiry=2_000,
        max_parent_fee_sat=1_000,
        max_fee_rate_sat_vb=10,
        min_recovery_delta=1,
        max_recovery_delta=2,
    ).snapshot()


def test_save_load_delete_and_private_modes(tmp_path: Path) -> None:
    store = TakerChannelInputStore(tmp_path / "private", max_active_sessions=1)
    snapshot = _snapshot()
    store.save(snapshot)
    assert store.load(snapshot.invite.session_id, snapshot.invite.attempt) == snapshot
    record = next(store.directory.iterdir())
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    store.delete(snapshot)
    assert store.load(snapshot.invite.session_id, snapshot.invite.attempt) is None


def test_nick_identity_is_private_durable_and_not_a_snapshot(tmp_path: Path) -> None:
    store = TakerChannelInputStore(tmp_path / "private", max_active_sessions=1)

    first = store.load_or_create_nick_secret()
    second = store.load_or_create_nick_secret()

    assert first == second
    assert len(first) == 32
    identity_path = store.directory / "coordinator-nick.key"
    assert stat.S_IMODE(identity_path.stat().st_mode) == 0o600
    assert identity_path.read_text(encoding="ascii") == first.hex()
    assert store.load_all().records == ()
    assert store.load_all().safe


def test_invalid_nick_identity_fails_closed(tmp_path: Path) -> None:
    store = TakerChannelInputStore(tmp_path / "private", max_active_sessions=1)
    identity_path = store.directory / "coordinator-nick.key"
    identity_path.write_text("00" * 32, encoding="ascii")
    identity_path.chmod(0o600)

    with pytest.raises(TakerChannelInputStoreError):
        store.load_or_create_nick_secret()
    assert not store.load_all().safe


def test_capacity_and_corruption_are_fail_closed(tmp_path: Path) -> None:
    store = TakerChannelInputStore(tmp_path / "private", max_active_sessions=1)
    first = _snapshot("1")
    second = _snapshot("2")
    store.save(first)
    with pytest.raises(TakerChannelInputStoreLimitError):
        store.save(second)
    (store.directory / "unexpected.json").write_text("bad", encoding="ascii")
    report = store.load_all()
    assert report.records == (first,)
    assert report.corruptions
    with pytest.raises(TakerChannelInputStoreError):
        store.delete(first)


def test_symlink_records_are_reported_as_corrupt(tmp_path: Path) -> None:
    store = TakerChannelInputStore(tmp_path / "private", max_active_sessions=1)
    target = tmp_path / "target"
    target.write_text("{}", encoding="ascii")
    os.symlink(target, store.directory / ("11" * 32 + "-1-" + "22" * 32 + ".json"))
    assert store.load_all().corruptions


def test_canceled_excludes_active_capacity_but_cancel_intended_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        channel_input_store,
        "_validate_snapshot",
        lambda snapshot: (snapshot, "22" * 32),
    )
    canceled = _snapshot("1").model_copy(update={"stage": TakerChannelInputStage.CANCELED})
    active = _snapshot("2")
    store = TakerChannelInputStore(tmp_path / "canceled", max_active_sessions=1)
    store.save(canceled)
    store.save(active)
    counted = _snapshot("3").model_copy(update={"stage": TakerChannelInputStage.CANCEL_INTENDED})
    store = TakerChannelInputStore(tmp_path / "counted", max_active_sessions=1)
    store.save(counted)
    with pytest.raises(TakerChannelInputStoreLimitError):
        store.save(active)


def test_atomic_and_fsync_failures_are_wrapped_and_preserve_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TakerChannelInputStore(tmp_path / "private", max_active_sessions=1)
    snapshot = _snapshot()
    store.save(snapshot)
    original = next(store.directory.iterdir()).read_bytes()
    atomic_write = channel_input_store.atomic_write_private
    monkeypatch.setattr(
        channel_input_store,
        "atomic_write_private",
        lambda _path, _payload: (_ for _ in ()).throw(OSError("write")),
    )
    with pytest.raises(TakerChannelInputStoreError):
        store.save(snapshot)
    assert next(store.directory.iterdir()).read_bytes() == original
    monkeypatch.setattr(channel_input_store, "atomic_write_private", atomic_write)
    monkeypatch.setattr(
        channel_input_store,
        "_fsync_private_directory",
        lambda _path: (_ for _ in ()).throw(OSError("fsync")),
    )
    with pytest.raises(TakerChannelInputStoreError):
        store.save(snapshot)


def test_delete_fsync_and_load_boundary_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TakerChannelInputStore(tmp_path / "private", max_active_sessions=1)
    snapshot = _snapshot()
    store.save(snapshot)
    calls: list[Path] = []
    monkeypatch.setattr(
        channel_input_store, "_fsync_private_directory", lambda path: calls.append(path)
    )
    store.delete(snapshot)
    assert calls == [store.directory]
    store.save(snapshot)
    monkeypatch.setattr(
        channel_input_store,
        "_fsync_private_directory",
        lambda _path: (_ for _ in ()).throw(OSError("delete fsync")),
    )
    with pytest.raises(TakerChannelInputStoreError):
        store.delete(snapshot)
    monkeypatch.setattr(channel_input_store, "_fsync_private_directory", lambda _path: None)
    store.save(snapshot)
    record = next(store.directory.iterdir())
    record.rename(store.directory / "wrong.json")
    assert store.load_all().corruptions
    (store.directory / "oversize.json").write_bytes(
        b"x" * (channel_input_store.MAX_RECORD_BYTES + 1)
    )
    assert store.load_all().corruptions
    limited = TakerChannelInputStore(tmp_path / "limited", max_active_sessions=1)
    monkeypatch.setattr(channel_input_store, "MAX_TOTAL_RECORDS", 1)
    (limited.directory / "one.json").write_text("{}", encoding="ascii")
    (limited.directory / "two.json").write_text("{}", encoding="ascii")
    report = limited.load_all()
    assert report.records == ()
    assert report.corruptions == ("channel-input store exceeds maximum record count",)
