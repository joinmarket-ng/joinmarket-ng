"""
Manager for PoDLE commitments (used for retry tracking).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jmcore.commitment_blacklist import get_blacklist
from jmcore.paths import get_used_commitments_path
from jmcore.podle import generate_podle
from loguru import logger

from taker.podle import ExtendedPoDLECommitment, get_eligible_podle_utxos

if TYPE_CHECKING:
    from jmwallet.wallet.models import UTXOInfo


class PoDLEManager:
    """Manages tracking of used PoDLE commitments."""

    def __init__(self, data_dir: Path | None = None):
        self.filepath = get_used_commitments_path(data_dir)
        self.used_commitments: set[str] = set()
        self.external_commitments: dict = {}
        self._load()

    def _load(self) -> None:
        """Load used commitments from file."""
        if not self.filepath.exists():
            return
        try:
            with open(self.filepath) as f:
                data = json.load(f)
                # Handle reference implementation format: {"used": ["hex..."], "external": ...}
                if isinstance(data, dict):
                    self.used_commitments = set(data.get("used", []))
                    self.external_commitments = data.get("external", {})
                else:
                    self.used_commitments = set()
                    self.external_commitments = {}
            logger.debug(f"Loaded {len(self.used_commitments)} used PoDLE commitments")
        except Exception as e:
            logger.error(f"Failed to load used commitments: {e}")

    def _save(self) -> None:
        """Save used commitments to file."""
        try:
            data = {
                "used": list(self.used_commitments),
                "external": self.external_commitments,
            }
            with open(self.filepath, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save used commitments: {e}")

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
        private_key_getter: Any,  # Callable[[str], bytes]
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
        eligible_utxos = get_eligible_podle_utxos(
            wallet_utxos, cj_amount, min_confirmations, min_percent
        )

        if not eligible_utxos:
            logger.warning("No eligible UTXOs for PoDLE")
            return None

        # Consult both our local "used" set and the shared blacklist file.
        # The blacklist (cmtdata/commitmentlist) may be populated from !hp2
        # broadcasts or from remote "blacklisted" rejections we persisted.
        # It is harmless (but slightly slower) when empty, e.g. fresh installs.
        try:
            blacklist = get_blacklist()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not load commitment blacklist: {exc}")
            blacklist = None

        # Try each UTXO in order (already sorted by confirmations, value)
        # Fresh UTXOs naturally succeed faster (at index 0)
        for utxo in eligible_utxos:
            private_key = private_key_getter(utxo.address)
            if private_key is None:
                continue

            utxo_str = f"{utxo.txid}:{utxo.vout}"

            # Try indices 0..max_retries-1 for this UTXO
            for index in range(max_retries):
                try:
                    # Generate commitment to check hash
                    podle = generate_podle(private_key, utxo_str, index)
                    commitment_hex = podle.commitment.hex()

                    if commitment_hex in self.used_commitments:
                        logger.debug(f"PoDLE commitment for {utxo_str} index {index} already used")
                        continue

                    if blacklist is not None and blacklist.is_blacklisted(commitment_hex):
                        logger.debug(
                            f"PoDLE commitment for {utxo_str} index {index} is blacklisted"
                        )
                        # Also persist to used_commitments so we don't regenerate
                        # the same candidate next call.
                        self.used_commitments.add(commitment_hex)
                        self._save()
                        continue

                    # Found unused commitment
                    self.used_commitments.add(commitment_hex)
                    self._save()

                    logger.info(
                        f"Generated fresh PoDLE for {utxo_str} using index {index} "
                        f"(utxo value={utxo.value}, confs={utxo.confirmations})"
                    )

                    return ExtendedPoDLECommitment(
                        commitment=podle,
                        scriptpubkey=utxo.scriptpubkey,
                        blockheight=utxo.height,
                    )
                except Exception as e:
                    logger.warning(f"Failed to generate PoDLE for {utxo_str} index {index}: {e}")
                    continue

            # All indices exhausted for this UTXO
            logger.debug(f"Skipping {utxo.txid}:{utxo.vout} - all {max_retries} indices used")

        logger.error("Failed to generate any fresh PoDLE commitment from available UTXOs")
        return None
