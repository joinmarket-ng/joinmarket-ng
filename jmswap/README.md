# jmswap

A standalone, bidirectional **Taproot Lightning swap** provider/client library.

`jmswap` lets a JoinMarket maker act as a bidirectional Lightning swap provider:
it clears Lightning swaps in both directions from its existing wallet, which
breaks the cross-slot maker clustering described in
`papers/maker-swap-provider.md` while earning swap fees. The library is
deliberately self-contained (it depends only on `jmcore` for the
Bitcoin/Taproot primitives) so it can be extracted and reused outside
JoinMarket.

## What it does

- **Swap-in** (provider receives on-chain, pays Lightning): the client funds an
  on-chain HTLC and receives a Lightning payment. Mechanically a *submarine*
  swap.
- **Swap-out** (provider pays on-chain, receives Lightning): the client pays a
  Lightning hold invoice and claims an on-chain HTLC. Mechanically a *reverse*
  swap.
- **Whole-coin third role**: accept a client's whole UTXO at base fee (no client
  change), the simplest on-chain shape that severs the peel chain.
- **Channel-as-change primitives**: emit CoinJoin change as a simple-taproot
  channel-funding output (MuSig2 2-of-2, key-path cooperative close), with
  sum-preserving split/combine of change (`jmswap.channel`). The full Lightning
  dual-funding negotiation is the documented open item.

Each swap locks funds in a single **P2TR** output:

- the internal key is the **MuSig2** aggregate of `[provider, client]`, so a
  cooperative settlement is an ordinary BIP340 key-path spend, indistinguishable
  from any other Taproot spend;
- the script tree carries a preimage **claim** leaf and a timelock **refund**
  leaf as trustless fallbacks.

The leaf scripts are byte-for-byte compatible with `boltz-core` v2.

## Architecture

```
SwapServer (provider)        SwapClient (user)
        \                       /
         \   Nostr (adverts +  /
          \  NIP-04 RPC DMs)  /
           SwapNostrNode -- relay
        |            |            |
 LightningBackend  ChainBackend  SwapWallet
 (LDK Server)      (Core / API)  (jmwallet)
```

- `SwapTree` / `jmswap.spend` - the Taproot HTLC and its claim / refund /
  cooperative spends (`jmcore.taproot` + `jmcore.musig2`).
- `LightningBackend` - normal + hold invoices, pay, settle, cancel.
  `LdkServerBackend` speaks LDK Server's gRPC API; `FakeLightningBackend`
  drives in-process tests with a real hold-invoice settle/cancel flow.
- `ChainBackend` - watch an address, broadcast, find a spending tx.
- `SwapWallet` - per-swap keys, fund a swap address, destination scripts.
- `SwapNostrNode` - advertise terms (kind 31000), serve / call swap RPCs
  (NIP-04 DMs, kind 25582). A `LoopbackRelay` runs the whole protocol in-process.
- `SwapStore` / `jmswap.recovery` - crash-safe contract persistence and timelock
  refund recovery so an in-flight swap survives a restart.

Inside JoinMarket, the maker runs the provider as a background task when
`[maker.swap_provider]` is enabled, using `maker.swap_adapter` to bind the swap
backends to the maker's wallet and blockchain backend (`jm-maker`).

## Status and validation

The cryptographic core is validated against the official **BIP341** (taproot
scriptPubKey + key-path sighash) and **BIP327** (MuSig2 key aggregation) test
vectors, the swap leaves against `boltz-core` byte layouts, and every spend path
by signing-then-verifying against the exact committed sighash. The full swap
protocol (both directions, the whole-coin variant and the complete Nostr path)
is exercised end-to-end in-process over faithful fake backends with **real
BIP341 Taproot transactions**.

Live validation against real LDK Server nodes and a real relay is wired into the
`swap` docker-compose profile (`nostr-relay`, `ldk-provider`, `ldk-client`) and
is the next integration step.

## Tests

```bash
pytest jmswap
```
