# jmswap

`jmswap` provides the LND backend used by JoinMarket's co-funded channel-ring
protocol. It opens and validates private, externally funded final-Taproot
channels through LND's PSBT funding shim.

The runtime package contains only:

- `jmswap.channel_ring`, which initializes the backend and exposes validated
  participant limits for the ring protocol.
- `jmswap.lnd`, which validates LND capabilities, negotiates externally funded
  channels, validates funding PSBTs, observes channel readiness, and performs
  guarded pre-broadcast retirement.
- Generated LND gRPC stubs pinned to LND `v0.21.1-beta`.

It does not include an Electrum swap client or provider, public swap-provider
service, Nostr transport, or atomic-swap runtime.

## External LND Channels

The backend targets stock LND `v0.21.1-beta` or newer and requires the
production final-Taproot `TAPROOT` commitment type (wire value 7), not the
staging `SIMPLE_TAPROOT` type. `ExternalChannelRequest` and
`FundingNegotiation` open a private `PsbtShim(no_publish=true)` channel with a
caller-supplied pending channel ID and exact negotiated policy. The fundee's
`ChannelAcceptor` validates that same contract.

`verify_external_funding` checks the expected P2TR output, amount, TXID,
outpoint, and every native SegWit witness UTXO before calling
`PsbtVerify(skip_finalize=true)`. Readiness requires matching
`PendingChannelObservation` values from both endpoints.

The confirmation-gated channel-input escrow and reuse of live channels are not
implemented. The current backend supports only the co-funded channel-ring
lifecycle and guarded retirement before any funding transaction signatures are
created or broadcast.

After `PsbtVerify(skip_finalize=true)`, a funding shim cannot provide a
reliable rollback. `retire_verified_external_channel` permits LND's destructive
`AbandonChannel` operation only with exact locally authorized funding, proof
that the unsigned transaction is absent from mempool and chain, and proof that
no input signature was created or sent, broadcast occurred, or final funding
transaction was observed.

Regenerate the pinned LND gRPC stubs with:

```bash
python jmswap/scripts/gen_lnd_protos.py
```

Run package tests with:

```bash
pytest jmswap
```
