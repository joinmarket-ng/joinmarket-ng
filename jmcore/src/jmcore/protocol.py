"""
JoinMarket protocol definitions, message types, and serialization.

Feature Flag System
===================
This implementation uses feature flags for capability negotiation instead of
protocol version bumping. This allows incremental feature adoption while
maintaining full compatibility with the reference implementation from joinmarket-clientserver.

Features are advertised in the handshake `features` dict and negotiated
per-CoinJoin session via extended !fill/!pubkey messages.

Available Features:
- neutrino_compat: Extended UTXO metadata (scriptpubkey, blockheight) for
  light client verification. Required for Neutrino backend takers.
- push_encrypted: Encrypted !push command with session binding. Prevents
  abuse of makers as unauthenticated broadcast bots.

Feature Dependencies:
- neutrino_compat: No dependencies
- push_encrypted: Requires active NaCl encryption session (implicit)

Nick Format:
============
JoinMarket nicks encode the protocol version: J{version}{hash}
All nicks use version 5 for maximum compatibility with reference implementation.
Feature detection happens via handshake and !fill/!pubkey exchange, not nick.

Cross-Implementation Compatibility:
===================================
**Our Implementation ↔ Reference (JAM):**
- We use J5 nicks and proto-ver=5 in handshake
- Features field is ignored by reference implementation
- Legacy UTXO format used unless both peers advertise neutrino_compat
- Graceful fallback to v5 behavior for all features

**Feature Negotiation During CoinJoin:**
- Taker advertises features in !fill (optional JSON suffix)
- Maker responds with features in !pubkey (optional JSON suffix)
- Extended formats used only when both peers support the feature

**Peerlist Feature Extension:**
Our directory server extends the peerlist format to include features:
- Legacy format: nick;location (or nick;location;D for disconnected)
- Extended format: nick;location;F:feature1+feature2 (features as plus-separated list)
The extended format is backward compatible - legacy clients will ignore the F: suffix.
Note: Plus separator is used because the peerlist itself uses commas to separate entries.
"""

from __future__ import annotations

import binascii
import json
import re
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field
from pydantic.dataclasses import dataclass

# Protocol version: v5 for full reference implementation compatibility
# Features are negotiated separately via the features dict
JM_VERSION = 5
# JM_VERSION_MIN is kept as an alias for backward compatibility.
# Since we only support v5, min == max.
JM_VERSION_MIN = JM_VERSION

COMMAND_PREFIX = "!"
NICK_PEERLOCATOR_SEPARATOR = ";"
ONION_VIRTUAL_PORT = 5222
NOT_SERVING_ONION_HOSTNAME = "NOT-SERVING-ONION"
NICK_HASH_LENGTH = 10
NICK_MAX_ENCODED = 14

# Feature flag constants
FEATURE_NEUTRINO_COMPAT = "neutrino_compat"
FEATURE_PUSH_ENCRYPTED = "push_encrypted"
FEATURE_PEERLIST_FEATURES = "peerlist_features"  # Supports extended peerlist with F: suffix

# Feature dependencies: feature -> list of required features
FEATURE_DEPENDENCIES: dict[str, list[str]] = {
    FEATURE_NEUTRINO_COMPAT: [],
    FEATURE_PUSH_ENCRYPTED: [],  # Requires NaCl session, but that's implicit
    FEATURE_PEERLIST_FEATURES: [],  # No dependencies
}

# All known features
ALL_FEATURES = {FEATURE_NEUTRINO_COMPAT, FEATURE_PUSH_ENCRYPTED, FEATURE_PEERLIST_FEATURES}


@dataclass
class FeatureSet:
    """
    Represents a set of protocol features advertised by a peer.

    Used for feature negotiation during handshake and CoinJoin sessions.
    """

    features: set[str] = Field(default_factory=set)

    @classmethod
    def from_handshake(cls, handshake_data: dict[str, Any]) -> FeatureSet:
        """Extract features from a handshake payload."""
        features_dict = handshake_data.get("features", {})
        # Only include features that are set to True
        features = {k for k, v in features_dict.items() if v is True}
        return cls(features=features)

    @classmethod
    def from_list(cls, feature_list: list[str]) -> FeatureSet:
        """Create from a list of feature names."""
        return cls(features=set(feature_list))

    @classmethod
    def from_comma_string(cls, s: str) -> FeatureSet:
        """Parse from plus-separated string (e.g., 'neutrino_compat+push_encrypted').

        Note: Despite the method name, uses '+' as separator because the peerlist
        itself uses ',' to separate entries. The name is kept for backward compatibility.
        Also accepts ',' for legacy/handshake use cases.
        """
        if not s or not s.strip():
            return cls(features=set())
        # Support both + (peerlist) and , (legacy/handshake) separators
        if "+" in s:
            return cls(features={f.strip() for f in s.split("+") if f.strip()})
        return cls(features={f.strip() for f in s.split(",") if f.strip()})

    def to_dict(self) -> dict[str, bool]:
        """Convert to dict for JSON serialization."""
        return dict.fromkeys(sorted(self.features), True)

    def to_comma_string(self) -> str:
        """Convert to plus-separated string for peerlist F: suffix.

        Note: Uses '+' as separator instead of ',' because the peerlist
        itself uses ',' to separate entries. Using ',' for features would
        cause parsing ambiguity.
        """
        return "+".join(sorted(self.features))

    def supports(self, feature: str) -> bool:
        """Check if this set includes a specific feature."""
        return feature in self.features

    def supports_neutrino_compat(self) -> bool:
        """Check if neutrino_compat is supported."""
        return FEATURE_NEUTRINO_COMPAT in self.features

    def supports_push_encrypted(self) -> bool:
        """Check if push_encrypted is supported."""
        return FEATURE_PUSH_ENCRYPTED in self.features

    def supports_peerlist_features(self) -> bool:
        """Check if peer supports extended peerlist with features (F: suffix)."""
        return FEATURE_PEERLIST_FEATURES in self.features

    def validate_dependencies(self) -> tuple[bool, str]:
        """Check that all feature dependencies are satisfied."""
        for feature in self.features:
            deps = FEATURE_DEPENDENCIES.get(feature, [])
            for dep in deps:
                if dep not in self.features:
                    return False, f"Feature '{feature}' requires '{dep}'"
        return True, ""

    def intersection(self, other: FeatureSet) -> FeatureSet:
        """Return features supported by both sets."""
        return FeatureSet(features=self.features & other.features)

    def __bool__(self) -> bool:
        """True if any features are set."""
        return bool(self.features)

    def __contains__(self, feature: str) -> bool:
        return feature in self.features

    def __iter__(self):
        return iter(self.features)

    def __len__(self) -> int:
        return len(self.features)


@dataclass
class RequiredFeatures:
    """
    Features that this peer requires from counterparties.

    Used to filter incompatible peers during maker selection.
    """

    required: set[str] = Field(default_factory=set)

    @classmethod
    def for_neutrino_taker(cls) -> RequiredFeatures:
        """Create requirements for a taker using Neutrino backend."""
        return cls(required={FEATURE_NEUTRINO_COMPAT})

    @classmethod
    def none(cls) -> RequiredFeatures:
        """No required features."""
        return cls(required=set())

    def is_compatible(self, peer_features: FeatureSet) -> tuple[bool, str]:
        """Check if peer supports all required features."""
        missing = self.required - peer_features.features
        if missing:
            return False, f"Missing required features: {missing}"
        return True, ""

    def __bool__(self) -> bool:
        return bool(self.required)


def get_nick_version(nick: str) -> int:
    """
    Extract protocol version from a JoinMarket nick.

    Nick format: J{version}{hash} where version is a single digit.
    Example: J5abc123... (v5)

    Returns JM_VERSION (5) if version cannot be determined.
    """
    if nick and len(nick) >= 2 and nick[0] == "J" and nick[1].isdigit():
        return int(nick[1])
    return JM_VERSION


@dataclass
class UTXOMetadata:
    """
    Extended UTXO metadata for Neutrino-compatible verification.

    This allows light clients to verify UTXOs without arbitrary blockchain queries
    by providing the scriptPubKey (for Neutrino watch list) and block height
    (for efficient rescan starting point).
    """

    txid: str
    vout: int
    scriptpubkey: str | None = None  # Hex-encoded scriptPubKey
    blockheight: int | None = None  # Block height where UTXO was confirmed

    def __post_init__(self) -> None:
        """Strict validation of UTXO fields to match legacy protocol."""
        # TXID validation
        if len(self.txid) != 64:
            raise ValueError(f"Invalid TXID length: {len(self.txid)} (expected 64)")
        try:
            binascii.unhexlify(self.txid)
        except (binascii.Error, TypeError) as exc:
            raise ValueError(f"Invalid TXID hex: {self.txid}") from exc

        # Vout validation (Bitcoin uint32)
        if self.vout < 0:
            raise ValueError(f"Invalid vout (must be non-negative): {self.vout}")
        if self.vout > 0xFFFFFFFF:
            raise ValueError(f"Invalid vout (overflow, max 4294967295): {self.vout}")

        # Optional blockheight validation
        if self.blockheight is not None and self.blockheight < 0:
            raise ValueError(f"Invalid blockheight (must be non-negative): {self.blockheight}")

    def to_legacy_str(self) -> str:
        """Format as legacy string: txid:vout"""
        return f"{self.txid}:{self.vout}"

    def to_extended_str(self) -> str:
        """Format as extended string: txid:vout:scriptpubkey:blockheight"""
        if self.scriptpubkey is None or self.blockheight is None:
            return self.to_legacy_str()
        return f"{self.txid}:{self.vout}:{self.scriptpubkey}:{self.blockheight}"

    @classmethod
    def from_str(cls, s: str) -> UTXOMetadata:
        """
        Parse UTXO string in either legacy or extended format.

        Legacy format: txid:vout
        Extended format: txid:vout:scriptpubkey:blockheight
        """
        parts = s.split(":")
        if len(parts) not in [2, 4]:
            raise ValueError(f"Invalid UTXO format: {s}")

        txid = parts[0]
        try:
            vout = int(parts[1])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid vout (not an integer): {parts[1]}") from exc

        if len(parts) == 2:
            return cls(txid=txid, vout=vout)

        # Extended format
        scriptpubkey = parts[2]
        try:
            blockheight = int(parts[3])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid blockheight (not an integer): {parts[3]}") from exc

        return cls(
            txid=txid,
            vout=vout,
            scriptpubkey=scriptpubkey,
            blockheight=blockheight,
        )

    def has_neutrino_metadata(self) -> bool:
        """Check if this UTXO has the metadata needed for Neutrino verification."""
        return self.scriptpubkey is not None and self.blockheight is not None

    @staticmethod
    def is_valid_scriptpubkey(scriptpubkey: str) -> bool:
        """Validate scriptPubKey format (hex string)."""
        if not scriptpubkey:
            return False
        # Must be valid hex
        if not re.match(r"^[0-9a-fA-F]+$", scriptpubkey):
            return False
        # Common scriptPubKey lengths (in hex chars):
        # P2PKH: 50 (25 bytes), P2SH: 46 (23 bytes)
        # P2WPKH: 44 (22 bytes), P2WSH: 68 (34 bytes)
        # P2TR: 68 (34 bytes)
        return not (len(scriptpubkey) < 4 or len(scriptpubkey) > 200)


def parse_utxo_list(utxo_list_str: str, require_metadata: bool = False) -> list[UTXOMetadata]:
    """
    Parse a comma-separated list of UTXOs.

    Args:
        utxo_list_str: Comma-separated UTXOs (legacy or extended format)
        require_metadata: If True, raise error if any UTXO lacks Neutrino metadata

    Returns:
        List of UTXOMetadata objects
    """
    if not utxo_list_str:
        return []

    utxos = []
    for utxo_str in utxo_list_str.split(","):
        utxo = UTXOMetadata.from_str(utxo_str.strip())
        if require_metadata and not utxo.has_neutrino_metadata():
            raise ValueError(f"UTXO {utxo.to_legacy_str()} missing Neutrino metadata")
        utxos.append(utxo)
    return utxos


def format_utxo_list(utxos: list[UTXOMetadata], extended: bool = False) -> str:
    """
    Format a list of UTXOs as comma-separated string.

    Args:
        utxos: List of UTXOMetadata objects
        extended: If True, use extended format with scriptpubkey:blockheight

    Returns:
        Comma-separated UTXO string
    """
    if extended:
        return ",".join(u.to_extended_str() for u in utxos)
    else:
        return ",".join(u.to_legacy_str() for u in utxos)


class MessageType(IntEnum):
    PRIVMSG = 685
    PUBMSG = 687
    PEERLIST = 789
    GETPEERLIST = 791
    HANDSHAKE = 793
    DN_HANDSHAKE = 795
    PING = 797
    PONG = 799
    DISCONNECT = 801

    CONNECT = 785
    CONNECT_IN = 797


class ProtocolMessage(BaseModel):
    type: MessageType
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps({"type": self.type.value, "data": self.payload})

    @classmethod
    def from_json(cls, data: str) -> ProtocolMessage:
        obj = json.loads(data)
        return cls(type=MessageType(obj["type"]), payload=obj["data"])

    def to_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> ProtocolMessage:
        return cls.from_json(data.decode("utf-8"))


def create_handshake_request(
    nick: str,
    location: str,
    network: str,
    directory: bool = False,
    neutrino_compat: bool = False,
    features: FeatureSet | None = None,
) -> dict[str, Any]:
    """
    Create a handshake request message.

    Args:
        nick: Bot nickname
        location: Onion address or NOT-SERVING-ONION
        network: Bitcoin network (mainnet, testnet, signet, regtest)
        directory: True if this is a directory server
        neutrino_compat: True to advertise Neutrino-compatible UTXO metadata support
        features: FeatureSet to advertise (overrides neutrino_compat if provided)

    Returns:
        Handshake request payload dict
    """
    if features is not None:
        features_dict = features.to_dict()
    else:
        features_dict = {}
        if neutrino_compat:
            features_dict[FEATURE_NEUTRINO_COMPAT] = True

    return {
        "app-name": "joinmarket",
        "directory": directory,
        "location-string": location,
        "proto-ver": JM_VERSION,
        "features": features_dict,
        "nick": nick,
        "network": network,
    }


def create_handshake_response(
    nick: str,
    network: str,
    accepted: bool = True,
    motd: str = "JoinMarket Directory Server",
    neutrino_compat: bool = False,
    features: FeatureSet | None = None,
) -> dict[str, Any]:
    """
    Create a handshake response message.

    Args:
        nick: Directory server nickname
        network: Bitcoin network
        accepted: Whether the connection is accepted
        motd: Message of the day
        neutrino_compat: True to advertise Neutrino-compatible UTXO metadata support
        features: FeatureSet to advertise (overrides neutrino_compat if provided)

    Returns:
        Handshake response payload dict
    """
    if features is not None:
        features_dict = features.to_dict()
    else:
        features_dict = {}
        if neutrino_compat:
            features_dict[FEATURE_NEUTRINO_COMPAT] = True

    return {
        "app-name": "joinmarket",
        "directory": True,
        "proto-ver-min": JM_VERSION,
        "proto-ver-max": JM_VERSION,
        "features": features_dict,
        "accepted": accepted,
        "nick": nick,
        "network": network,
        "motd": motd,
    }


def peer_supports_neutrino_compat(handshake_data: dict[str, Any]) -> bool:
    """
    Check if a peer supports Neutrino-compatible UTXO metadata.

    Args:
        handshake_data: Handshake payload from peer

    Returns:
        True if peer advertises neutrino_compat feature
    """
    features = handshake_data.get("features", {})
    return features.get(FEATURE_NEUTRINO_COMPAT, False)


def parse_peer_location(location: str) -> tuple[str, int]:
    if location == NOT_SERVING_ONION_HOSTNAME:
        return (location, -1)
    try:
        host, port_str = location.split(":")
        port = int(port_str)
        if port <= 0 or port > 65535:
            raise ValueError(f"Invalid port: {port}")
        return (host, port)
    except (ValueError, AttributeError) as e:
        raise ValueError(f"Invalid location string: {location}") from e


def create_peerlist_entry(
    nick: str,
    location: str,
    disconnected: bool = False,
    features: FeatureSet | None = None,
) -> str:
    """
    Create a peerlist entry string.

    Format:
    - Legacy: nick;location or nick;location;D
    - Extended: nick;location;F:feature1,feature2 or nick;location;D;F:feature1,feature2

    The F: prefix is used to identify the features field and maintain backward compatibility.
    """
    entry = f"{nick}{NICK_PEERLOCATOR_SEPARATOR}{location}"
    if disconnected:
        entry += f"{NICK_PEERLOCATOR_SEPARATOR}D"
    if features and features.features:
        entry += f"{NICK_PEERLOCATOR_SEPARATOR}F:{features.to_comma_string()}"
    return entry


def parse_peerlist_entry(entry: str) -> tuple[str, str, bool, FeatureSet]:
    """
    Parse a peerlist entry string.

    Returns:
        Tuple of (nick, location, disconnected, features)
    """
    parts = entry.split(NICK_PEERLOCATOR_SEPARATOR)
    if len(parts) < 2:
        raise ValueError(f"Invalid peerlist entry: {entry}")

    nick = parts[0]
    location = parts[1]
    disconnected = False
    features = FeatureSet()

    # Parse remaining parts
    for part in parts[2:]:
        if part == "D":
            disconnected = True
        elif part.startswith("F:"):
            features = FeatureSet.from_comma_string(part[2:])

    return (nick, location, disconnected, features)


def format_jm_message(from_nick: str, to_nick: str, cmd: str, message: str) -> str:
    return f"{from_nick}{COMMAND_PREFIX}{to_nick}{COMMAND_PREFIX}{cmd} {message}"


def parse_jm_message(msg: str) -> tuple[str, str, str] | None:
    try:
        parts = msg.split(COMMAND_PREFIX)
        if len(parts) < 3:
            return None
        from_nick = parts[0]
        to_nick = parts[1]
        rest = COMMAND_PREFIX.join(parts[2:])
        return (from_nick, to_nick, rest)
    except Exception:
        return None
