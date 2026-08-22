"""Tests for crash-durable maker channel-input snapshot storage."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest
from coincurve import PrivateKey
from jmcore.channel_input import ChannelInputInvitePayload, ChannelInputKeyPair, sign_payload
from jmcore.channel_input_config import ChannelInputConfig
from jmcore.secure_files import atomic_write_private
from test_channel_input_negotiation import _cancel, _proposal_approved

import maker.channel_input_store as channel_input_store
from maker.channel_input import MakerChannelInputNegotiation, MakerChannelInputSnapshot
from maker.channel_input_store import (
    MAX_ACTIVE_SESSIONS,
    MAX_RECORD_BYTES,
    MakerChannelInputStore,
    MakerChannelInputStoreCollisionError,
    MakerChannelInputStoreError,
    MakerChannelInputStoreLimitError,
)

_COORDINATOR_SECRET = (1).to_bytes(32, "big")


def _node(index: int) -> str:
    return PrivateKey(index.to_bytes(32, "big")).public_key.format(compressed=True).hex()


def _snapshot(
    *,
    session_id: str = "11" * 32,
    attempt: int = 1,
    participant_secret: int = 2,
) -> MakerChannelInputSnapshot:
    coordinator = ChannelInputKeyPair.from_secret(_COORDINATOR_SECRET)
    invite = cast(
        ChannelInputInvitePayload,
        sign_payload(
            ChannelInputInvitePayload(
                session_id=session_id,
                attempt=attempt,
                signer_key=coordinator.public_key,
                network="regtest",
                expiry=2_000,
                max_parent_fee_sat=2_000,
                max_fee_rate_sat_vb=100,
                min_recovery_delta=10,
                max_recovery_delta=50,
            ),
            _COORDINATOR_SECRET,
            aux_randomness=b"\x01" * 32,
        ),
    )
    config = ChannelInputConfig(
        enabled=True,
        lnd_grpc_url="localhost:10009",
        lnd_tls_cert_path=Path("/tmp/lnd.cert"),
        lnd_macaroon_path=Path("/tmp/admin.macaroon"),
        lnd_escrow_macaroon_path=Path("/tmp/escrow.macaroon"),
        allowed_channel_points=("10" * 32 + ":0",),
        allowed_peer_node_pubkeys=(_node(10),),
        min_channel_capacity_sat=100_000,
        max_channel_capacity_sat=100_000,
        max_claim_adjustment_sat=1_000,
        max_miner_fee_share_sat=1_000,
        recovery_delay_blocks=20,
    )
    negotiation = MakerChannelInputNegotiation.from_invite(
        local_actor="maker",
        coordinator_actor="coordinator",
        invite=invite,
        config=config,
        network="regtest",
        local_node_key=_node(3),
        key_pair=ChannelInputKeyPair.from_secret(participant_secret.to_bytes(32, "big")),
    )
    return negotiation.snapshot()


def _record_path(directory: Path, snapshot: MakerChannelInputSnapshot) -> Path:
    participant_key = MakerChannelInputNegotiation.from_snapshot(snapshot).participant_key
    return (
        directory / f"{snapshot.invite.session_id}-{snapshot.invite.attempt}-{participant_key}.json"
    )


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.parametrize("max_active_sessions", [0, True, MAX_ACTIVE_SESSIONS + 1])
def test_store_rejects_unbounded_or_nonpositive_cap(
    tmp_path: Path, max_active_sessions: int
) -> None:
    with pytest.raises(ValueError, match="max_active_sessions"):
        MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=max_active_sessions)


def test_save_load_update_and_restore_uses_deterministic_filename(tmp_path: Path) -> None:
    snapshot = _snapshot()
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    expected_path = _record_path(store.directory, snapshot)

    store.save(snapshot)
    store.save(snapshot)

    loaded = store.load(snapshot.invite.session_id, snapshot.invite.attempt)
    assert loaded == snapshot
    assert expected_path.exists()
    assert MakerChannelInputNegotiation.from_snapshot(loaded).snapshot() == snapshot
    assert store.load_all().records == (snapshot,)


def test_secret_is_private_in_record_and_not_in_store_error(tmp_path: Path) -> None:
    snapshot = _snapshot()
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    store.save(snapshot)
    record_path = _record_path(store.directory, snapshot)
    secret = snapshot.participant_secret_key_hex

    assert secret not in repr(snapshot)
    assert secret.encode() in record_path.read_bytes()
    assert _mode(record_path) == 0o600

    invalid = snapshot.model_copy(update={"participant_secret_key_hex": "not-a-secret"})
    with pytest.raises(MakerChannelInputStoreError) as raised:
        store.save(invalid)
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert raised.value.__cause__ is None


def test_nick_identity_is_private_stable_and_accepted_by_store_scan(tmp_path: Path) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)

    first = store.load_or_create_nick_secret()
    second = store.load_or_create_nick_secret()
    identity_path = store.directory / "participant-nick.key"

    assert first == second
    assert len(first) == 32
    assert identity_path.read_text(encoding="ascii") == first.hex()
    assert _mode(identity_path) == 0o600
    assert store.load_all().safe


@pytest.mark.parametrize("contents", (b"short", b"z" * 64))
def test_invalid_nick_identity_fails_closed(tmp_path: Path, contents: bytes) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    identity_path = store.directory / "participant-nick.key"
    atomic_write_private(identity_path, contents)

    with pytest.raises(MakerChannelInputStoreError, match="identity"):
        store.load_or_create_nick_secret()
    assert store.load_all().corruptions == ("participant-nick.key: invalid participant identity",)


def test_store_tightens_directory_and_record_modes(tmp_path: Path) -> None:
    directory = tmp_path / "channel-input"
    directory.mkdir(mode=0o755)
    snapshot = _snapshot()
    store = MakerChannelInputStore(directory, max_active_sessions=1)
    store.save(snapshot)
    record_path = _record_path(directory, snapshot)

    assert _mode(directory) == 0o700
    assert _mode(record_path) == 0o600
    record_path.chmod(0o644)

    assert store.load(snapshot.invite.session_id, snapshot.invite.attempt) == snapshot
    assert _mode(record_path) == 0o600


def test_save_fsyncs_parent_directory_after_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    snapshot = _snapshot()
    events: list[str] = []

    def write_then_record(path: Path, data: bytes) -> None:
        atomic_write_private(path, data)
        events.append("replace")

    def record_directory_fsync(directory: Path) -> None:
        assert events == ["replace"]
        assert _record_path(directory, snapshot).exists()
        events.append("fsync")

    monkeypatch.setattr("maker.channel_input_store.atomic_write_private", write_then_record)
    monkeypatch.setattr(channel_input_store, "_fsync_private_directory", record_directory_fsync)

    store.save(snapshot)

    assert events == ["replace", "fsync"]


def test_cap_allows_exact_update_but_rejects_new_session(tmp_path: Path) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    first = _snapshot()
    second = _snapshot(session_id="22" * 32)
    store.save(first)
    store.save(first)

    with pytest.raises(MakerChannelInputStoreLimitError):
        store.save(second)


def test_same_session_attempt_with_different_participant_is_collision(tmp_path: Path) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=2)
    store.save(_snapshot(participant_secret=2))

    with pytest.raises(MakerChannelInputStoreCollisionError):
        store.save(_snapshot(participant_secret=3))


def test_load_all_reports_preexisting_duplicate_session_attempt(tmp_path: Path) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=2)
    first = _snapshot(participant_secret=2)
    second = _snapshot(participant_secret=3)
    store.save(first)
    atomic_write_private(
        _record_path(store.directory, second), channel_input_store._serialize_snapshot(second)
    )

    report = store.load_all()

    assert report.records == (first, second)
    assert report.corruptions == (
        f"{_record_path(store.directory, first).name}: duplicate channel-input session attempt",
    )
    with pytest.raises(MakerChannelInputStoreError, match="corrupt"):
        store.load(first.invite.session_id, first.invite.attempt)


def test_load_all_reports_corrupt_json_and_load_fails_closed(tmp_path: Path) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    snapshot = _snapshot()
    store.save(snapshot)
    (store.directory / "malformed.json").write_text("not json", encoding="utf-8")

    report = store.load_all()

    assert report.records == (snapshot,)
    assert report.corruptions == ("malformed.json: invalid channel-input snapshot",)
    with pytest.raises(MakerChannelInputStoreError, match="corrupt"):
        store.load(snapshot.invite.session_id, snapshot.invite.attempt)
    with pytest.raises(MakerChannelInputStoreError, match="corrupt"):
        store.save(snapshot)


def test_load_all_reports_oversized_and_filename_mismatch_records(tmp_path: Path) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=2)
    snapshot = _snapshot()
    payload = channel_input_store._serialize_snapshot(snapshot)
    (store.directory / "oversized.json").write_bytes(b"x" * (MAX_RECORD_BYTES + 1))
    (store.directory / "wrong-name.json").write_bytes(payload)

    report = store.load_all()

    assert report.records == ()
    assert report.corruptions == (
        "oversized.json: invalid channel-input snapshot",
        "wrong-name.json: invalid channel-input snapshot",
    )


def test_load_all_reports_symlink_file_and_symlink_directory_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "channel-input"
    store = MakerChannelInputStore(directory, max_active_sessions=1)
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    (directory / "linked.json").symlink_to(target)
    temporary_name = f".{_record_path(directory, _snapshot()).name}.leftover.tmp"
    (directory / temporary_name).symlink_to(target)

    assert store.load_all().corruptions == (
        f"{temporary_name}: invalid channel-input snapshot",
        "linked.json: invalid channel-input snapshot",
    )

    link = tmp_path / "linked-directory"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(MakerChannelInputStoreError, match="directory"):
        MakerChannelInputStore(link, max_active_sessions=1)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are not available")
def test_load_all_reports_nonregular_entry(tmp_path: Path) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    os.mkfifo(store.directory / "record.json")

    assert store.load_all().corruptions == ("record.json: invalid channel-input snapshot",)


def test_atomic_write_failure_preserves_prior_valid_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    snapshot = _snapshot()
    store.save(snapshot)

    def fail_write(_: Path, __: bytes) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr("maker.channel_input_store.atomic_write_private", fail_write)
    with pytest.raises(MakerChannelInputStoreError, match="durably save"):
        store.save(snapshot)

    assert store.load(snapshot.invite.session_id, snapshot.invite.attempt) == snapshot


def test_invalid_snapshot_never_calls_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    invalid = _snapshot().model_copy(update={"participant_secret_key_hex": "invalid"})
    write_called = False

    def record_write(_: Path, __: bytes) -> None:
        nonlocal write_called
        write_called = True

    monkeypatch.setattr("maker.channel_input_store.atomic_write_private", record_write)
    with pytest.raises(MakerChannelInputStoreError, match="invalid"):
        store.save(invalid)

    assert not write_called


def test_delete_requires_exact_durable_snapshot_and_fsyncs(tmp_path: Path) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    snapshot = _snapshot()
    store.save(snapshot)

    with pytest.raises(MakerChannelInputStoreError, match="differs"):
        store.delete(_snapshot(session_id="22" * 32))
    assert store.load(snapshot.invite.session_id, snapshot.invite.attempt) == snapshot

    store.delete(snapshot)
    assert store.load(snapshot.invite.session_id, snapshot.invite.attempt) is None


def test_delete_refuses_corrupt_store_without_unlinking_target(tmp_path: Path) -> None:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    snapshot = _snapshot()
    store.save(snapshot)
    (store.directory / "corrupt.json").write_text("bad", encoding="ascii")

    with pytest.raises(MakerChannelInputStoreError, match="corrupt"):
        store.delete(snapshot)
    assert _record_path(store.directory, snapshot).exists()


def test_canceled_snapshot_does_not_consume_active_session_capacity(tmp_path: Path) -> None:
    negotiation, proposal, _ = _proposal_approved()
    assert negotiation.accept_cancel("coordinator", _cancel(proposal)) is None
    negotiation.record_canceled(
        tuple(
            sorted(
                (term.funding_outpoint for term in proposal.recipient_terms),
                key=lambda item: item.key(),
            )
        )
    )
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)

    store.save(negotiation.snapshot())
    store.save(_snapshot(session_id="22" * 32))

    assert len(store.load_all().records) == 2
