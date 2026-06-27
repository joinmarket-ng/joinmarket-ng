# Bidirectional Lightning swap provider

A JoinMarket maker's coins are linked across its CoinJoin slots by structural
edges that no fee policy hides: the change peel chain, the common-input co-spend,
and the reused equal-output forward edge. Being a **bidirectional Lightning swap
provider** is a deployable, self-financing partial fix: a swap client requests
*their* amount, so clearing swaps from the maker's existing wallet displaces the
prior-slot coins that would otherwise chain, fragmenting the maker's slots into
many effective wallets while earning swap fees. The full analysis is in
`papers/maker-swap-provider.md`; this page documents the implementation
(`jmswap`).

## Protocol

Each swap locks funds in a single Taproot (P2TR) output. The internal key is the
**MuSig2** aggregate of the provider and client keys, so a cooperative
settlement is an ordinary BIP340 key-path spend, indistinguishable from any
other P2TR spend (the privacy-preserving happy path). The output's script tree
adds two trustless fallbacks: a preimage **claim** leaf and an absolute-timelock
**refund** leaf. The leaf scripts are byte-for-byte compatible with Boltz v2.

The provider is always the swap "server" and never pays a swap fee:

- **Swap-in** (provider receives on-chain, pays Lightning) is a submarine swap:
  the client funds the HTLC and is paid over Lightning; the provider claims the
  HTLC with the preimage learned from settling that payment.
- **Swap-out** (provider pays on-chain, receives Lightning) is a reverse swap:
  the provider issues a hold invoice, funds the HTLC to the client's claim key
  once the invoice is paid, watches the client's on-chain claim for the revealed
  preimage, then settles the hold invoice.
- **Whole-coin third role**: the provider absorbs the client's whole UTXO at the
  base fee with no client change, the simplest on-chain shape that severs the
  peel chain (it concentrates linkage onto the provider's own wallet, which it
  manages separately).
- **Channel-as-change** (experimental, off by default): the maker can emit its
  CoinJoin change as a Taproot channel-funding output (a simple-taproot 2-of-2),
  so the change has no chaining successor in the wallet. Today both funding keys
  are derived from the maker's seed, so the output is tracked on-chain and
  cooperatively swept back with ``jm-maker swap-channel-sweep`` (an ordinary
  P2TR key-path spend, validated against bitcoind). Opening a *real Lightning
  channel* whose funding output is the CoinJoin output is also supported: a
  forked LDK Server (built from the patches in ``ldk_server/patches/``) exposes
  externally-funded channel opens via LDK's
  ``unsafe_manual_funding_transaction_generated``, driven by
  ``jmswap.request_external_channel_funding`` /
  ``complete_external_channel_funding`` (the maker uses the returned funding
  address as its CoinJoin change output, then supplies the resulting outpoint).
  This is validated live: a channel opens with its funding output supplied by an
  external transaction. The open design item that remains is the maker-to-maker
  negotiation of *who* opens the channel and the in-CoinJoin coordination of the
  channel amount.

Discovery and signalling run over **Nostr**: the provider advertises its terms
as an addressable event and answers swap RPCs sent as NIP-04 encrypted DMs; the
client discovers providers and calls those RPCs. The client always recomputes
the swap address from the parameters it agreed to before funding, so the
provider cannot pin a swap to a script the client did not accept.

## Operating it

Set `[maker.swap_provider]` in the config (see `config.toml.template`) to enable
the role, choose directions, fee terms, and the Nostr relays, and point it at an
**LDK Server** Lightning node for the invoices and payments. The `swap`
docker-compose profile brings up a Nostr relay and two LDK Server nodes
(`ldk-provider`, `ldk-client`) on regtest for end-to-end testing. The library
itself (`jmswap`) is standalone and depends only on `jmcore`.

## Limitations

The break is structural and on-chain, not perfection. A force close re-exposes
the channel/HTLC script on chain; the swap counterparty and network-level
observers can correlate the Lightning legs; and timing can associate the swap,
fill and settlement. The robust protections are a decentralised many-provider
market, cooperative closes, and batching/decoys (the `ln-subgraph` direction).
Bitcoin's transparency makes a perfect maker-anonymity guarantee impossible; the
goal is to raise the analyst's cost substantially with deployable steps.
