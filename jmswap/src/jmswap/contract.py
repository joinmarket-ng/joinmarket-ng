# Copyright (C) 2026 The Electrum developers
# Distributed under the MIT software license.
"""Canonical Electrum taproot-v1 contract, adapted from Electrum."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from coincurve import PublicKey
from electrum_ecc import musig
from jmcore.bitcoin import NetworkType, scriptpubkey_to_address, sha256, tagged_hash
from jmcore.taproot import (
    LEAF_VERSION_TAPSCRIPT,
    OP_CHECKLOCKTIMEVERIFY,
    OP_CHECKSIG,
    OP_CHECKSIGVERIFY,
    OP_EQUALVERIFY,
    OP_HASH160,
    OP_SIZE,
    TapTreeNode,
    construct_script,
    control_block,
    encode_script_num,
    taproot_merkle_root,
    taproot_output_script,
)

NLOCKTIME_BLOCKHEIGHT_MAX = 500_000_000


class SwapDirection(StrEnum):
    FORWARD = "forward"
    REVERSE = "reverse"


class SwapLeaf(StrEnum):
    CLAIM = "claim"
    REFUND = "refund"


def _require_bytes(value: object, *, name: str, length: int) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError(f"{name} must be bytes")
    if len(value) != length:
        raise ValueError(f"{name} must be {length} bytes")
    return value


def validate_pubkey(pubkey: object, *, name: str) -> bytes:
    value = _require_bytes(pubkey, name=name, length=33)
    if value[0] not in (2, 3):
        raise ValueError(f"{name} must be a compressed public key")
    try:
        PublicKey(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid public key") from exc
    return value


@dataclass(frozen=True)
class TaprootSwapContract:
    """Electrum taproot-v1 contract with provider-first MuSig aggregation."""

    direction: SwapDirection
    payment_hash: bytes
    locktime: int
    provider_pubkey: bytes
    user_pubkey: bytes

    def __post_init__(self) -> None:
        if type(self.direction) is not SwapDirection:
            raise TypeError("direction must be a SwapDirection")
        _require_bytes(self.payment_hash, name="payment_hash", length=32)
        if type(self.locktime) is not int or not 0 < self.locktime <= NLOCKTIME_BLOCKHEIGHT_MAX:
            raise ValueError("locktime must be a positive block height")
        validate_pubkey(self.provider_pubkey, name="provider_pubkey")
        validate_pubkey(self.user_pubkey, name="user_pubkey")
        if self.provider_pubkey[1:] == self.user_pubkey[1:]:
            raise ValueError("provider and user x-only keys must be distinct")

    @property
    def claim_pubkey(self) -> bytes:
        return self.provider_pubkey if self.direction is SwapDirection.FORWARD else self.user_pubkey

    @property
    def refund_pubkey(self) -> bytes:
        return self.user_pubkey if self.direction is SwapDirection.FORWARD else self.provider_pubkey

    @property
    def claim_leaf_script(self) -> bytes:
        items: list[int | bytes] = []
        if self.direction is SwapDirection.REVERSE:
            items.extend((OP_SIZE, encode_script_num(32), OP_EQUALVERIFY))
        payment_hash160 = hashlib.new("ripemd160", self.payment_hash).digest()
        items.extend(
            (OP_HASH160, payment_hash160, OP_EQUALVERIFY, self.claim_pubkey[1:], OP_CHECKSIG)
        )
        return construct_script(items)

    @property
    def refund_leaf_script(self) -> bytes:
        return construct_script(
            [
                self.refund_pubkey[1:],
                OP_CHECKSIGVERIFY,
                encode_script_num(self.locktime),
                OP_CHECKLOCKTIMEVERIFY,
            ]
        )

    @property
    def tap_tree(self) -> TapTreeNode:
        return [
            (LEAF_VERSION_TAPSCRIPT, self.claim_leaf_script),
            (LEAF_VERSION_TAPSCRIPT, self.refund_leaf_script),
        ]

    @property
    def merkle_root(self) -> bytes:
        return taproot_merkle_root(self.tap_tree)

    def keyagg_cache(self) -> musig.KeyAggCache:
        return cast(
            musig.KeyAggCache,
            musig.KeyAggCache.from_pubkeys([self.provider_pubkey, self.user_pubkey]),
        )

    @property
    def internal_pubkey(self) -> bytes:
        return cast(bytes, self.keyagg_cache().aggregate_xonly_pubkey())

    @property
    def output_script(self) -> bytes:
        return taproot_output_script(self.internal_pubkey, self.tap_tree)

    def address(self, network: str | NetworkType = NetworkType.MAINNET) -> str:
        return scriptpubkey_to_address(self.output_script, network)

    def script_path(self, leaf: SwapLeaf) -> tuple[bytes, bytes]:
        if type(leaf) is not SwapLeaf:
            raise TypeError("leaf must be a SwapLeaf")
        return control_block(
            self.internal_pubkey, self.tap_tree, 0 if leaf is SwapLeaf.CLAIM else 1
        )

    def serialized_tree(self) -> dict[str, dict[str, int | str]]:
        return {
            "claimLeaf": {
                "version": LEAF_VERSION_TAPSCRIPT,
                "output": self.claim_leaf_script.hex(),
            },
            "refundLeaf": {
                "version": LEAF_VERSION_TAPSCRIPT,
                "output": self.refund_leaf_script.hex(),
            },
        }

    def validate_provider_data(
        self,
        *,
        serialized_tree: object,
        address: str,
        network: str | NetworkType = NetworkType.MAINNET,
    ) -> None:
        if type(serialized_tree) is not dict:
            raise ValueError("swap tree must be an object")
        expected = {"claimLeaf": self.claim_leaf_script, "refundLeaf": self.refund_leaf_script}
        if set(serialized_tree) != set(expected):
            raise ValueError("swap tree must contain only claimLeaf and refundLeaf")
        for name, script in expected.items():
            leaf: Any = serialized_tree[name]
            if type(leaf) is not dict or set(leaf) != {"version", "output"}:
                raise ValueError(f"{name} must contain only version and output")
            if leaf["version"] != LEAF_VERSION_TAPSCRIPT:
                raise ValueError(f"{name} has an unsupported tapleaf version")
            output = leaf["output"]
            if not isinstance(output, str) or len(output) > 20_000:
                raise ValueError(f"{name} output must be bounded hex")
            try:
                actual = bytes.fromhex(output)
            except ValueError as exc:
                raise ValueError(f"{name} output must be hex") from exc
            if actual != script:
                raise ValueError(f"{name} does not match the expected contract")
        if not isinstance(address, str) or address != self.address(network):
            raise ValueError("swap address does not match the expected contract")


def taproot_tweak_hash(internal_pubkey: bytes, merkle_root: bytes) -> bytes:
    return tagged_hash("TapTweak", internal_pubkey + merkle_root)


def payment_hash(preimage: bytes) -> bytes:
    return sha256(preimage)
