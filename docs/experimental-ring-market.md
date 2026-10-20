# Experimental Ring Market

Taproot CoinJoins and private channel buyouts are experimental and not audited.
Their protocols and on-disk formats may change incompatibly, and bugs can lose
funds. They are disabled by default. Start on signet with a dedicated wallet,
dedicated LND nodes, and small amounts you can afford to lose.

The `tr0` pit uses P2TR inputs and outputs. Configure a P2TR wallet and a matching
Taproot offer preference; separate CoinJoin pits never mix. Fidelity bonds are
shared across pits, which publicly links makers using the same bond.

A private channel buyout spends a Taproot channel in a later CoinJoin. The
channel peer is paid over Lightning after confirmation, while a presigned
timelocked split protects recovery. It requires the authorized
[patched LND backend](../lnd/README.md). Follow the
[buyout configuration and recovery guide](../jmswap/README.md).

Keep journals, original LND state, and recovery artifacts. Missing state never
authorizes a new payment, a migration, or deletion. Do not delete intent markers
or retry a payment manually to bypass an uncertain outcome.
