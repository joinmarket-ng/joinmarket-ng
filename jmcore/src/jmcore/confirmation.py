"""
User confirmation prompts for fund-moving operations.
"""

from __future__ import annotations

import os
import sys
from typing import Any


def is_interactive_mode() -> bool:
    """
    Check if we're running in interactive mode.

    Returns False if NO_INTERACTIVE env var is set or if not attached to a TTY.
    """
    if os.environ.get("NO_INTERACTIVE"):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


# Display width for coinjoin confirmation
_COINJOIN_WIDTH = 96
_LABEL_WIDTH = 16  # Width for labels like "CoinJoin Amount:"
_SEND_WIDTH = 80  # Display width for standard send confirmation


def _display_coinjoin_send_confirmation(
    amount: int,
    destination: str | None,
    mining_fee: int | None,
    additional_info: dict[str, Any] | None,
    stage: str = "",
) -> None:
    """Display coinjoin confirmation in column format."""
    from jmcore.bitcoin import format_amount

    print("\n" + "=" * _COINJOIN_WIDTH)
    if stage == "broadcast":
        print("FINAL COINJOIN Transaction -- ready to broadcast")
    elif stage == "initial":
        print("Expected COINJOIN Transaction -- fee estimates, confirm makers")
    else:
        print("Expected COINJOIN Transaction")
    print("=" * _COINJOIN_WIDTH)

    # Extract info from additional_info
    source_mixdepth = additional_info.get("Source Mixdepth") if additional_info else None
    makers = additional_info.get("Makers", []) if additional_info else []
    total_maker_fee = additional_info.get("Total Maker Fee", 0) if additional_info else 0
    fee_rate = additional_info.get("Fee Rate") if additional_info else None

    # Source Mixdepth
    if source_mixdepth is not None:
        print(f"{'Source Mixdepth:':<{_LABEL_WIDTH}}  {source_mixdepth}")

    # Destination
    if destination:
        if destination == "INTERNAL":
            print(f"{'Destination:':<{_LABEL_WIDTH}}  INTERNAL (next mixdepth)")
        else:
            print(f"{'Destination:':<{_LABEL_WIDTH}}  {destination}")

    # CoinJoin Amount
    if amount == 0:
        print(f"{'CoinJoin Amount:':<{_LABEL_WIDTH}}  SWEEP (all available funds)")
    else:
        print(f"{'CoinJoin Amount:':<{_LABEL_WIDTH}}  {format_amount(amount)}")

    # Makers (formatted with alignment)
    if makers:
        # First maker line includes label
        label = f"Makers ({len(makers)}):"
        for i, maker_str in enumerate(makers):
            if i == 0:
                print(f"{label:<{_LABEL_WIDTH}}  {i + 1}. {maker_str}")
            else:
                print(f"{'':<{_LABEL_WIDTH}}  {i + 1}. {maker_str}")

    # Total Maker Fee
    if total_maker_fee:
        print(f"{'Total Maker Fee:':<{_LABEL_WIDTH}}  {total_maker_fee:,} sats")

    # Miner Fee Rate
    if fee_rate is not None:
        print(f"{'Miner Fee Rate:':<{_LABEL_WIDTH}}  {fee_rate:.2f} sat/vB")

    # Mining fee
    if mining_fee is not None:
        print(f"{'Miner Fee:':<{_LABEL_WIDTH}}  {format_amount(mining_fee)}")

    # Total Fee (maker fee + miner fee)
    if mining_fee is not None and total_maker_fee:
        total_fee = mining_fee + total_maker_fee
        print(f"{'Total Fee:':<{_LABEL_WIDTH}}  {format_amount(total_fee)}")

    print("=" * _COINJOIN_WIDTH)


def _display_standard_send_confirmation(
    operation: str,
    amount: int,
    destination: str | None,
    fee: int | None,
    mining_fee: int | None,
    additional_info: dict[str, Any] | None,
) -> None:
    """Display standard send transaction confirmation (non-coinjoin)."""
    from jmcore.bitcoin import format_amount

    print("\n" + "=" * _SEND_WIDTH)
    print("Expected SEND Transaction")
    print("=" * _SEND_WIDTH)

    # Extract info from additional_info
    source_mixdepth = additional_info.get("Source Mixdepth") if additional_info else None
    change = additional_info.get("Change") if additional_info else None
    fee_rate = additional_info.get("Miner Fee Rate") if additional_info else None

    # Source Mixdepth
    if source_mixdepth is not None:
        print(f"{'Source Mixdepth:':<{_LABEL_WIDTH}}  {source_mixdepth}")

    # Destination
    if destination:
        if destination == "INTERNAL":
            print(f"{'Destination:':<{_LABEL_WIDTH}}  INTERNAL (next mixdepth)")
        else:
            print(f"{'Destination:':<{_LABEL_WIDTH}}  {destination}")

    # Amount
    if amount == 0:
        print(f"{'Amount:':<{_LABEL_WIDTH}}  SWEEP (all available funds)")
    else:
        print(f"{'Amount:':<{_LABEL_WIDTH}}  {format_amount(amount)}")

    # Change
    if change is not None:
        print(f"{'Change:':<{_LABEL_WIDTH}}  {change}")

    # Miner Fee Rate
    if fee_rate is not None:
        print(f"{'Miner Fee Rate:':<{_LABEL_WIDTH}}  {fee_rate}")

    # Miner Fee (use mining_fee if provided, otherwise fall back to fee)
    effective_mining_fee = mining_fee if mining_fee is not None else fee
    if effective_mining_fee is not None:
        print(f"{'Miner Fee:':<{_LABEL_WIDTH}}  {format_amount(effective_mining_fee)}")

    print("=" * _SEND_WIDTH)


def confirm_transaction(
    operation: str,
    amount: int,
    destination: str | None = None,
    fee: int | None = None,
    mining_fee: int | None = None,
    additional_info: dict[str, Any] | None = None,
    skip_confirmation: bool = False,
    stage: str = "",
) -> bool:
    """
    Prompt user to confirm a transaction that moves funds.

    Args:
        operation: Type of operation (e.g., "send", "coinjoin")
        amount: Amount in satoshis (0 for sweep)
        destination: Destination address (optional)
        fee: Total fee in satoshis (optional, for CoinJoin this is maker fees + mining fee)
        mining_fee: Mining/transaction fee in satoshis (optional)
        additional_info: Additional details to show (e.g., maker fees, counterparties)
        skip_confirmation: If True, skip prompt (from --yes flag)
        stage: Prompt stage identifier.  Pass "initial" for the maker-selection
            estimate and "broadcast" for the final pre-broadcast confirmation.
            Shown in the section header so users can tell the two prompts apart.

    Returns:
        True if user confirms, False otherwise

    Raises:
        RuntimeError: If in non-interactive mode without skip_confirmation
    """
    # Skip if confirmation disabled
    if skip_confirmation:
        return True

    # Error if non-interactive without --yes
    if not is_interactive_mode():
        raise RuntimeError(
            "Cannot prompt for confirmation in non-interactive mode. "
            "Use --yes flag or set NO_INTERACTIVE=1 to skip confirmation."
        )

    # Use different display for coinjoin vs regular transactions
    if operation.lower() == "coinjoin":
        _display_coinjoin_send_confirmation(
            amount=amount,
            destination=destination,
            mining_fee=mining_fee,
            additional_info=additional_info,
            stage=stage,
        )
    else:
        _display_standard_send_confirmation(
            operation=operation,
            amount=amount,
            destination=destination,
            fee=fee,
            mining_fee=mining_fee,
            additional_info=additional_info,
        )

    # Prompt for confirmation - flush stdout and clear any buffered stdin
    try:
        sys.stdout.flush()
        # Drain any pending input to ensure we get fresh user input
        # (important when running in asyncio context with logging)
        try:
            import termios

            # Flush input buffer to discard any stale data
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except ImportError:
            # Not Unix
            pass
        except (OSError, ValueError):
            # Not a TTY or no terminal settings available
            pass

        response = input("\nProceed with this transaction? [y/N]: ").strip().lower()
        return response in ("y", "yes")
    except (KeyboardInterrupt, EOFError):
        print("\n\nTransaction cancelled by user.")
        return False


def format_maker_summary(
    makers: list[dict[str, Any]], fee_rate: float | None = None
) -> dict[str, Any]:
    """
    Format maker information for confirmation display.

    Args:
        makers: List of selected maker dicts with 'nick', 'fee', 'bond_value', 'location', etc.
        fee_rate: Fee rate in sat/vB (optional)

    Returns:
        Dict with formatted maker info for confirmation display
    """
    total_maker_fee = sum(m.get("fee", 0) for m in makers)

    # Find max widths for alignment
    max_fee_width = max((len(f"{m.get('fee', 0):,}") for m in makers), default=1)
    max_bond_width = max((len(f"{m.get('bond_value', 0):,}") for m in makers), default=1)

    maker_details = []
    for m in makers:
        nick = m.get("nick", "unknown")
        fee = m.get("fee", 0)
        bond_value = m.get("bond_value", 0)
        location = m.get("location")

        # Right-align fee and bond values
        fee_str = f"{fee:>{max_fee_width},}"
        bond_str = f" [bond: {bond_value:>{max_bond_width},}]" if bond_value > 0 else " [no bond]"

        # Add location info if available
        if location and location != "NOT-SERVING-ONION":
            # Truncate onion address for readability (show first 16 chars)
            if ":" in location:
                onion, port = location.rsplit(":", 1)
                if onion.endswith(".onion") and len(onion) > 20:
                    location_str = f" @ {onion[:16]}...:{port}"
                else:
                    location_str = f" @ {location}"
            else:
                location_str = f" @ {location[:20]}..."
            maker_details.append(f"{nick}: {fee_str} sats{bond_str}{location_str}")
        else:
            maker_details.append(f"{nick}: {fee_str} sats{bond_str}")

    result: dict[str, Any] = {
        "Total Maker Fee": total_maker_fee,
        "Makers": maker_details,
    }

    if fee_rate is not None:
        result["Fee Rate"] = fee_rate

    return result
