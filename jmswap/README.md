# jmswap

`jmswap` provides the LND backend used by JoinMarket's co-funded channel-ring
protocol. It opens and validates private, externally funded final-Taproot
channels through LND's PSBT funding shim.

The runtime package contains only:

- `jmswap.channel_ring`, which initializes the backend and exposes validated
  participant limits for the ring protocol.
- `jmswap.channel_input`, which initializes the optional patched-LND escrow
  backend and validates established-channel candidates.
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

The stock-LND path supports the co-funded channel-ring lifecycle and guarded
retirement before any funding transaction signatures are created or broadcast.
The separate taker policy layer owns established-channel input coordination.

## Optional ChannelEscrowRPC Transport

The channel-input transport is experimental. A complete live three-maker
channel-input CoinJoin, including broadcast, confirmation, durable recovery,
and taker restart, is exercised by `scripts/run-channel-input-e2e.sh` against
patched LND nodes. Makers credit the advertised fee through the exactly
authorized channel settlement instead of the ordinary change output, and keep
the completed ordinary session available for its bound channel-signing
exchange. Deployment still depends on a reproducible patched-LND build and a
published MuSig-enabled `python-bitcointx` release; do not deploy
`channel_input_v1` from unverified local artifacts.

For a patched LND v0.21.1 deployment, `LndBackend` also exposes a narrow,
typed ChannelEscrowRPC transport boundary for a final-Taproot channel input.
Pass `escrow_macaroon_hex` to the constructor, or `escrow_macaroon_path` to
`from_paths`, to enable it. Escrow RPCs use a separate authenticated gRPC
channel and refuse to run without that dedicated credential; ordinary
co-funded channel-ring use never initializes it.

Set `LND_EXTERNAL_IMAGE` for the two-node external-LND Compose profile or
`RING_E2E_LND_IMAGE` for the ring E2E profile to run a compatible patched
image. Both variables retain stock LND `v0.21.1-beta` as their default.

Run the opt-in mixed-input exact-parent restart-recovery test with a patched image:

```bash
LND_EXTERNAL_IMAGE=lnd-channelescrow:v0.21.1-beta-local \
  scripts/run-ring-e2e.sh lnd-escrow
```

The boundary can freeze a channel, bind an exact parent and its prevouts,
manage MuSig2 signing attempts, recover finalization from durable evidence,
query durable status, and cancel before signing. It validates all returned
outpoints, transaction IDs, final witnesses, and stage evidence. It does not
own coordination, timeout or HTLC policy, or transaction broadcast.
`taker.channel_input_parent.ChannelInputParentCoordinator` combines this
transport with prevalidated ordinary native-SegWit witnesses, atomically
persists the exact final transaction, and broadcasts only after every input is
finalized.

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

## Cryptography Development

Schnorr signing, Taproot tweaks, and sighashes use `python-bitcointx`; there is
no `coincurve` dependency. The `jmcore.musig2` adapter requires the new native
`bitcointx.core.musig` API in the local fork and a libsecp256k1 build with the
MuSig module. That API is not in the currently pinned fork release, v2.1.1.
Use the updated local fork when running the MuSig unit and escrow integration
tests. A published, hash-pinned fork artifact and corresponding CI dependency
update are required before those tests are reproducible from release locks.

LND performs production channel signing itself. The Python MuSig adapter is not
a replacement for LND's durable escrow state or its nonce lifecycle.
