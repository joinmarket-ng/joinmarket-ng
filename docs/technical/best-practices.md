# Best Practices

This page intentionally stays short and links to concrete operational guides.

## Users

- Start with [Wallet](../README-jmwallet.md), then [Taker](../README-taker.md)
- Prefer internal destinations (`INTERNAL`) when building privacy across mixdepths
- Run multiple rounds over time instead of one large single round
- Keep wallet backups offline (mnemonic is the critical backup)

## Makers

- Prefer Bitcoin Core `descriptor_wallet` backend for full compatibility
- Run with Tor control enabled for ephemeral onion services
- Keep fees simple at first; tune only after observing market behavior
- Monitor logs and wallet balance regularly

## Operators and Contributors

- Keep docs focused on defaults and working paths; avoid duplicate deep dives
- Prefer linking to one canonical page per topic
- Validate docs by running commands in a clean environment
