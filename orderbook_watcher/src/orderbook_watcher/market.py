"""Bounded, ephemeral discovery state for signed credential advertisements."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from jmcore.credential_market import MarketListing, SignedDocument, accept_listing

MAX_MARKET_OFFERS = 256


@dataclass
class _ObservedListing:
    seller_nick: str
    document: SignedDocument
    listing: MarketListing
    directories: set[str] = field(default_factory=set)


class MarketListingCache:
    """Keep validated advertisements, never inventory or collateral assessments."""

    def __init__(self, network: str) -> None:
        self.network = network
        self._offers: OrderedDict[tuple[str, str, int], _ObservedListing] = OrderedDict()

    def _expire(self, now: int) -> None:
        for key, observation in tuple(self._offers.items()):
            if observation.listing.expires_at <= now:
                del self._offers[key]

    def observe(self, seller_nick: str, raw: bytes, directory: str, now: int) -> bool:
        """Accept a nick-authenticated document from the directory client's drain."""
        self._expire(now)
        try:
            document, listing = accept_listing(raw, self.network, now)
        except ValueError:
            return False
        key = (seller_nick, listing.seller_pubkey, listing.period)
        prior = self._offers.get(key)
        if prior is not None:
            if listing.expires_at < prior.listing.expires_at:
                return False
            if document == prior.document:
                prior.directories.add(directory)
                return True
        # Only attribute the selected document to directories that relayed it.
        self._offers[key] = _ObservedListing(seller_nick, document, listing, {directory})
        self._offers.move_to_end(key)
        if len(self._offers) > MAX_MARKET_OFFERS:
            self._offers.popitem(last=False)
        return True

    def snapshot(self, now: int) -> dict[str, list[dict[str, Any]]]:
        self._expire(now)
        result: dict[str, list[dict[str, Any]]] = {"podle_offers": [], "bond_offers": []}
        for observation in self._offers.values():
            entry = {
                "seller_nick": observation.seller_nick,
                "listing": observation.document.model_dump(mode="json"),
                "directory_nodes": sorted(observation.directories),
            }
            for product in sorted(set(observation.listing.products)):
                result[f"{product}_offers"].append(entry)
        return result
