"""
Central registry for JoinMarket on-wire CoinJoin commands.

Historically the on-wire commands (``!fill``, ``!auth``, ``!ioauth`` ...) were
scattered as string literals across the maker dispatcher, the taker phase
code, and a handful of allow-lists in the directory server / orderbook
watcher. JMP-0005 (ZKP credentials) and JMP-0006 (multi-round tx-extension)
add eleven more commands, so this module introduces a single source of
truth:

- :class:`Command` enumerates every valid on-wire command (legacy + ZKP).
- :data:`COMMAND_SPECS` maps each command to a :class:`CommandSpec` that
  describes direction, encryption, broadcast scope, and the feature flag
  that gates it.

The wire prefix (``!``) is **not** part of the enum value: ``Command.FILL``
is ``"fill"``. Use :func:`with_prefix` / :func:`strip_prefix` when
interoperating with the wire format. This avoids the historical
inconsistency where some sites used ``"!fill"`` and others ``"fill"``.

This module only declares names and metadata; dispatch and serialization
remain the responsibility of the maker (``protocol_handlers``), taker
(``taker``), and directory server. New commands therefore touch only this
module + the corresponding dispatcher branch, instead of multiple
allow-lists.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict


class Command(StrEnum):
    """All valid CoinJoin on-wire commands (without the ``!`` prefix).

    Values are intentionally lowercase to match the historical wire format.
    """

    # -- Legacy CoinJoin (always available) --
    FILL = "fill"
    AUTH = "auth"
    PUBKEY = "pubkey"
    IOAUTH = "ioauth"
    TX = "tx"
    SIG = "sig"
    PUSH = "push"
    HP2 = "hp2"
    ORDERBOOK = "orderbook"

    # -- JMP-0005 ZKP credentials --
    ZKPPARAMS = "zkpparams"
    ZKPISSUE_IN = "zkpissue_in"
    ZKPCRED_IN = "zkpcred_in"
    ZKPREQ = "zkpreq"
    ZKPCRED = "zkpcred"
    ZKPREG = "zkpreg"

    # -- JMP-0006 multi-round tx-extension --
    CJEXT = "cjext"
    TXEXT = "txext"
    SIGEXT = "sigext"
    TXFREEZE = "txfreeze"
    SIGFINAL = "sigfinal"


class Direction(StrEnum):
    """Who originates a command on the wire."""

    TAKER_TO_MAKER = "taker_to_maker"
    MAKER_TO_TAKER = "maker_to_taker"
    PUBLIC = "public"  # broadcast (orderbook, hp2 commitment broadcast)


class FeatureGate(StrEnum):
    """Which feature flag must be enabled for a command to be accepted."""

    ALWAYS = "always"
    ZKP = "zkp"
    TX_EXTENSION = "tx_extension"


class CommandSpec(BaseModel):
    """Static metadata for one wire command.

    Attributes:
        direction: Who originates this command on the wire.
        encrypted: True if the payload is end-to-end encrypted via the
            session NaCl box. ``pubkey`` and the public broadcasts
            (``orderbook``, ``hp2`` pubmsg) are always plaintext.
        broadcast: True for commands that may be sent as PUBMSG. Most
            commands are PRIVMSG only.
        feature: Feature flag gating dispatch. Makers must drop incoming
            commands whose feature is disabled in their config.
    """

    model_config = ConfigDict(frozen=True)

    direction: Direction
    encrypted: bool = False
    broadcast: bool = False
    feature: FeatureGate = FeatureGate.ALWAYS


COMMAND_SPECS: Final[dict[Command, CommandSpec]] = {
    # Legacy
    Command.FILL: CommandSpec(direction=Direction.TAKER_TO_MAKER),
    Command.AUTH: CommandSpec(direction=Direction.TAKER_TO_MAKER, encrypted=True),
    Command.PUBKEY: CommandSpec(direction=Direction.MAKER_TO_TAKER),
    Command.IOAUTH: CommandSpec(direction=Direction.MAKER_TO_TAKER, encrypted=True),
    Command.TX: CommandSpec(direction=Direction.TAKER_TO_MAKER, encrypted=True),
    Command.SIG: CommandSpec(direction=Direction.MAKER_TO_TAKER, encrypted=True),
    Command.PUSH: CommandSpec(direction=Direction.TAKER_TO_MAKER, encrypted=True),
    # hp2 has both privmsg (commitment transfer request) and pubmsg
    # (commitment broadcast for blacklisting). We mark broadcast=True
    # because the maker re-broadcasts publicly to obfuscate the source.
    Command.HP2: CommandSpec(direction=Direction.PUBLIC, broadcast=True),
    Command.ORDERBOOK: CommandSpec(direction=Direction.PUBLIC, broadcast=True),
    # JMP-0005 ZKP credentials. All taker<->maker, encrypted, and
    # gated on the ``zkp`` feature flag.
    Command.ZKPPARAMS: CommandSpec(
        direction=Direction.MAKER_TO_TAKER, encrypted=True, feature=FeatureGate.ZKP
    ),
    Command.ZKPISSUE_IN: CommandSpec(
        direction=Direction.MAKER_TO_TAKER, encrypted=True, feature=FeatureGate.ZKP
    ),
    Command.ZKPCRED_IN: CommandSpec(
        direction=Direction.MAKER_TO_TAKER, encrypted=True, feature=FeatureGate.ZKP
    ),
    Command.ZKPREQ: CommandSpec(
        direction=Direction.TAKER_TO_MAKER, encrypted=True, feature=FeatureGate.ZKP
    ),
    Command.ZKPCRED: CommandSpec(
        direction=Direction.MAKER_TO_TAKER, encrypted=True, feature=FeatureGate.ZKP
    ),
    # zkpreg is the bond attestation registration broadcast (PRIVMSG-to-onion
    # primary, PUBMSG fallback per spec). PUBMSG variant is handled via
    # broadcast=True; the body remains encrypted to nobody (it's plaintext
    # because the attestation itself is the cryptographic proof).
    Command.ZKPREG: CommandSpec(
        direction=Direction.PUBLIC, broadcast=True, feature=FeatureGate.ZKP
    ),
    # JMP-0006 tx-extension. All taker<->maker, encrypted, gated on
    # ``tx_extension``.
    Command.CJEXT: CommandSpec(
        direction=Direction.TAKER_TO_MAKER, encrypted=True, feature=FeatureGate.TX_EXTENSION
    ),
    Command.TXEXT: CommandSpec(
        direction=Direction.TAKER_TO_MAKER, encrypted=True, feature=FeatureGate.TX_EXTENSION
    ),
    Command.SIGEXT: CommandSpec(
        direction=Direction.MAKER_TO_TAKER, encrypted=True, feature=FeatureGate.TX_EXTENSION
    ),
    Command.TXFREEZE: CommandSpec(
        direction=Direction.TAKER_TO_MAKER, encrypted=True, feature=FeatureGate.TX_EXTENSION
    ),
    Command.SIGFINAL: CommandSpec(
        direction=Direction.MAKER_TO_TAKER, encrypted=True, feature=FeatureGate.TX_EXTENSION
    ),
}


# Subsets handy for dispatchers and allow-lists.
ZKP_COMMANDS: Final[frozenset[Command]] = frozenset(
    cmd for cmd, spec in COMMAND_SPECS.items() if spec.feature is FeatureGate.ZKP
)
TX_EXTENSION_COMMANDS: Final[frozenset[Command]] = frozenset(
    cmd for cmd, spec in COMMAND_SPECS.items() if spec.feature is FeatureGate.TX_EXTENSION
)
LEGACY_COMMANDS: Final[frozenset[Command]] = frozenset(
    cmd for cmd, spec in COMMAND_SPECS.items() if spec.feature is FeatureGate.ALWAYS
)


def with_prefix(command: Command | str) -> str:
    """Return the wire form (``!cmd``) of a command name.

    Accepts either a :class:`Command` member or a plain string. Strings
    that already start with ``!`` are returned unchanged.
    """
    s = str(command)
    return s if s.startswith("!") else f"!{s}"


def strip_prefix(wire: str) -> str:
    """Return the bare command name (without ``!`` prefix)."""
    return wire.lstrip("!")


def parse_command(wire: str) -> Command | None:
    """Map a wire token (with or without ``!``) to a :class:`Command`.

    Returns ``None`` if the token is not a recognized command. Callers
    are responsible for additional validation (e.g. payload structure).
    """
    bare = strip_prefix(wire).split(" ", 1)[0]
    try:
        return Command(bare)
    except ValueError:
        return None


def is_feature_enabled(command: Command, *, zkp_enabled: bool, tx_extension_enabled: bool) -> bool:
    """Check whether ``command`` is currently allowed by feature flags.

    Always-on commands return True regardless of flag state. ZKP commands
    require ``zkp_enabled``. Tx-extension commands additionally require
    ``zkp_enabled`` because the spec embeds them in the credential flow.
    """
    feature = COMMAND_SPECS[command].feature
    if feature is FeatureGate.ALWAYS:
        return True
    if feature is FeatureGate.ZKP:
        return zkp_enabled
    if feature is FeatureGate.TX_EXTENSION:
        return zkp_enabled and tx_extension_enabled
    return False


__all__ = [
    "COMMAND_SPECS",
    "Command",
    "CommandSpec",
    "Direction",
    "FeatureGate",
    "LEGACY_COMMANDS",
    "TX_EXTENSION_COMMANDS",
    "ZKP_COMMANDS",
    "is_feature_enabled",
    "parse_command",
    "strip_prefix",
    "with_prefix",
]
