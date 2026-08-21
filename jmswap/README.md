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

The end-to-end confirmation-gated channel-input coordinator is not implemented.
The stock-LND path supports the co-funded channel-ring lifecycle and guarded
retirement before any funding transaction signatures are created or broadcast.

## Optional ChannelEscrowRPC Transport

For a patched LND v0.21.1 deployment, `LndBackend` also exposes a narrow,
typed ChannelEscrowRPC transport boundary for a final-Taproot channel input.
Pass `escrow_macaroon_hex` to the constructor, or `escrow_macaroon_path` to
`from_paths`, to enable it. Escrow RPCs use a separate authenticated gRPC
channel and refuse to run without that dedicated credential; ordinary
co-funded channel-ring use never initializes it.

Set `LND_EXTERNAL_IMAGE` for the two-node external-LND Compose profile or
`RING_E2E_LND_IMAGE` for the ring E2E profile to run a compatible patched
image. Both variables retain stock LND `v0.21.1-beta` as their default.

Run the opt-in exact-parent restart-recovery test with a patched image:

```bash
LND_EXTERNAL_IMAGE=lnd-channelescrow:v0.21.1-beta-local \
  scripts/run-ring-e2e.sh lnd-escrow
```

The boundary can freeze a channel, bind an exact parent and its prevouts,
manage MuSig2 signing attempts, recover finalization from durable evidence,
query durable status, and cancel before signing. It validates all returned
outpoints, transaction IDs, final witnesses, and stage evidence. It does not
provide an escrow coordinator, established-channel-input lifecycle, timeout or
HTLC policy, or transaction broadcast. The external coordinator broadcasts
only after all CoinJoin inputs are finalized.

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
