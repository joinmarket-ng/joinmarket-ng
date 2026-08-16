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
  external transaction. That validates the funding primitive, not an external-peer
  privacy deployment. Makers do not coordinate directly; the taker pairs CoinJoin
  participants, and each maker channelizes *its own* change with its selected partner
  for the deterministically computed amount. The maker hands the taker the channel's
  funding address as an ordinary change address; the taker builds and broadcasts the
  CoinJoin as usual, and the maker supplies the resulting outpoint after validation
  (before signing for LND, after signing for the LDK Server backend).
  The exact-change-amount bookkeeping in the live CoinJoin is implemented and
  validated end to end (a maker opens a real channel from its CoinJoin change in
  a real maker<->taker round).

The taker also contains three legacy experimental combined constructions. `pool` emits
one shared MuSig2 output for a maker pair, while `knapsack` emits overlapping,
grid-quantized MuSig2 pieces. These are on-chain 2-of-2 primitives, not Lightning
channels by themselves; production use requires a complete cooperative lifecycle
for those outputs. `lnd_ring` creates real private SIMPLE_TAPROOT LND channels:
the taker plans co-funded ring edges, asks each edge funder to open a PSBT-shim
channel, and merges both makers' change contributions at the returned funding
address. The request includes a remote push equal to the partner's contribution.
This is required because LND derives initial balances from the open request, not
from ownership of the CoinJoin inputs that pay the funding output.

None of these constructions is the channel-input HTLC escrow protocol. They fund
new channels from CoinJoin outputs and do not cooperatively spend an existing
channel funding outpoint into an escrow parent with a pre-signed timeout child.
They are disabled unless both the original feature switch and
`allow_legacy_channel_change` are set. This second switch is an acknowledgment for
controlled research deployments, not a production-safety claim.

### Channel-input escrow backend boundary

The confirmation-gated construction is an explicit, disabled-by-default maker mode. Its
composite runtime provides the full lifecycle declared by
`jmswap.ChannelInputEscrowCapability`:

1. Cooperatively spend an existing channel funding output into the exact escrow parent.
2. Fully sign and validate timeout recovery before releasing the parent signature.
3. Monitor exact-parent confirmations, conflicts, and reorgs before Lightning settlement.
4. Force a single payment part and exclude the consumed channel from routing.
5. Persist recovery state and rebroadcast or fee-bump recovery transactions after restart.

The repository now includes a pinned LND `v0.20.1-beta` patch and image under `lnd/`.
Both owners of a consumed channel must run this build. Its `ChannelEscrowRPC` quiesces
and durably locks a `SIMPLE_TAPROOT` channel, binds the complete BIP341 prevout set and
exact parent, exchanges MuSig2 nonces and partial signatures, rejects normal close and
abandon paths while locked, persists the final cooperative transaction, and publishes
or rebroadcasts only that parent. Build it with
`docker build -t jm-lnd-channel-escrow:local lnd`.

Configure `lnd_macaroon_path` with a macaroon authorized for the standard LND
operations used by channel funding. Configure `lnd_escrow_macaroon_path` with the
patched node's generated `channelescrow.macaroon`, normally found beside the network's
other macaroons. Keeping these credentials separate limits custom escrow RPC access;
the standard macaroon remains the compatibility fallback when the scoped path is unset.

`LndBackend` exposes these operations as the narrow `freeze_channel_input`,
`prepare_channel_input_parent`, `begin_channel_input_signing`,
`sign_channel_input_parent`, and `finalize_channel_input_parent` API. It deliberately
declares an empty full-lifecycle capability set by itself. The maker composes it with the
timeout signer, Bitcoin Core chain adapter, single-part LND payment adapter, authenticated
peer runtime, durable stores, and lifecycle monitor before declaring the complete capability
set. A partial adapter therefore cannot advertise production support.
`jmswap.bitcoin_escrow` provides the isolated cryptographic layer for constructing and
independently verifying the role-ordered P2TR escrow tree, fully signed timeout child,
and immediate RBF success sweep. Bitcoin Core regtest acceptance covers both spend paths.
External channel funding APIs still do not satisfy this boundary merely by returning a
new channel funding address.

`jmswap.ChannelInputEscrowBackend` is the typed adapter contract for the concrete
endpoint implementation. Its strict request and evidence models bind the quiescent channel
snapshot, exact parent, fully signed timeout child, parent authorization, fresh chain
observation, single-part route, payment result, and success or timeout broadcast to one
session. `require_channel_input_escrow_backend` requires both every declared capability
and every protocol method, so partial adapters fail closed.

Endpoint coordination uses authenticated LND custom messages on odd type `53357`.
Canonical envelopes bind the immutable ring authorization, finalized channel point, both
LND node identities, per-direction sequence, and previous message ID. The endpoint journal
persists exact outbound bytes before sending, rejects conflicting replay, records inbound
messages before dispatch, and redelivers incomplete work after restart. Version 1 requires
the escrow session ID to equal an authorized maker-ring edge ID and therefore does not
permit arbitrary external peers.

`ChannelInputEscrowCoordinator` owns write-before-side-effect ordering for a complete
backend. It durably records timeout evidence before parent authorization, records the exact
confirmation-gated payment request before sending, monitors pending payments through parent
reorganizations, and requires descendant rebroadcast evidence before restoring a reorged
state. Startup reconciliation treats the local store as authoritative and rejects unknown,
conflicting, duplicate, or ahead-of-local backend records before resuming side effects.

`ChannelInputEscrowDeployment` continuously advances each durable session in an independent
monitor task. It observes exact-parent depth and conflicts, excludes the consumed channel
before payment, persists immediate payment failure, broadcasts timeout recovery only after
strict maturity, fee-bumps an unconfirmed success spend at most once per block before its
deadline, and monitors confirmed descendants through the configured reorganization margin.
Shallow reorganizations suspend new economic actions until the parent regains the selected
depth. A failed monitor session retries without blocking other sessions, while new escrow
admission remains disabled until monitor health recovers. Conflict and cross-layer loss states
produce operational error logs.

`ChannelInputEscrowRecord` enforces the authorization and recovery ordering, retains the
prior state across a parent reorganization, and rejects persisted states that lack their
required evidence. Confirmation and terminal states require exact-parent, descendant, or
conflict observations at their required confirmation depths. Payment attempts retain the fresh
confirmation-gated request, late peer settlement remains recoverable but is classified as a
protocol violation, and success fee replacements plus reorganization rebroadcast attempts
remain durable. `ChannelInputEscrowStore` serializes each session with a file lock, rejects
stale or colliding revisions, writes records atomically, syncs both the file and directory,
uses collision-resistant hashed filenames, and rejects session-file substitution. These
records contain private invoice, balance, timeout, and preimage-related artifacts and must
remain inside the maker endpoint; they are not taker messages or aggregate-log data.

The evidence fields such as `invoice_verified`, `signature_verified`, and
`exact_transaction_verified` are adapter attestations. The typed boundary validates their
presence, cross-artifact identities, economic equations, confirmation gate, route exclusion,
CLTV inequalities, and broadcast budgets. It does not itself decode BOLT 11 invoices,
verify Bitcoin signatures or scripts, inspect a node's route, or broadcast transactions.
A production adapter must perform those operations before setting the evidence fields.

### Channel-as-change is maker-to-maker only

A channel funding output reveals its outpoint to the co-signing counterparty, so
opening a channel-as-change to an **external** peer would hand the maker's
funding<->wallet link to an outside observer: it is no privacy improvement and is
never used. A valid channel-as-change always joins two CoinJoin participants; a
maker that cannot be paired falls back to a plain change output.

### Knapsack pairing (taker-arranged, beats the fixed pool)

A rigid pool of two makers' *exact* change defeats the per-output change-match,
but a whole-round subset-sum solver that hypothesises group-funded shared outputs
can uniquely invert a fixed pool whenever the pool size relates simply to the
participant count (the `coinjoin-simulator` `maker-swap-provider` study measures
the fixed pool attributed ~1.0 at 4 makers and ~0.94 at 6, an even/odd sawtooth).
The robust construction is **knapsack** pairing (`taker.knapsack.plan_knapsack`):
the taker pools *every* change-bearing maker and splits each change into
grid-quantized pieces that share one round-wide grid value, emitted as
*overlapping* maker-to-maker 2-of-2 outputs. Because many makers emit an
equal-valued grid piece, a grid piece of one maker is interchangeable with a grid
piece of another without changing any output value or any maker's total, so the
whole-round partition is combinatorially ambiguous (low-density subset-sum
hardness); the study drives the maximal attacker's attribution to ~0 at *every*
set size, where the fixed pool fails, at the cost of ~2 channel opens per maker
(two pieces, the minimum that leaves no single output equal to the residual). An
**allowance** of makers may open no channel at all: their plain change is diffused
into the same grid pool, so on chain they are indistinguishable from the channel
makers (their plain pieces still chain across slots, the honest limit). The
planner is implemented and unit-tested (`taker/tests/test_knapsack.py`); like the
combined variant its piece *amounts* feed `build_coinjoin_tx`'s per-maker
`change_outputs` (via `taker.knapsack.plan_to_change_outputs`, which also enforces
the maker-to-maker rule with `assert_maker_to_maker`) and the maker-to-maker 2-of-2
*addresses* come from the channel layer.

#### Runtime protocol wiring

The legacy runtime is feature-flagged with taker `combine_channel_as_change`,
`allow_legacy_channel_change`, and
`combine_mode = "pool" | "knapsack" | "lnd_ring"`. Non-participating makers and
failed plans retain ordinary change. The real-channel `lnd_ring` flow is:

1. **Advertise.** A capable maker advertises `ring_edge_auth_v1` during the encrypted
   session handshake and sends a per-round funding key and its configured Lightning
   node in encrypted `!ioauth` fields. The feature is advertised only when the maker
   has both durable authorization storage and a channel-funding backend.
2. **Plan.** After `!ioauth`, the taker computes exact maker change and plans equal or
   near-equal co-funded ring channels above `ring_min_channel_sats`.
3. **Authorize both endpoints.** The taker creates a random edge ID and canonical record
   binding the PoDLE round, taker and maker nicks, all three NaCl session keys, both LND
   node IDs, capacity, contributions, and expiry. It sends the same encrypted record to
   both makers in `chanauth`; both persist and acknowledge its hash before any open.
4. **Open and allocate.** The encrypted `chanreq` contains only the edge ID and
   authorization hash. The funder loads the peer, capacity, and exact remote push from
   durable state, starts a private LND PSBT-shim open with `no_publish`, binds the returned
   P2TR funding script, and returns the edge ID, hash, and address in `chanaddr`.
5. **Build and verify.** The transaction builder merges both contributions into one
   output per address. Each maker requires exactly its acknowledged edge set and verifies
   every authorization hash, local contribution, singleton script, full capacity, and
   computed funding outpoint before signing.
6. **Pin before signing.** LND verifies the complete unsigned CoinJoin PSBT with
   `skip_finalize` before the maker releases signatures. If opening, address collection,
   allocation, or PSBT verification fails, the taker sends authenticated `chancancel`
   messages for every requested edge. The maker cancels the exact pending shim and either
   accepts a verified plain-change fallback or refuses the round.

### On-chain-only takers: sweep under homogeneous fees

Once makers are this hard to cluster, a pure on-chain taker (no Lightning, never a
maker) regains anonymity by **sweeping** its denomination coin
mixdepth-to-mixdepth (no change, no co-spend) under homogeneous fees: its only
cross-round edge is the ambiguous reused-equal-output, so a matching attacker
links none of its tumble hops. Leaving change re-introduces the taker's own change
peel chain and CIOH, and distinct per-maker fees expose it by fee-fingerprint
elimination even when sweeping. The taker should therefore sweep and prefer a
uniform-fee market (the `maker-swap-provider` study Section 4.10).

### Why Boltz v2 (not papa-swap)

A more on-chain-efficient design exists (supertestnet's *papa-swap*: one funding
transaction plus pre-signed spends, roughly halving footprint and latency). We
chose the Boltz v2 HTLC instead for the first implementation because: it is a
**deployed standard** (Boltz, Electrum, many wallets), so a maker can clear
swaps against the existing market; its claim/refund are **signed at spend time**,
so fees track the market and can be RBF-bumped (papa-swap pre-signs spends at
fixed fees, which is risky on the timelock-bounded refund path); and its
**MuSig2 2-of-2 cooperative close** reuses exactly the same Taproot/MuSig2
machinery as the channel-as-change funding output, rather than papa-swap's
separate pre-signed tx0-tx3 state machine (with its two race conditions and a
two-week recovery window). papa-swap's single-transaction efficiency is an
attractive future optimization, but Boltz v2 is the better v1 trade-off.

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

Existing-channel escrow additionally requires `[maker.channel_input_escrow] enabled = true`,
a descriptor-wallet Bitcoin Core backend, all four LND connection and macaroon paths under
`[maker.swap_provider]`, and the patched LND build on both channel endpoints. The
`monitor_poll_interval_seconds` setting controls lifecycle polling. This mode is mutually
exclusive with the legacy `channel_change` and `knapsack_capable` constructions.

## Privacy fidelity (what is actually achieved)

A privacy audit of the code against the design surfaced four points; three are
now addressed and one remains a deployment discipline:

- **Cooperative key-path swap claim (addressed for both directions).** Both swap
  directions now settle with a 2-of-2 MuSig2 key-path spend (an ordinary P2TR
  signature, no preimage or tapscript on-chain), falling back to the script path
  only if the counterparty does not cooperate. For swap-out the client claims and
  the provider co-signs (`coopclaim`); for swap-in the provider receives the coins
  and chooses the destination, the client co-signs and broadcasts on the
  provider's behalf (`coopclaim_in`) after the provider has paid the invoice. The
  swap-out path is validated live; both fall back trustlessly to the script path.
- **Split / combined / knapsack channel-as-change (addressed for the per-output
  change-match; whole-round hardened by knapsack).** A maker can emit its change as
  several funding outputs (`build_coinjoin_tx`'s `change_outputs`) so no single
  output equals its exact residual, and two makers can pool their change into one
  shared output; both defeat the per-output change-match. A fixed pool is still
  invertible by the whole-round (CoinJoin-Sudoku) solver at set-divisible sizes, so
  the robust construction is the taker-arranged **knapsack** pairing
  (`taker.knapsack.plan_knapsack`): grid-quantized overlapping maker-to-maker pieces
  that keep the whole-round partition combinatorially ambiguous at every set size,
  with an allowance of non-channel makers hidden in the same pool. Channel-as-change
  is maker-to-maker only (an external-peer channel is never used).
- **On-chain fidelity-bond verification (addressed).** Providers advertise a real
  timelocked bond and clients verify it on-chain (`jmswap.verify_provider_bond` /
  `select_bonded_providers`): a sybil that merely sets `bond_value` is filtered
  out.
- **The self-2-of-2 channel-change recovery sweep re-links the funds** (it spends
  the funding output back into the wallet). The privacy holds only if the output
  is used as a *real* channel and cooperatively closed (a decorrelated balance);
  the sweep is a recovery tool, not the privacy path.

What *is* achieved as implemented: the cross-slot peel-chain break (the deployable
~100-effective-wallets win), per-swap key isolation, a swap-in coin whose only
on-chain parent is the funder's transaction (a 1-in/1-out claim, no co-spend),
combined funding that defeats the single-owner partition, and cooperative
sweeps/closes that are themselves ordinary P2TR key-path spends.

## Limitations

The break is structural and on-chain, not perfection. A force close re-exposes
the channel/HTLC script on chain; the swap counterparty and network-level
observers can correlate the Lightning legs; and timing can associate the swap,
fill and settlement. The robust protections are a decentralised many-provider
market, cooperative closes, and batching/decoys (the `ln-subgraph` direction).
Bitcoin's transparency makes a perfect maker-anonymity guarantee impossible; the
goal is to raise the analyst's cost substantially with deployable steps.
