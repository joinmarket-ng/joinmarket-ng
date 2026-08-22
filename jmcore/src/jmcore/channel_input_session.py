"""Runtime-neutral authenticated actor and replay state for channel-input v1."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from enum import StrEnum

from jmcore.channel_input import (
    MAX_CHANNELS,
    MAX_PARTICIPANTS,
    ChannelInputPayload,
    ChannelInputValidationError,
    _validate_xonly_key,
    canonical_json,
    verify_payload,
)

MAX_CHANNEL_INPUT_REPLAY_SLOT_BYTES = 256
MAX_CHANNEL_INPUT_REPLAY_SLOTS = MAX_PARTICIPANTS * (MAX_CHANNELS * 6 + 8)


class ChannelInputReplayResult(StrEnum):
    """Outcome of recording a signed payload in one semantic replay slot."""

    NEW = "new"
    DUPLICATE = "duplicate"


def _validate_actor(actor: str) -> None:
    if not isinstance(actor, str) or not actor:
        raise ChannelInputValidationError("actor identifier must be a non-empty string")


def _validate_signer_key(signer_key: str) -> None:
    if not isinstance(signer_key, str):
        raise ChannelInputValidationError("signer_key must be a valid BIP340 public key")
    try:
        _validate_xonly_key(signer_key, "signer_key")
    except ValueError as exc:
        raise ChannelInputValidationError(str(exc)) from exc


def _require_valid_payload(payload: ChannelInputPayload) -> None:
    if not isinstance(payload, ChannelInputPayload) or not verify_payload(payload):
        raise ChannelInputValidationError("payload BIP340 signature is invalid")


class ChannelInputActorRegistry:
    """Maintain a bounded bijection between authenticated actors and signer keys."""

    def __init__(self) -> None:
        self._actor_keys: dict[str, str] = {}
        self._key_actors: dict[str, str] = {}

    def bind(self, actor: str, signer_key: str) -> None:
        """Bind an authenticated actor to one signer key, rejecting substitutions."""

        _validate_actor(actor)
        _validate_signer_key(signer_key)
        bound_key = self._actor_keys.get(actor)
        if bound_key is not None:
            if bound_key != signer_key:
                raise ChannelInputValidationError(
                    "actor is already bound to a different signer key"
                )
            return
        bound_actor = self._key_actors.get(signer_key)
        if bound_actor is not None:
            raise ChannelInputValidationError("signer key is already bound to a different actor")
        if len(self._actor_keys) >= MAX_PARTICIPANTS:
            raise ChannelInputValidationError(
                "channel input actor registry exceeds participant limit"
            )
        self._actor_keys[actor] = signer_key
        self._key_actors[signer_key] = actor

    def bind_verified(self, actor: str, payload: ChannelInputPayload) -> None:
        """Verify a payload before binding its signer to an authenticated actor."""

        _require_valid_payload(payload)
        self.bind(actor, payload.signer_key)

    def require_verified(self, actor: str, payload: ChannelInputPayload) -> None:
        """Require a valid signature from the actor's already established signer key."""

        _require_valid_payload(payload)
        _validate_actor(actor)
        if self._actor_keys.get(actor) != payload.signer_key:
            raise ChannelInputValidationError("actor is not bound to payload signer key")

    def snapshot(self) -> tuple[tuple[str, str], ...]:
        """Return immutable actor-to-signer bindings in canonical actor order."""

        return tuple(sorted(self._actor_keys.items()))

    @classmethod
    def from_snapshot(cls, snapshot: Sequence[tuple[str, str]]) -> ChannelInputActorRegistry:
        """Restore a registry only from a canonical, valid immutable snapshot."""

        entries = _validated_binding_snapshot(snapshot)
        registry = cls()
        for actor, signer_key in entries:
            registry.bind(actor, signer_key)
        return registry

    @classmethod
    def restore(cls, snapshot: Sequence[tuple[str, str]]) -> ChannelInputActorRegistry:
        """Alias for :meth:`from_snapshot` for state restoration call sites."""

        return cls.from_snapshot(snapshot)


class ChannelInputReplayLedger:
    """Record exact signed payload retransmissions by state-machine-owned slots."""

    def __init__(self) -> None:
        self._slot_hashes: dict[str, str] = {}

    def record(self, slot: str, payload: ChannelInputPayload) -> ChannelInputReplayResult:
        """Record a valid signed payload, accepting only exact retransmissions."""

        _validate_slot(slot)
        _require_valid_payload(payload)
        payload_hash = hashlib.sha256(canonical_json(payload)).hexdigest()
        existing_hash = self._slot_hashes.get(slot)
        if existing_hash is not None:
            if existing_hash == payload_hash:
                return ChannelInputReplayResult.DUPLICATE
            raise ChannelInputValidationError("replay slot already contains a different payload")
        if len(self._slot_hashes) >= MAX_CHANNEL_INPUT_REPLAY_SLOTS:
            raise ChannelInputValidationError("channel input replay ledger exceeds slot limit")
        self._slot_hashes[slot] = payload_hash
        return ChannelInputReplayResult.NEW

    def snapshot(self) -> tuple[tuple[str, str], ...]:
        """Return immutable slot-to-complete-payload-hash entries in canonical order."""

        return tuple(sorted(self._slot_hashes.items()))

    @classmethod
    def from_snapshot(cls, snapshot: Sequence[tuple[str, str]]) -> ChannelInputReplayLedger:
        """Restore a ledger only from canonical slots and complete payload hashes."""

        entries = _validated_replay_snapshot(snapshot)
        ledger = cls()
        ledger._slot_hashes = dict(entries)
        return ledger

    @classmethod
    def restore(cls, snapshot: Sequence[tuple[str, str]]) -> ChannelInputReplayLedger:
        """Alias for :meth:`from_snapshot` for state restoration call sites."""

        return cls.from_snapshot(snapshot)


def _validated_binding_snapshot(snapshot: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    entries = _validate_snapshot_entries(snapshot, "actor registry snapshot")
    if len(entries) > MAX_PARTICIPANTS:
        raise ChannelInputValidationError("channel input actor registry exceeds participant limit")
    for actor, signer_key in entries:
        _validate_actor(actor)
        _validate_signer_key(signer_key)
    return entries


def _validated_replay_snapshot(snapshot: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    entries = _validate_snapshot_entries(snapshot, "replay ledger snapshot")
    if len(entries) > MAX_CHANNEL_INPUT_REPLAY_SLOTS:
        raise ChannelInputValidationError("channel input replay ledger exceeds slot limit")
    for slot, payload_hash in entries:
        _validate_slot(slot)
        if (
            not isinstance(payload_hash, str)
            or len(payload_hash) != 64
            or payload_hash.lower() != payload_hash
        ):
            raise ChannelInputValidationError(
                "replay payload hash must be lowercase SHA256 hexadecimal"
            )
        try:
            bytes.fromhex(payload_hash)
        except ValueError as exc:
            raise ChannelInputValidationError(
                "replay payload hash must be lowercase SHA256 hexadecimal"
            ) from exc
    return entries


def _validate_snapshot_entries(
    snapshot: Sequence[tuple[str, str]], snapshot_name: str
) -> tuple[tuple[str, str], ...]:
    if not isinstance(snapshot, Sequence) or isinstance(snapshot, str | bytes):
        raise ChannelInputValidationError(f"{snapshot_name} must be a finite sequence")
    entries = tuple(snapshot)
    if any(not isinstance(entry, tuple) or len(entry) != 2 for entry in entries):
        raise ChannelInputValidationError(f"{snapshot_name} entries must be actor/key pairs")
    typed_entries = tuple((entry[0], entry[1]) for entry in entries)
    if any(
        not isinstance(first, str) or not isinstance(second, str) for first, second in typed_entries
    ):
        raise ChannelInputValidationError(f"{snapshot_name} entries must contain strings")
    if len(set(typed_entries)) != len(typed_entries) or typed_entries != tuple(
        sorted(typed_entries)
    ):
        raise ChannelInputValidationError(f"{snapshot_name} must be unique and sorted")
    return typed_entries


def _validate_slot(slot: str) -> None:
    if not isinstance(slot, str) or not slot:
        raise ChannelInputValidationError("replay slot must be a non-empty ASCII string")
    try:
        encoded = slot.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ChannelInputValidationError("replay slot must be a non-empty ASCII string") from exc
    if len(encoded) > MAX_CHANNEL_INPUT_REPLAY_SLOT_BYTES or any(
        byte < 0x21 or byte > 0x7E for byte in encoded
    ):
        raise ChannelInputValidationError("replay slot must be bounded printable ASCII")
