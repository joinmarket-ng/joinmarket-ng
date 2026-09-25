# JoinMarket Orderbook Watcher

A clean, performant, and secure orderbook watcher for JoinMarket that aggregates offers from multiple directory nodes via Tor.

## Features

- **Tor Integration**: Connects to directory nodes via Tor for privacy
- **Multi-Directory Aggregation**: Fetches and combines orderbooks from multiple directory nodes
- **Web Interface**: Clean, modern UI with real-time updates
- **Separate Markets**: CoinJoin, PoDLE, and fidelity-bond rental offers have separate tabs, counts, searches, and sorting
- **Advanced Filtering**: Filter by offer type, directory node, and counterparty
- **Selection Estimate**: Compare round-level offer pick chances under current quantized-only taker defaults
- **Directory Statistics**: See offer counts per directory node
- **Direct Peer Indicator**: Highlights makers reachable via direct onion connection (beyond what directories advertise)
- **Mempool.space Integration**: Validates fidelity bonds using mempool.space API
- **Docker Support**: Easy deployment with Docker Compose

## Credential Market Offers

The watcher requests public `mbook` listings every 30 seconds over its existing
directory connections. It verifies the announcing nick's signature and each
listing's seller signature, network, and expiry, then merges identical listings
received from multiple directories. Listings expire locally and the in-memory
cache is bounded to 256 advertisements. No wallet, keys, or payment service is
needed to browse them.

`orderbook.json` retains its CoinJoin `offers` and `fidelitybonds` fields and adds
`credential_market.podle_offers` and `credential_market.bond_offers`. Each entry
contains `seller_nick`, `directory_nodes`, and the signed `listing` document.
A seller advertising both products appears in both separate market views.
Expired advertisements are removed from cached HTTP responses at their expiry,
even if a background refresh fails. This does not trigger additional network
queries or change the cached CoinJoin fields.
These advertisements do not prove available inventory, collateral, or the
announcing nick's ownership of the seller key. A buyer must validate its quote
and backing independently before payment. The watcher does not trade.

Run the frontend regression suite from `tests/playwright` with
`bun run test:obwatcher`. It uses local fixtures and does not contact a live market.

## Documentation

For full documentation, see [orderbook_watcher Documentation](https://joinmarket-ng.github.io/joinmarket-ng/README-orderbook-watcher/).
