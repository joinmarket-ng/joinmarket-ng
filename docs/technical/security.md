# Security

This page summarizes practical security properties and controls.

## Threat Model (High Level)

Primary adversaries:

- malicious peers
- network observers
- malicious or degraded infrastructure nodes

Primary goals:

- protect funds from unauthorized signing
- reduce linkability between participant activity
- preserve availability under spam/DoS pressure

## Core Controls

- transaction verification before maker signing (see
  [Maker Verification Checklist](maker-verification-checklist.md))
- PoDLE anti-abuse commitments
- Tor-based transport and hidden-service support
- rate limiting and message validation in directory/maker paths
- fidelity bond weighting as Sybil-cost mechanism

## Directory and Messaging

- use multiple directory servers where possible
- prefer direct maker/taker channels when available
- enforce channel/session consistency during CoinJoin flow

## Neutrino Notes

- neutrino is convenient, but full-node backends remain the strongest default for verification and compatibility
- run neutrino infrastructure you trust and route traffic with Tor where possible
- neutrino-api supports TLS with certificate pinning and bearer-token authentication, enabled by default; see [Neutrino TLS](neutrino-tls.md) for setup details
- the TLS certificate is self-signed and pinned on first use (TOFU model), so only the specific neutrino-api instance that generated it is trusted

## Operational Advice

- treat mnemonics and wallet files as high-value secrets
- keep software updated
- test operational setup on testnet/signet/regtest before production use

For protocol-level details, see [Protocol](protocol.md) and [Privacy](privacy.md).
For the maker-side pre-sign checklist, see
[Maker Verification Checklist](maker-verification-checklist.md).
