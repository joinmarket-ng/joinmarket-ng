"""Strict portable representation of an externally supplied PoDLE proof."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jmcore.podle import PoDLECommitment, verify_podle, verify_podle_binding

_LOWER_HEX = re.compile(r"^[0-9a-f]+$")


def _validate_lower_hex(value: str, *, field_name: str, length: int | None = None) -> str:
    """Reject non-canonical hex before it reaches cryptographic verification."""
    if not _LOWER_HEX.fullmatch(value):
        raise ValueError(f"{field_name} must be lowercase hexadecimal")
    if length is not None and len(value) != length:
        raise ValueError(f"{field_name} must be {length} hexadecimal characters")
    if len(value) % 2:
        raise ValueError(f"{field_name} must have an even hexadecimal length")
    return value


class ExternalPoDLEOutpoint(BaseModel):
    """Outpoint of the chain UTXO authorizing an external PoDLE proof."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    txid: str
    vout: int = Field(ge=0, le=0xFFFFFFFF)

    @field_validator("txid")
    @classmethod
    def validate_txid(cls, value: str) -> str:
        return _validate_lower_hex(value, field_name="txid", length=64)

    def to_string(self) -> str:
        """Return the canonical wire-format outpoint."""
        return f"{self.txid}:{self.vout}"


class ExternalPoDLE(BaseModel):
    """An externally generated PoDLE credential eligible for one taker use.

    The record carries enough public chain metadata for a taker to verify the
    backing UTXO independently before it reveals the proof to makers. It never
    contains a private key and the backing UTXO is not a CoinJoin funding input.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1]
    network: Literal["mainnet", "testnet", "signet", "regtest"]
    outpoint: ExternalPoDLEOutpoint
    P: str
    P2: str
    sig: str
    e: str
    commitment: str
    index: int = Field(ge=0, le=255)
    scriptpubkey: str = Field(max_length=1040)
    blockheight: int = Field(gt=0)

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value

    @field_validator("P")
    @classmethod
    def validate_p(cls, value: str) -> str:
        return _validate_lower_hex(value, field_name="P", length=66)

    @field_validator("P2")
    @classmethod
    def validate_p2(cls, value: str) -> str:
        return _validate_lower_hex(value, field_name="P2", length=66)

    @field_validator("sig", "e", "commitment")
    @classmethod
    def validate_scalar_or_commitment(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "field")
        return _validate_lower_hex(value, field_name=field_name, length=64)

    @field_validator("scriptpubkey")
    @classmethod
    def validate_scriptpubkey(cls, value: str) -> str:
        if len(value) > 1040:
            raise ValueError("scriptpubkey must be at most 520 bytes")
        return _validate_lower_hex(value, field_name="scriptpubkey")

    @model_validator(mode="after")
    def verify_proof_and_binding(self) -> Self:
        """Require a proof for precisely this NUMS index and its claimed script."""
        valid, error = verify_podle(
            p=bytes.fromhex(self.P),
            p2=bytes.fromhex(self.P2),
            sig=bytes.fromhex(self.sig),
            e=bytes.fromhex(self.e),
            commitment=bytes.fromhex(self.commitment),
            index_range=range(self.index, self.index + 1),
        )
        if not valid:
            raise ValueError(f"invalid PoDLE proof: {error}")

        bound, error = verify_podle_binding(bytes.fromhex(self.P), self.scriptpubkey)
        if not bound:
            raise ValueError(f"PoDLE proof is not bound to scriptpubkey: {error}")
        return self

    def to_podle_commitment(self) -> PoDLECommitment:
        """Convert the public record to the runtime commitment representation."""
        return PoDLECommitment(
            commitment=bytes.fromhex(self.commitment),
            p=bytes.fromhex(self.P),
            p2=bytes.fromhex(self.P2),
            sig=bytes.fromhex(self.sig),
            e=bytes.fromhex(self.e),
            utxo=self.outpoint.to_string(),
            index=self.index,
        )
