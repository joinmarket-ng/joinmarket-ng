from __future__ import annotations

import pytest

from jmcore.channel_input import (
    MAX_PARTICIPANTS,
    ChannelInputInvitePayload,
    ChannelInputKeyPair,
    ChannelInputValidationError,
    sign_payload,
)
from jmcore.channel_input_session import (
    MAX_CHANNEL_INPUT_REPLAY_SLOT_BYTES,
    MAX_CHANNEL_INPUT_REPLAY_SLOTS,
    ChannelInputActorRegistry,
    ChannelInputReplayLedger,
    ChannelInputReplayResult,
)


def _secret(index: int) -> bytes:
    return index.to_bytes(32, "big")


def _key(index: int) -> str:
    return ChannelInputKeyPair.from_secret(_secret(index)).public_key


def _signed_invite(index: int = 1, *, aux: int = 1) -> ChannelInputInvitePayload:
    return sign_payload(
        ChannelInputInvitePayload(
            session_id="11" * 32,
            attempt=3,
            signer_key=_key(index),
            network="regtest",
            expiry=1_900_000_000,
            max_parent_fee_sat=10_000,
            max_fee_rate_sat_vb=100,
            min_recovery_delta=6,
            max_recovery_delta=144,
        ),
        _secret(index),
        aux_randomness=bytes([aux]) * 32,
    )


def test_actor_registry_binds_verified_actors_bijectively() -> None:
    payload = _signed_invite()
    other_payload = _signed_invite(2, aux=2)
    registry = ChannelInputActorRegistry()

    registry.bind_verified("actor-a", payload)
    registry.bind("actor-a", payload.signer_key)
    registry.require_verified("actor-a", payload)

    with pytest.raises(ChannelInputValidationError, match="not bound"):
        registry.require_verified("actor-b", payload)
    with pytest.raises(ChannelInputValidationError, match="different signer"):
        registry.bind("actor-a", other_payload.signer_key)
    with pytest.raises(ChannelInputValidationError, match="different actor"):
        registry.bind("actor-b", payload.signer_key)
    with pytest.raises(ChannelInputValidationError, match="BIP340"):
        registry.bind_verified("actor-b", payload.model_copy(update={"sig": "00" * 64}))
    with pytest.raises(ChannelInputValidationError, match="not bound"):
        registry.require_verified("actor-a", other_payload)

    snapshot = registry.snapshot()
    assert snapshot == (("actor-a", payload.signer_key),)
    assert ChannelInputActorRegistry.from_snapshot(snapshot).snapshot() == snapshot
    assert ChannelInputActorRegistry.restore(snapshot).snapshot() == snapshot
    with pytest.raises(ChannelInputValidationError, match="unique and sorted"):
        ChannelInputActorRegistry.from_snapshot(
            (("actor-b", _key(2)), ("actor-a", payload.signer_key))
        )
    with pytest.raises(ChannelInputValidationError, match="BIP340 public key"):
        ChannelInputActorRegistry.from_snapshot((("actor-a", "00" * 32),))


def test_actor_registry_enforces_participant_cap() -> None:
    registry = ChannelInputActorRegistry()
    for index in range(1, MAX_PARTICIPANTS + 1):
        registry.bind(f"actor-{index}", _key(index))
    with pytest.raises(ChannelInputValidationError, match="participant limit"):
        registry.bind("overflow", _key(MAX_PARTICIPANTS + 1))


def test_replay_ledger_accepts_only_exact_signed_retransmissions() -> None:
    payload = _signed_invite(aux=3)
    resigned = sign_payload(payload, _secret(1), aux_randomness=b"\x04" * 32)
    ledger = ChannelInputReplayLedger()

    assert ledger.record("invite:actor-a", payload) is ChannelInputReplayResult.NEW
    snapshot = ledger.snapshot()
    assert ledger.record("invite:actor-a", payload) is ChannelInputReplayResult.DUPLICATE
    assert ledger.snapshot() == snapshot
    with pytest.raises(ChannelInputValidationError, match="different payload"):
        ledger.record("invite:actor-a", resigned)
    with pytest.raises(ChannelInputValidationError, match="BIP340"):
        ledger.record("invite:actor-b", payload.model_copy(update={"sig": "00" * 64}))

    assert ChannelInputReplayLedger.from_snapshot(snapshot).snapshot() == snapshot
    assert ChannelInputReplayLedger.restore(snapshot).snapshot() == snapshot
    with pytest.raises(ChannelInputValidationError, match="SHA256"):
        ChannelInputReplayLedger.from_snapshot((("invite:actor-a", "00" * 31),))
    with pytest.raises(ChannelInputValidationError, match="unique and sorted"):
        ChannelInputReplayLedger.from_snapshot((("slot-b", "00" * 32), ("slot-a", "11" * 32)))


@pytest.mark.parametrize(
    "slot",
    ("", "non-ascii-\u00e9", "space slot", "\x01", "a" * (MAX_CHANNEL_INPUT_REPLAY_SLOT_BYTES + 1)),
)
def test_replay_ledger_validates_slots(slot: str) -> None:
    with pytest.raises(ChannelInputValidationError, match="replay slot"):
        ChannelInputReplayLedger().record(slot, _signed_invite())


def test_replay_ledger_enforces_slot_cap() -> None:
    snapshot = tuple(
        (f"slot-{index:04d}", "00" * 32) for index in range(MAX_CHANNEL_INPUT_REPLAY_SLOTS)
    )
    ledger = ChannelInputReplayLedger.from_snapshot(snapshot)
    with pytest.raises(ChannelInputValidationError, match="slot limit"):
        ledger.record("overflow", _signed_invite())
    with pytest.raises(ChannelInputValidationError, match="slot limit"):
        ChannelInputReplayLedger.from_snapshot(tuple(sorted(snapshot + (("overflow", "00" * 32),))))
