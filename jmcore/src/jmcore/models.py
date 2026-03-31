"""
Core data models using Pydantic for validation and serialization.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from functools import cached_property
from typing import Any

from pydantic import BaseModel, Field, field_validator

from jmcore.bitcoin import calculate_relative_fee


class MessageParsingError(Exception):
    """Exception raised when message parsing fails due to security limits."""

    pass


def validate_json_nesting_depth(obj: Any, max_depth: int = 10, current_depth: int = 0) -> None:
    """
    Validate that a JSON object does not exceed maximum nesting depth.

    Args:
        obj: The object to validate (dict, list, or primitive)
        max_depth: Maximum allowed nesting depth
        current_depth: Current depth in recursion

    Raises:
        MessageParsingError: If nesting depth exceeds max_depth
    """
    if current_depth > max_depth:
        raise MessageParsingError(f"JSON nesting depth exceeds maximum of {max_depth}")

    if isinstance(obj, dict):
        for value in obj.values():
            validate_json_nesting_depth(value, max_depth, current_depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            validate_json_nesting_depth(item, max_depth, current_depth + 1)


# Default directory servers for each network
# Mainnet nodes verified as working (from https://joinmarket-ng.sgn.space/orderbook.json)
DIRECTORY_NODES_MAINNET: list[str] = [
    "satoshi2vcg5e2ept7tjkzlkpomkobqmgtsjzegg6wipnoajadissead.onion:5222",
    "coinjointovy3eq5fjygdwpkbcdx63d7vd4g32mw7y553uj3kjjzkiqd.onion:5222",
    "nakamotourflxwjnjpnrk7yc2nhkf6r62ed4gdfxmmn5f4saw5q5qoyd.onion:5222",
    "odpwaf67rs5226uabcamvypg3y4bngzmfk7255flcdodesqhsvkptaid.onion:5222",
    "jmarketxf5wc4aldf3slm5u6726zsky52bqnfv6qyxe5hnafgly6yuyd.onion:5222",
]

# Signet default directory nodes
DIRECTORY_NODES_SIGNET: list[str] = [
    "signetvaxgd3ivj4tml4g6ed3samaa2rscre2gyeyohncmwk4fbesiqd.onion:5222",
]
# No default directory nodes for testnet/regtest - must be configured by user
DIRECTORY_NODES_TESTNET: list[str] = []


def get_default_directory_nodes(network: NetworkType) -> list[str]:
    """Get default directory nodes for a given network."""
    if network == NetworkType.MAINNET:
        return DIRECTORY_NODES_MAINNET.copy()
    elif network == NetworkType.SIGNET:
        return DIRECTORY_NODES_SIGNET.copy()
    elif network == NetworkType.TESTNET:
        return DIRECTORY_NODES_TESTNET.copy()
    # Regtest has no default directory nodes - must be configured
    return []


class PeerStatus(StrEnum):
    UNCONNECTED = "unconnected"
    CONNECTED = "connected"
    HANDSHAKED = "handshaked"
    DISCONNECTED = "disconnected"


class NetworkType(StrEnum):
    MAINNET = "mainnet"
    TESTNET = "testnet"
    SIGNET = "signet"
    REGTEST = "regtest"


class PeerInfo(BaseModel):
    nick: str = Field(..., min_length=1, max_length=64)
    onion_address: str = Field(..., pattern=r"^[a-z2-7]{56}\.onion$|^NOT-SERVING-ONION$")
    port: int = Field(..., ge=-1, le=65535)
    status: PeerStatus = PeerStatus.UNCONNECTED
    is_directory: bool = False
    network: NetworkType = NetworkType.MAINNET
    last_seen: datetime | None = None
    features: dict[str, Any] = Field(default_factory=dict)
    protocol_version: int = Field(default=5, ge=5, le=10)  # Negotiated protocol version
    neutrino_compat: bool = False  # True if peer supports extended UTXO metadata

    @field_validator("onion_address")
    @classmethod
    def validate_onion(cls, v: str) -> str:
        if v == "NOT-SERVING-ONION":
            return v
        if not v.endswith(".onion"):
            raise ValueError("Invalid onion address")
        return v

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int, info) -> int:
        if v == -1 and info.data.get("onion_address") == "NOT-SERVING-ONION":
            return v
        if v < 1 or v > 65535:
            raise ValueError("Port must be between 1 and 65535")
        return v

    @cached_property
    def location_string(self) -> str:
        if self.onion_address == "NOT-SERVING-ONION":
            return "NOT-SERVING-ONION"
        return f"{self.onion_address}:{self.port}"

    def supports_extended_utxo(self) -> bool:
        """Check if this peer supports extended UTXO format (neutrino_compat)."""
        # With feature-based detection, we check the neutrino_compat flag
        # which is set from the features dict during handshake
        return self.neutrino_compat

    model_config = {"frozen": False}


class MessageEnvelope(BaseModel):
    message_type: int = Field(..., ge=0)
    payload: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_bytes(self) -> bytes:
        import json

        result = json.dumps({"type": self.message_type, "line": self.payload}).encode("utf-8")
        return result

    @classmethod
    def from_bytes(
        cls, data: bytes, max_line_length: int = 65536, max_json_nesting_depth: int = 10
    ) -> MessageEnvelope:
        """
        Parse a message envelope from bytes with security limits.

        Args:
            data: Raw message bytes (without \\r\\n terminator)
            max_line_length: Maximum allowed line length in bytes (default 64KB)
            max_json_nesting_depth: Maximum JSON nesting depth (default 10)

        Returns:
            Parsed MessageEnvelope

        Raises:
            MessageParsingError: If message exceeds security limits
            json.JSONDecodeError: If JSON is malformed
        """
        import json

        # Check line length BEFORE parsing to prevent DoS
        if len(data) > max_line_length:
            raise MessageParsingError(
                f"Message line length {len(data)} exceeds maximum of {max_line_length} bytes"
            )

        # Parse JSON
        obj = json.loads(data)

        # Validate nesting depth BEFORE creating model
        validate_json_nesting_depth(obj, max_json_nesting_depth)

        return cls(message_type=obj["type"], payload=obj["line"])


class HandshakeRequest(BaseModel):
    app_name: str = "JoinMarket"
    directory: bool = False
    location_string: str
    proto_ver: int
    features: dict[str, Any] = Field(default_factory=dict)
    nick: str = Field(..., min_length=1)
    network: NetworkType


class HandshakeResponse(BaseModel):
    app_name: str = "JoinMarket"
    directory: bool = True
    proto_ver_min: int
    proto_ver_max: int
    features: dict[str, Any] = Field(default_factory=dict)
    accepted: bool
    nick: str = Field(..., min_length=1)
    network: NetworkType
    motd: str = "JoinMarket Directory Server"


class OfferType(StrEnum):
    SW0_ABSOLUTE = "sw0absoffer"
    SW0_RELATIVE = "sw0reloffer"
    SWA_ABSOLUTE = "swabsoffer"
    SWA_RELATIVE = "swreloffer"
    TR0_ABSOLUTE = "tr0absoffer"
    TR0_RELATIVE = "tr0reloffer"
    TRA_ABSOLUTE = "trabsoffer"
    TRA_RELATIVE = "trreloffer"


def is_absolute_offer_type(offer_type: OfferType) -> bool:
    """Check if an offer type uses absolute fees."""
    return offer_type in (
        OfferType.SW0_ABSOLUTE,
        OfferType.SWA_ABSOLUTE,
        OfferType.TR0_ABSOLUTE,
        OfferType.TRA_ABSOLUTE,
    )


def is_taproot_offer_type(offer_type: OfferType) -> bool:
    """Check if an offer type is for Taproot addresses."""
    return offer_type in (
        OfferType.TR0_ABSOLUTE,
        OfferType.TR0_RELATIVE,
        OfferType.TRA_ABSOLUTE,
        OfferType.TRA_RELATIVE,
    )


def get_default_offer_types(address_type: str) -> set[OfferType]:
    """Get the default offer types for a given address type."""
    if address_type == "p2tr":
        return {OfferType.TR0_RELATIVE, OfferType.TR0_ABSOLUTE}
    return {OfferType.SW0_RELATIVE, OfferType.SW0_ABSOLUTE}


def calculate_cj_fee(offer_type: OfferType, cjfee: str | int, amount: int) -> int:
    """
    Calculate actual CoinJoin fee based on offer type.

    This is the canonical fee calculation used by both makers and takers.

    Args:
        offer_type: Absolute or relative offer type
        cjfee: Fee value (int for absolute, string decimal for relative)
        amount: CoinJoin amount in satoshis

    Returns:
        Actual fee in satoshis
    """
    if is_absolute_offer_type(offer_type):
        return int(cjfee)
    else:
        return calculate_relative_fee(amount, str(cjfee))


class Offer(BaseModel):
    counterparty: str = Field(..., min_length=1)
    oid: int = Field(..., ge=0)
    ordertype: OfferType
    minsize: int = Field(..., ge=0)
    maxsize: int = Field(..., ge=0)
    txfee: int = Field(..., ge=0)
    cjfee: str | int
    fidelity_bond_value: int = Field(default=0, ge=0)
    directory_node: str | None = None
    directory_nodes: list[str] = Field(
        default_factory=list,
        description="All directory nodes that announced this offer (for statistics)",
    )
    fidelity_bond_data: dict[str, Any] | None = None
    neutrino_compat: bool = Field(
        default=False,
        description="Maker requires extended UTXO format (neutrino-compatible backend)",
    )
    features: dict[str, bool] = Field(
        default_factory=dict,
        description="Features supported by this maker (from handshake)",
    )
    directly_reachable: bool | None = Field(
        default=None,
        description="Whether maker is directly reachable via their onion address (None = not checked)",
    )

    @field_validator("cjfee")
    @classmethod
    def validate_cjfee(cls, v: str | int, info) -> str | int:
        ordertype = info.data.get("ordertype")
        if is_absolute_offer_type(ordertype):
            return int(v)
        return str(v)

    def is_absolute_fee(self) -> bool:
        return is_absolute_offer_type(self.ordertype)

    def calculate_fee(self, amount: int) -> int:
        return calculate_cj_fee(self.ordertype, self.cjfee, amount)


class FidelityBond(BaseModel):
    counterparty: str
    utxo_txid: str = Field(..., pattern=r"^[0-9a-fA-F]{64}$")
    utxo_vout: int = Field(..., ge=0)
    bond_value: int | None = Field(default=None, ge=0)
    locktime: int = Field(..., ge=0)
    amount: int = Field(default=0, ge=0)
    script: str
    utxo_confirmations: int = Field(..., ge=0)
    utxo_confirmation_timestamp: int = Field(default=0, ge=0)
    cert_expiry: int = Field(..., ge=0)
    directory_node: str | None = None
    directory_nodes: list[str] = Field(
        default_factory=list,
        description="All directory nodes that announced this bond (for statistics)",
    )
    fidelity_bond_data: dict[str, Any] | None = None


class OrderBook(BaseModel):
    offers: list[Offer] = Field(default_factory=list)
    fidelity_bonds: list[FidelityBond] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    directory_nodes: list[str] = Field(default_factory=list)

    def add_offers(self, offers: list[Offer], directory_node: str) -> None:
        for offer in offers:
            offer.directory_node = directory_node
        self.offers.extend(offers)
        if directory_node not in self.directory_nodes:
            self.directory_nodes.append(directory_node)

    def add_fidelity_bonds(self, bonds: list[FidelityBond], directory_node: str) -> None:
        for bond in bonds:
            bond.directory_node = directory_node
        self.fidelity_bonds.extend(bonds)

    def get_offers_by_directory(self) -> dict[str, list[Offer]]:
        """Get offers grouped by directory node.

        This uses the directory_nodes list (plural) which tracks all directories
        that announced each offer, so an offer will appear under multiple
        directories if it was announced by multiple directories.
        """
        result: dict[str, list[Offer]] = {}
        for offer in self.offers:
            # Use directory_nodes (plural) if populated, otherwise fallback to directory_node
            nodes = offer.directory_nodes if offer.directory_nodes else []
            if not nodes and offer.directory_node:
                nodes = [offer.directory_node]
            if not nodes:
                nodes = ["unknown"]
            for node in nodes:
                if node not in result:
                    result[node] = []
                result[node].append(offer)
        return result
