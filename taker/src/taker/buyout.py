"""The taker's view of the shared channel funding boundary.

The adapter itself lives in :mod:`jmswap.coinjoin_funding` because the maker
prepares buyouts the same way; this module keeps the taker's import path.
"""

from __future__ import annotations

from jmswap.coinjoin_funding import ChannelBuyout

__all__ = ["ChannelBuyout"]
