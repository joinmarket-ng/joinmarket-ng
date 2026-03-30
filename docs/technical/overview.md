# JoinMarket NG vs Reference Implementation

JoinMarket NG is an alternative implementation of the JoinMarket protocol.

It keeps wire-level compatibility with the reference implementation while using a modern codebase and tooling.

## What Is Different

- no long-lived JoinMarket daemon model for normal maker/taker CLI usage
- modern Python stack (Python 3.11+, AsyncIO, Pydantic v2, strict typing)
- built-in Neutrino backend support
- active compatibility testing against the reference ecosystem

## Compatibility Model

JoinMarket NG uses protocol version 5 and negotiates optional capabilities via handshake features.

Example feature keys include:

- `extended_peerlist`
- `neutrino_compat`

This allows gradual rollout without a network-wide protocol version bump.

## Neutrino Interop Notes

- Neutrino takers require makers that advertise `neutrino_compat`
- Full-node takers can interact with both legacy and feature-advertising makers

See [Protocol](protocol.md) for details on feature negotiation and message format differences.
