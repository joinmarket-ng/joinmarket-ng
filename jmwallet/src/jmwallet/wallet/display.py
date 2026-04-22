"""
Wallet address information and display helpers.

Provides methods for querying address status, finding unused addresses,
and generating fidelity bond address summaries.
"""

from __future__ import annotations

from pathlib import Path

from jmwallet.wallet.constants import FIDELITY_BOND_BRANCH
from jmwallet.wallet.models import AddressInfo, AddressStatus, UTXOInfo


class WalletDisplayMixin:
    """Mixin providing address info and display capabilities.

    Expects the host class to provide the attributes and methods defined
    on ``WalletService`` (utxo_cache, address_cache, addresses_with_history, etc.).
    """

    # Declared for mypy -- actually set by the host class __init__
    utxo_cache: dict[int, list[UTXOInfo]]
    address_cache: dict[str, tuple[int, int, int]]
    addresses_with_history: set[str]
    fidelity_bond_locktime_cache: dict[str, int]
    root_path: str
    data_dir: Path | None

    # Methods provided by the host class
    def get_address(self, mixdepth: int, change: int, index: int) -> str:
        raise NotImplementedError

    def get_receive_address(self, mixdepth: int, index: int) -> str:
        raise NotImplementedError

    def get_next_address_index(self, mixdepth: int, change: int) -> int:
        raise NotImplementedError

    def get_address_info_for_mixdepth(
        self,
        mixdepth: int,
        change: int,
        gap_limit: int = 6,
        used_addresses: set[str] | None = None,
        history_addresses: dict[str, str] | None = None,
    ) -> list[AddressInfo]:
        """
        Get detailed address information for a mixdepth branch.

        This generates a list of AddressInfo objects for addresses in the
        specified mixdepth and branch (external or internal), up to the
        specified gap limit beyond the last used address.

        Args:
            mixdepth: The mixdepth (account) number (0-4)
            change: Branch (0 for external/receive, 1 for internal/change)
            gap_limit: Number of empty addresses to show beyond last used
            used_addresses: Set of addresses that were used in CoinJoin history
            history_addresses: Dict mapping address -> status from history

        Returns:
            List of AddressInfo objects for display
        """
        if used_addresses is None:
            used_addresses = set()
        if history_addresses is None:
            history_addresses = {}

        is_external = change == 0
        addresses: list[AddressInfo] = []

        # Get UTXOs for this mixdepth
        utxos = self.utxo_cache.get(mixdepth, [])

        # Build maps of address -> balance and address -> has_unconfirmed
        address_balances: dict[str, int] = {}
        address_unconfirmed: dict[str, bool] = {}
        for utxo in utxos:
            if utxo.address not in address_balances:
                address_balances[utxo.address] = 0
                address_unconfirmed[utxo.address] = False
            address_balances[utxo.address] += utxo.value
            # Track if any UTXO at this address is unconfirmed (0 confirmations)
            if utxo.confirmations == 0:
                address_unconfirmed[utxo.address] = True

        # Find the highest index with funds or history
        max_used_index = -1
        for address, (md, ch, idx) in self.address_cache.items():
            if md == mixdepth and ch == change:
                has_balance = address in address_balances
                # Check both CoinJoin history AND general blockchain activity
                has_history = address in used_addresses or address in self.addresses_with_history
                if has_balance or has_history:
                    if idx > max_used_index:
                        max_used_index = idx

        # Also check UTXOs directly
        for utxo in utxos:
            if utxo.address in self.address_cache:
                md, ch, idx = self.address_cache[utxo.address]
                if md == mixdepth and ch == change and idx > max_used_index:
                    max_used_index = idx

        # Generate addresses from 0 to max_used_index + gap_limit
        end_index = max(0, max_used_index + 1 + gap_limit)

        for index in range(end_index):
            address = self.get_address(mixdepth, change, index)
            path = f"{self.root_path}/{mixdepth}'/{change}/{index}"
            balance = address_balances.get(address, 0)

            # Determine status
            status = self._determine_address_status(
                address=address,
                balance=balance,
                is_external=is_external,
                used_addresses=used_addresses,
                history_addresses=history_addresses,
            )

            addresses.append(
                AddressInfo(
                    address=address,
                    index=index,
                    balance=balance,
                    status=status,
                    path=path,
                    is_external=is_external,
                    has_unconfirmed=address_unconfirmed.get(address, False),
                )
            )

        return addresses

    def _determine_address_status(
        self,
        address: str,
        balance: int,
        is_external: bool,
        used_addresses: set[str],
        history_addresses: dict[str, str],
    ) -> AddressStatus:
        """
        Determine the status label for an address.

        Args:
            address: The address to check
            balance: Current balance in satoshis
            is_external: True if external (receive) address
            used_addresses: Set of addresses used in CoinJoin history
            history_addresses: Dict mapping address -> type (cj_out, change, etc.)

        Returns:
            Status string for display
        """
        # Check if it was used in CoinJoin history
        history_type = history_addresses.get(address)

        if balance > 0:
            # Has funds
            if history_type == "cj_out":
                return "cj-out"
            elif history_type == "change":
                # Change output from a CoinJoin transaction we created.
                # NOTE: unlike "cj-out" (an equal-amount output which can
                # plausibly belong to any participant), "cj-change" is
                # deanonymising — it ties this address back to our
                # specific CoinJoin — so we label it distinctly from
                # ordinary "non-cj-change" outputs.
                return "cj-change"
            elif history_type == "flagged":
                # Address was shared as part of a CoinJoin we initiated
                # (recorded in history), and now has funds at that address.
                # This is our pending CJ output (not yet confirmed: the
                # monitor will flip entry.success=True and the history_type
                # to cj_out/change after first confirmation). Treat it as
                # the CJ output it is rather than mislabeling it "deposit"
                # (external) or "non-cj-change" (internal).
                if is_external:
                    return "cj-out"
                else:
                    return "cj-change"
            elif is_external:
                return "deposit"
            else:
                # Internal address with funds but not from CJ
                return "non-cj-change"
        else:
            # No funds
            # Check if address was used in CoinJoin history OR had blockchain activity
            was_used_in_cj = address in used_addresses
            had_blockchain_activity = address in self.addresses_with_history

            if was_used_in_cj or had_blockchain_activity:
                # Was used but now empty
                if history_type == "cj_out":
                    return "used-empty"  # CJ output that was spent
                elif history_type == "change":
                    return "used-empty"  # Change that was spent
                elif history_type == "flagged":
                    return "flagged"  # Shared but tx failed
                else:
                    return "used-empty"
            else:
                return "new"

    def get_next_after_last_used_address(
        self,
        mixdepth: int,
        used_addresses: set[str] | None = None,
    ) -> tuple[str, int]:
        """
        Get the next receive address after the last used one for a mixdepth.

        This returns the address at (highest used index + 1). The highest used index
        is determined by checking blockchain history, UTXOs, and CoinJoin history.
        If no address has been used yet, returns index 0.

        This is useful for wallet info display, showing the next address to use
        after the last one that was used in any way, ignoring any gaps in the sequence.

        Args:
            mixdepth: The mixdepth (account) number
            used_addresses: Set of addresses that were used/flagged in CoinJoins

        Returns:
            Tuple of (address, index)
        """
        if used_addresses is None:
            if self.data_dir:
                from jmwallet.history import get_used_addresses

                used_addresses = get_used_addresses(self.data_dir)
            else:
                used_addresses = set()

        max_index = -1
        change = 0  # external/receive chain

        # Check addresses with current UTXOs
        utxos = self.utxo_cache.get(mixdepth, [])
        for utxo in utxos:
            if utxo.address in self.address_cache:
                md, ch, idx = self.address_cache[utxo.address]
                if md == mixdepth and ch == change and idx > max_index:
                    max_index = idx

        # Check addresses that ever had blockchain activity (including spent)
        for address in self.addresses_with_history:
            if address in self.address_cache:
                md, ch, idx = self.address_cache[address]
                if md == mixdepth and ch == change and idx > max_index:
                    max_index = idx

        # Check CoinJoin history for addresses that may have been shared
        for address in used_addresses:
            if address in self.address_cache:
                md, ch, idx = self.address_cache[address]
                if md == mixdepth and ch == change and idx > max_index:
                    max_index = idx

        # Return next index after the last used (or 0 if none used)
        next_index = max_index + 1

        address = self.get_receive_address(mixdepth, next_index)
        return address, next_index

    def get_next_unused_unflagged_address(
        self,
        mixdepth: int,
        used_addresses: set[str] | None = None,
    ) -> tuple[str, int]:
        """
        Get the next unused and unflagged receive address for a mixdepth.

        An address is considered "used" if it has blockchain history (received/spent funds).
        An address is considered "flagged" if it was shared with peers in a
        CoinJoin attempt (even if the transaction failed). These should not
        be reused for privacy.

        This method starts from the next index after the highest used address
        (based on blockchain history, UTXOs, and CoinJoin history), ensuring
        we never reuse addresses that have been seen on-chain.

        Args:
            mixdepth: The mixdepth (account) number
            used_addresses: Set of addresses that were used/flagged in CoinJoins

        Returns:
            Tuple of (address, index)
        """
        if used_addresses is None:
            if self.data_dir:
                from jmwallet.history import get_used_addresses

                used_addresses = get_used_addresses(self.data_dir)
            else:
                used_addresses = set()

        # Start from the next address after the highest used one
        # This accounts for blockchain history, UTXOs, and CoinJoin history
        index = self.get_next_address_index(mixdepth, 0)  # 0 = external/receive chain
        max_attempts = 1000  # Safety limit

        for _ in range(max_attempts):
            address = self.get_receive_address(mixdepth, index)
            if address not in used_addresses:
                return address, index
            index += 1

        raise RuntimeError(f"Could not find unused address after {max_attempts} attempts")

    def get_fidelity_bond_addresses_info(
        self,
        max_gap: int = 6,
    ) -> list[AddressInfo]:
        """
        Get information about fidelity bond addresses.

        Args:
            max_gap: Maximum gap of empty addresses to show

        Returns:
            List of AddressInfo for fidelity bond addresses
        """
        addresses: list[AddressInfo] = []

        # Get UTXOs that are fidelity bonds (in mixdepth 0)
        utxos = self.utxo_cache.get(0, [])
        bond_utxos = [u for u in utxos if u.is_timelocked]

        # Build address -> balance map and address -> has_unconfirmed map for bonds
        address_balances: dict[str, int] = {}
        address_unconfirmed: dict[str, bool] = {}
        for utxo in bond_utxos:
            if utxo.address not in address_balances:
                address_balances[utxo.address] = 0
                address_unconfirmed[utxo.address] = False
            address_balances[utxo.address] += utxo.value
            if utxo.confirmations == 0:
                address_unconfirmed[utxo.address] = True

        for address, locktime in self.fidelity_bond_locktime_cache.items():
            if address in self.address_cache:
                _, _, index = self.address_cache[address]
                balance = address_balances.get(address, 0)
                path = f"{self.root_path}/0'/{FIDELITY_BOND_BRANCH}/{index}:{locktime}"

                addresses.append(
                    AddressInfo(
                        address=address,
                        index=index,
                        balance=balance,
                        status="bond",
                        path=path,
                        is_external=False,
                        is_bond=True,
                        locktime=locktime,
                        has_unconfirmed=address_unconfirmed.get(address, False),
                    )
                )

        # Sort by locktime
        addresses.sort(key=lambda a: (a.locktime or 0, a.index))
        return addresses
