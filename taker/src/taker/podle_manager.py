"""
Manager for PoDLE commitments (used for retry tracking).
"""

from __future__ import annotations

import binascii
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jmcore.commitment_blacklist import get_blacklist
from jmcore.paths import (
    GENERATED_WALLETS_ONLY,
    create_commitment_history,
    get_used_commitments_path,
)
from jmcore.podle import PoDLECommitment, generate_podle
from jmcore.secure_files import atomic_write_sensitive_file
from jmwallet.commitment_history import (
    generated_wallet_is_authorized,
    history_requires_generation_authorization,
)
from loguru import logger

from taker.podle import ExtendedPoDLECommitment, get_eligible_podle_utxos

if TYPE_CHECKING:
    from jmwallet.wallet.models import UTXOInfo


class CommitmentHistoryError(Exception):
    """Commitment history needs operator action before CoinJoin can proceed."""


def initialize_commitment_history(data_dir: Path | None = None) -> Path:
    """Explicitly initialize only a missing history, never replace existing state."""
    try:
        path = create_commitment_history(data_dir)
    except FileExistsError as exc:
        raise CommitmentHistoryError(
            "Commitment history already exists; it was not replaced."
        ) from exc
    except OSError as exc:
        raise CommitmentHistoryError(
            "Could not initialize commitment history. Preserve any created bytes and "
            "resolve the storage failure before attempting recovery."
        ) from exc
    return path


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate commitment-history field")
        result[key] = value
    return result


def _validate_hex(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("Commitment-history hex fields must be strings")
    # Match the reference persistence reader, not proof verification: either
    # case is accepted, but whitespace and non-ASCII hex are not.
    binascii.unhexlify(value)


def _validate_history(data: object) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("used"), list):
        raise ValueError("Commitment history must contain a used list")
    if not isinstance(data.get("external"), dict):
        raise ValueError("Commitment history must contain an external object")
    if GENERATED_WALLETS_ONLY in data and data[GENERATED_WALLETS_ONLY] is not True:
        raise ValueError("Unknown automatic commitment-history restriction")
    for commitment in data["used"]:
        _validate_hex(commitment)
    for outpoint, entry in data["external"].items():
        if not isinstance(outpoint, str):
            raise ValueError("External commitment outpoints must be strings")
        parts = outpoint.split(":")
        if len(parts) != 2 or len(parts[0]) != 64 or int(parts[1]) < 0:
            raise ValueError("Invalid external commitment outpoint")
        _validate_hex(parts[0])
        if not isinstance(entry, dict) or not isinstance(entry.get("reveal"), dict):
            raise ValueError("External commitment must contain P and reveal records")
        _validate_hex(entry.get("P"))
        for index, reveal in entry["reveal"].items():
            int(index)  # Reference indices need not have canonical decimal spelling.
            if not isinstance(reveal, dict):
                raise ValueError("Invalid external commitment reveal")
            for field in ("P2", "s", "e"):
                _validate_hex(reveal.get(field))
    return data


class PoDLEManager:
    """Manages tracking of used PoDLE commitments."""

    def __init__(self, data_dir: Path | None = None, *, mnemonic: str | None = None) -> None:
        try:
            self.filepath = get_used_commitments_path(data_dir)
        except OSError as exc:
            raise CommitmentHistoryError(
                "Could not access the commitment-history directory."
            ) from exc
        self.used_commitments: set[str] = set()
        self.external_commitments: dict[str, Any] = {}
        self._history_data: dict[str, Any] = {}
        self._load(mnemonic=mnemonic)

    def _load(self, *, mnemonic: str | None = None) -> None:
        """Load used commitments from file."""
        try:
            with open(self.filepath, encoding="utf-8") as history:
                data = _validate_history(json.load(history, object_pairs_hook=_unique_json_object))
        except FileNotFoundError as exc:
            raise CommitmentHistoryError(
                f"Commitment history is missing at {self.filepath}. Restore trusted history "
                "for previously used wallets. Only new, unused histories may be initialized "
                "with jm-taker init-commitment-history."
            ) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise CommitmentHistoryError(
                f"Could not load commitment history at {self.filepath}; bytes were left untouched. "
                "Restore trusted history or resolve the read failure before retrying."
            ) from exc
        if history_requires_generation_authorization(self.filepath.parent.parent, data) and (
            mnemonic is None
            or not generated_wallet_is_authorized(self.filepath.parent.parent, mnemonic)
        ):
            raise CommitmentHistoryError(
                "Automatically initialized commitment history does not authorize this wallet. "
                "Restore trusted commitment history before using a recovered or imported wallet."
            )
        self._history_data = data
        self.used_commitments = {value.lower() for value in data["used"]}
        self.external_commitments = data["external"]
        logger.debug(f"Loaded {len(self.used_commitments)} used PoDLE commitments")

    def _save(self) -> None:
        """Save used commitments to file."""
        try:
            data = {
                **self._history_data,
                "used": sorted(self.used_commitments),
                "external": self.external_commitments,
            }
            _validate_history(data)
            atomic_write_sensitive_file(self.filepath, json.dumps(data, indent=2).encode())
        except (OSError, ValueError, TypeError) as exc:
            raise CommitmentHistoryError(
                f"Could not persist commitment history at {self.filepath}. No fresh commitment "
                "was released; resolve the storage failure before retrying."
            ) from exc

    def get_utxo_retry_count(self, utxo_str: str, private_key: bytes, max_retries: int) -> int:
        """
        Get the number of times a UTXO has been used for PoDLE commitments.

        Checks indices 0..(max_retries-1) in reverse order and returns the highest
        index + 1 where a commitment is found in used_commitments.

        Note: Only used in tests. Production code uses lazy evaluation in
        generate_fresh_commitment() to avoid generating all commitments upfront.

        Returns:
            0 if UTXO is fresh (no used commitments)
            1-max_retries if UTXO has been used that many times
        """
        # Early termination: stop at first match (reverse order)
        for i in reversed(range(max_retries)):
            try:
                podle = generate_podle(private_key, utxo_str, i)
                commitment_hex = podle.commitment.hex()
                if commitment_hex in self.used_commitments:
                    return i + 1  # Found highest used index
            except Exception:
                continue
        return 0  # No used commitments found

    def generate_fresh_commitment(
        self,
        wallet_utxos: list[UTXOInfo],
        cj_amount: int,
        private_key_getter: Callable[[str], bytes | None],
        min_confirmations: int = 5,
        min_percent: int = 20,
        max_retries: int = 3,
    ) -> ExtendedPoDLECommitment | None:
        """
        Generate a fresh PoDLE commitment for a CoinJoin.

        Iterates through eligible UTXOs and tries indices 0..max_retries-1 until
        finding an unused commitment. UTXOs are pre-sorted by confirmations and value,
        so fresh UTXOs (which succeed at index 0) are naturally preferred.

        Args:
            wallet_utxos: Available wallet UTXOs
            cj_amount: CoinJoin amount
            private_key_getter: Function to get private key for address
            min_confirmations: Minimum UTXO confirmations required
            min_percent: Minimum UTXO value as % of cj_amount
            max_retries: Maximum number of retries per UTXO (default: 3)

        Returns:
            ExtendedPoDLECommitment or None if no fresh commitment available
        """
        candidates = self._iter_fresh_commitments(
            wallet_utxos,
            cj_amount,
            private_key_getter,
            min_confirmations,
            min_percent,
            max_retries,
        )
        for utxo, podle in candidates:
            commitment_hex = podle.commitment.hex()
            self.used_commitments.add(commitment_hex)
            self._save()

            logger.info("Generated fresh PoDLE commitment")
            logger.bind(sensitive=True).info(
                "Generated fresh PoDLE for {} using index {} (utxo value={}, confs={})",
                podle.utxo,
                podle.index,
                utxo.value,
                utxo.confirmations,
            )

            return ExtendedPoDLECommitment(
                commitment=podle,
                scriptpubkey=utxo.scriptpubkey,
                blockheight=utxo.height,
            )

        logger.error("Failed to generate any fresh PoDLE commitment from available UTXOs")
        return None

    def get_fresh_commitment_utxos(
        self,
        wallet_utxos: list[UTXOInfo],
        cj_amount: int,
        private_key_getter: Callable[[str], bytes | None],
        min_confirmations: int = 5,
        min_percent: int = 20,
        max_retries: int = 3,
    ) -> list[UTXOInfo]:
        """Return PoDLE-capable UTXOs without consuming a commitment index."""
        fresh: list[UTXOInfo] = []
        seen: set[tuple[str, int]] = set()
        for utxo, _ in self._iter_fresh_commitments(
            wallet_utxos,
            cj_amount,
            private_key_getter,
            min_confirmations,
            min_percent,
            max_retries,
        ):
            outpoint = (utxo.txid, utxo.vout)
            if outpoint not in seen:
                fresh.append(utxo)
                seen.add(outpoint)
        return fresh

    def _iter_fresh_commitments(
        self,
        wallet_utxos: list[UTXOInfo],
        cj_amount: int,
        private_key_getter: Callable[[str], bytes | None],
        min_confirmations: int,
        min_percent: int,
        max_retries: int,
    ) -> Iterator[tuple[UTXOInfo, PoDLECommitment]]:
        eligible_utxos = get_eligible_podle_utxos(
            wallet_utxos, cj_amount, min_confirmations, min_percent
        )
        if not eligible_utxos:
            logger.warning("No eligible UTXOs for PoDLE")
            return

        try:
            blacklist = get_blacklist()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not load commitment blacklist: {exc}")
            blacklist = None

        for utxo in eligible_utxos:
            private_key = private_key_getter(utxo.address)
            if private_key is None:
                continue

            utxo_str = f"{utxo.txid}:{utxo.vout}"
            found = False
            for index in range(max_retries):
                try:
                    podle = generate_podle(private_key, utxo_str, index)
                    commitment_hex = podle.commitment.hex()
                    if commitment_hex in self.used_commitments:
                        logger.debug("PoDLE commitment retry index already used")
                        logger.bind(sensitive=True).debug(
                            "PoDLE commitment for {} index {} already used", utxo_str, index
                        )
                        continue
                    if blacklist is not None and blacklist.is_blacklisted(commitment_hex):
                        logger.debug("PoDLE commitment retry index is blacklisted")
                        logger.bind(sensitive=True).debug(
                            "PoDLE commitment for {} index {} is blacklisted", utxo_str, index
                        )
                        self.used_commitments.add(commitment_hex)
                        self._save()
                        continue
                    found = True
                    yield utxo, podle
                    break
                except CommitmentHistoryError:
                    raise
                except Exception as exc:
                    logger.warning("Failed to generate PoDLE commitment")
                    logger.bind(sensitive=True).warning(
                        "Failed to generate PoDLE for {} index {}: {}", utxo_str, index, exc
                    )
            if not found:
                logger.debug("Skipping UTXO after all PoDLE retry indices were used")
                logger.bind(sensitive=True).debug(
                    "Skipping {}:{} after all {} PoDLE retry indices were used",
                    utxo.txid,
                    utxo.vout,
                    max_retries,
                )
