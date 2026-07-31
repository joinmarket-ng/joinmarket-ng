# jmswap

`jmswap` is a standalone transport-neutral implementation of Electrum's
`taproot-v1` Lightning swap protocol. It provides strict Pydantic v2 wire DTOs,
canonical Taproot contracts, libsecp256k1-compatible BIP327 MuSig2 cooperation,
atomic recovery state, and trustless claim/refund fallback construction.

The library exposes four host boundaries:

- `LightningBackend` creates and validates main and miner-fee invoices.
- `ChainBackend` resolves funding outputs, reports height, and broadcasts transactions.
- `SwapWallet` derives one seed-backed key per swap and prepares exactly validated funding.
- `RpcAdapter` carries one of the five canonical Electrum methods and its payload.

`DirectOnionAdapter` accepts an authenticated encrypted request callback without
adding another envelope. `ElectrumNostrAdapter` implements kind `30315` discovery
and kind `25582` NIP-04 RPC using Electrum's top-level `method` and `reply_to`
formats.

## Maker swap provider

The maker can run a production `taproot-v1` provider by enabling
`[maker.swap_provider]`. This requires the Bitcoin Core descriptor wallet backend,
a `p2tr` maker wallet, an HTTPS LND REST endpoint with a pinned TLS certificate and
macaroon, at least one Nostr relay, and the configured Tor SOCKS proxy. Relay
websockets are opened only through SOCKS, with fresh credentials when stream
isolation is enabled. Direct RPC is accepted only on an established maker onion
connection and uses the same signed, recipient-bound NIP-04 event as Nostr RPC.

Provider contract keys use a dedicated BIP86 seed branch and a random persisted
non-hardened index per swap. Reverse funding preparation is serialized, and every
selected wallet input is reserved in the cross-process wallet lock store under the
swap ID before the exact signed transaction is persisted. Recovery validates the
derived key, transaction ID, input reservation, output index, amount, and script
before rebroadcast. Owner-scoped reservations remain until a terminal swap
transition, so unrelated CoinJoin or swap cleanup cannot release them.

Admission is globally bounded across transport identities. Forward requests must
register their invoice before the shorter registration deadline, and only a bounded
number of newest terminal records are retained. These limits prevent disposable Nostr
keys from creating unbounded background polling work or persistence growth.

Forward swaps pay only after exact confirmed contract funding, persist the payment
intent before calling LND, and persist the provider claim before broadcast. Reverse
swaps require the miner-fee invoice settled and the main hold invoice accepted before
funding. The hold invoice is settled only after an observed spend of the exact
contract outpoint reveals the matching preimage in that input's claim witness, or
after the exact cooperative transaction persisted during the single-use MuSig2
exchange is observed. Script-path claim and timeout refund remain available when
cooperation fails.

Run the isolated live provider test with:

The restart handshake uses a bind-mounted state directory and therefore requires
a local Docker daemon exposed through a Unix socket context.

```bash
rm -rf tmp/swap-provider-e2e
mkdir -p tmp/swap-provider-e2e junit
export RING_E2E_DATA_DIR="$PWD/tmp/swap-provider-e2e"
export SWAP_PROVIDER_E2E_PROJECT=jm-swap-provider-e2e
export SWAP_PROVIDER_E2E=true
docker compose -p "$SWAP_PROVIDER_E2E_PROJECT" \
  -f jmswap/docker-compose.ring-e2e.yml \
  --profile ring-e2e --profile swap-provider-e2e \
  build swap-provider-test-runner
docker compose -p "$SWAP_PROVIDER_E2E_PROJECT" \
  -f jmswap/docker-compose.ring-e2e.yml \
  --profile ring-e2e --profile swap-provider-e2e up -d
./scripts/run-swap-provider-e2e.sh
docker compose -p "$SWAP_PROVIDER_E2E_PROJECT" \
  -f jmswap/docker-compose.ring-e2e.yml \
  --profile ring-e2e --profile swap-provider-e2e down -v
```

## External LND channels

`jmswap.lnd` is an independent foundation for externally funded, co-funded
Lightning channels. It targets stock LND `v0.21.1-beta` or newer and selects the
production final-Taproot `TAPROOT` commitment type (wire value 7), never the
staging `SIMPLE_TAPROOT` type.

The typed API uses `ExternalChannelRequest` and `FundingNegotiation` to start a
private `PsbtShim(no_publish=true)` open with a caller-supplied pending channel
ID, exact push, reserves, CSV delays, and confirmation depth.
`InboundChannelExpectation` drives the strict fundee
`ChannelAcceptor` gate. `verify_external_funding` validates the negotiated P2TR
output, amount, TXID, outpoint, and every SegWit v0/v1 witness UTXO before
calling `PsbtVerify(skip_finalize=true)`. Readiness requires matching
`PendingChannelObservation` values from both opener and fundee; an acceptor
success alone is not readiness.

LND reports the initiator's commitment fee separately in `PendingChannels`.
For final Taproot, LND v0.21.1 also holds 660 sats outside both reported
balances and `commit_fee` for the two 330-sat anchors. The accounting invariant
is therefore:

```text
initiator local balance + persisted commit_fee + commitment overhead = initiator contribution
fundee pushed balance = fundee contribution
local balance + remote balance + commit_fee + commitment overhead = channel capacity
```

`COFUNDED_CHANNEL_RING_V1_CAPABLE` gates advertising the ring backend. It is true
only when either safe verified retirement has passed the required stock-LND live
test, or retained-state reconciliation and strict anti-grief limits are both
implemented. The current LND adapter uses the first strategy. Maker/taker
runtime capability negotiation admits a ring round only when the taker and its
entire selected maker set advertise this backend capability.

After `PsbtVerify(skip_finalize=true)`, a funding shim can no longer provide a
reliable rollback. `retire_verified_external_channel` instead permits LND's
destructive `AbandonChannel` operation only for the exact locally authorized
`VerifiedFunding` and channel point. The authorization must repeat the unsigned
transaction TXID, prove that exact TXID absent from both mempool and confirmed
chain, and confirm that no local CoinJoin input signature was created or sent,
no broadcast occurred, and no final transaction was observed. The backend
requires an exact point in `PendingChannels`, calls `AbandonChannel` with
`pending_funding_shim_only=false` and `i_know_what_i_am_doing=true`, and then
checks that every `PendingChannels` category no longer contains the point.
Already-absent results are idempotent only with the same chain-absence proof.

This is safe only in that narrow pre-signing, pre-broadcast state. Abandoning an
active, signed, broadcast, mempool-observed, chain-observed, or uncertain funding
transaction can discard LND's channel state while funds remain encumbered and
can cause loss. `PendingChannels` provides the channel point and channel policy
state used here; it does not provide the PSBT pending-channel ID. The fundee is
therefore bound to the exact point only after this backend accepted the pending
ID and observed the expected point locally.

Regenerate the pinned `v0.21.1-beta` gRPC stubs with:

```bash
python jmswap/scripts/gen_lnd_protos.py
```

Use `--refresh` only to re-download the pinned proto; the script verifies its
SHA256 before writing or generating. The gated live spike runs against two
unmodified LND nodes and Bitcoin Core. It retires from both endpoints, proves
the funding transaction absent, and repeats a fresh verified open to prove the
pending slots are reusable:

```bash
docker compose -f jmswap/docker-compose.lnd-external.yml \
  --profile lnd_external up -d
LND_EXTERNAL_E2E=1 pytest -c pytest.ini \
  tests/e2e/test_lnd_external_channel_e2e.py \
  -m lnd_external --fail-on-skip --no-cov
docker compose -f jmswap/docker-compose.lnd-external.yml \
  --profile lnd_external down -v
```

Run the package tests with:

```bash
pytest jmswap
```
