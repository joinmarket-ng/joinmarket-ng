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

## Node-Local Channel Maintenance

Run one maintenance service for each LND identity that owns private co-funded
ring channels:

```bash
jm-channel-maintenance --role maker run
jm-channel-maintenance --role maker status
```

The command defaults to the maker role; use `--role taker` only for the
separately configured taker LND identity. `--config PATH` follows the normal
`JOINMARKET_CONFIG_FILE` override, and `--data-dir PATH` overrides the resolved
JoinMarket data directory. `run` is the default command, so the first command
can omit it. The service stays in the foreground and must have a single owner
for an LND identity.

Set both `enabled = true` and `private_coordination = true` in the selected
`[maker.channel_ring]` or `[taker.channel_ring]` section. Set
`maintenance_directory` to one stable, private directory for that same LND
identity. Every maker, taker, and maintenance process controlling that identity
must use the same directory. Do not share it between identities or start more
than one maintenance service against it. The regular ring producer records
affirmative enrollment for new private-ring channels.

Upgrading preserves legacy v1 coordination unless private coordination is
explicitly enabled. Retained v1 rounds remain recoverable; enabling v2 does not
enroll their channels. An interrupted enrollment handoff records an operator-review
error in the ring record and is not automatically retried if enrollment state is
missing. There is currently no automatic repair command for this condition.

On restart, an unknown or unresolved payment remains blocked for reconciliation
of that original payment. Do not delete maintenance state to bypass it. Existing channels
that were not affirmatively enrolled by private-ring production are untouched.
Zero routing fees do not make a payment risk-free and do not guarantee privacy.
Current discovery requires an eligible three-node triangle, so a lone four-node
ring has no eligible triangle to maintain.

Run the isolated service-level regtest with
`./scripts/run_parallel_tests.sh --suite ring-e2e-maintenance` from the repository
root. It creates a private triangle plus an unenrolled parallel return channel,
checks selected-channel settlement and zero fees, and exercises advisory pauses
over real LND peer messages. Payments use one explicit `SendToRouteV2` attempt.
A hold invoice releases its preimage only for a single HTLC on the selected
incoming channel with the exact amount. A payment arriving through a parallel
channel is canceled. This controls settlement; it does not prevent ordinary
Lightning forwarding or temporary HTLCs on other channels.

Maker and taker freezes now share a durable admission lock with maintenance.
Admission checks both selected local funding outpoints. Local spend guards
survive restart and only proven cancellation releases them. After upgrading,
older enrollments without an admission marker remain paused for new payments
and fee initialization. Existing dispatched payments are still reconciled.
After reconciling both local spend registries and checking the selected channels
are idle, stop maintenance and explicitly enable their admission:

```bash
jm-channel-maintenance --role maker reconcile-admission \
  --channel-point "$CHANNEL_POINT" --confirm-idle
```

Repeat `--channel-point` for additional fully enrolled channels. This command
does not enroll channels, reset fees, or remove local spend guards. Unknown fee
initialization states remain blocked.

If backend history cannot establish a historical payment's outcome, independently
verify its exact terminal outcome and that no HTLC remains in flight. With the
maintenance service stopped, record that operator resolution using its exact hash:

```bash
jm-channel-maintenance --role maker resolve-payment \
  --payment-hash "$PAYMENT_HASH" --terminal-status failed --confirm-no-inflight
```

Use `succeeded` for a verified successful payment. Expiry, canceled RPCs, and
missing history alone do not establish failure. The command records an audit
marker and schedules the next attempt in the future.

The experimental custom-message type 65501 also supports `spend_pause_v1` through
explicit capability negotiation. Pauses bind the Lightning peer, chain genesis,
funding outpoint, request ID, and expiry. Each channel permits at most 30 seconds
of remote pause per rolling minute. Restart expires remote pauses and quarantines
new requests for a full elapsed minute; replay and budget records remain durable.
The service makes one best-effort announcement per observed local guard and
releases its accepted lease after that guard disappears. A freeze can happen
before this scheduling pass, so announcement or acknowledgment is never required
for spending. These hints only suspend future maintenance initiation. They do
not clear local guards, authorize signing, guarantee drained HTLCs, or replace
the existing nick-bound channel-input protocol.

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
