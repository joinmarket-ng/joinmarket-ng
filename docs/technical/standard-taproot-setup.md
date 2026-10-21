# Standard Taproot Setup (Design)

Status: experimental design for the unreleased Taproot pit, channel ring, and
credential market features. It replaces hand-wired configuration with one
standard setup and integrates those features into the existing maker and taker
flows.

## Goals and Non-Goals

The standard setup trades configurability for a single, private-by-default
path:

- One switch enables the Taproot stack: the Taproot pit, managed Lightning
  nodes, channel rings, and the credential market. Without it nothing changes.
- One managed LND node per mixdepth. JoinMarket creates, configures, runs, and
  rotates these nodes; the user never configures endpoints or copies files.
- Market purchases and sales run inside the taker and maker. A taker buys a
  PoDLE commitment during its own CoinJoin; a maker sells PoDLE commitments
  and rents idle fidelity bonds while it runs.
- Every interactive step has a scriptable flag equivalent.

Non-goals: channel buyouts stay an advanced manual feature (they need the
patched LND build in `lnd/`). The file-oriented `jm-market` CLI stays as an
advanced and offline tool, for example for bonds whose owner key is kept
offline. P2WPKH wallets get no Lightning or market integration.

## One Switch

`wallet.address_type = "p2tr"` is the switch. The maker `offer_type` and the
taker `preferred_offer_type` follow the wallet family when they are not set
explicitly (`tr0reloffer` for a P2TR wallet, `sw0reloffer` otherwise); an
explicit mismatch is still rejected at startup. The Taproot stack stays off by
default because a P2TR round cannot include P2WPKH makers.

`[lightning] mode` selects how the nodes are run:

| Mode | Meaning |
| --- | --- |
| `off` | No Lightning integration (default) |
| `managed` | JoinMarket runs one LND per mixdepth, the standard setup |
| `manual` | The operator runs LND (for example with Docker) and lists endpoints as in the channel ring configuration |

Managed and manual modes require a P2TR wallet. In managed mode the channel
ring sections need no node configuration: every mixdepth maps to its managed
node and ring participation is enabled.

## Managed Lightning Nodes

### Node Identity and Rotation

Each node's LND wallet is initialized (`InitWallet` with an extended master
key) from key material derived from the JoinMarket seed, including its BIP39
passphrase. Running two LND instances with the same keys can lose channel
funds, so an identity must never be allocated twice. A purely deterministic
scheme cannot guarantee that: the same seed may be restored on another
machine, or a data directory may be lost while its nodes still have
channels. Every identity is therefore scoped to a random epoch:

```text
label path = "JoinMarket NG" / "lightning node v1" / NETWORK / EPOCH
             / "mixdepth M" / "generation G"
entropy    = SLIP-21 key at that label path (32 bytes)
LND root   = BIP32 master key from entropy, serialized as xprv or tprv
```

`EPOCH` is 16 random hex characters chosen when nodes are first created on an
installation and stored in the node registry before any LND wallet exists.
Mixdepth and generation are bounded (`M < 10`, `G < 1000`); exhaustion is an
error. SLIP-21 labels separate this use from every other derivation of the
seed, including the wallet's market keys and other BIP85 tools. The seed and
the epoch together reproduce every node identity and its onchain wallet; the
epoch is part of every backup and shown by `jm-lightning status`.

Rotation gives a mixdepth a fresh identity, the next generation in the current
epoch:

1. The old node is marked `retiring` and receives no new rings, payments, or
   invoices from then on.
2. The replacement is provisioned and becomes `active`.
3. For each channel of the retiring node, a close intent with a fresh delivery
   address of the same mixdepth is recorded, then a cooperative close is
   requested. Peers that stay unreachable are force closed after a timeout.
   After an uncertain result the channel state is reconciled before retrying.
4. When no channel, pending close, or HTLC resolution remains, a sweep intent
   is recorded and the node's onchain balance is sent to a fresh address of
   the same mixdepth.
5. The node is `retired`. Its entry stays as a tombstone, so neither the
   generation nor its data directory is ever reused.

Old and new nodes run side by side during this; they are distinct identities.

### Process Supervision

`jm-lightning run` is the long-running service. It starts the pinned
upstream `lnd` binary once per active node with a generated configuration:

- Tor only, with a per-node onion service for the ring endpoint.
- Bitcoin Core backend in RPC polling mode, so no ZMQ setup is needed.
- Private production Taproot channels and SCID aliases (the ring
  requirements), no public announcements, no REST listener.
- Ports chosen once at creation and stored in the node registry.

It unlocks each wallet through the unlock RPC with a password derived from the
seed under its own SLIP-21 label (`"lightning unlock v1"`, scoped like the node
key), so no LND password is stored on disk or passed on a command line. The
supervisor keeps only these scoped derivation capabilities, not the seed. It
restarts crashed nodes with backoff, keeps the static channel backups current,
and stops the nodes cleanly. `jm-lightning install-lnd` downloads the upstream release and
checks it against SHA-256 digests pinned in the source. Operators who prefer
Docker use manual mode instead.

### Wallet Reserve

Anchor channels need confirmed wallet coins to fee-bump a force close. LND
only enforces its reserve for public channels, so ring channels open without
one, but a node without coins may be unable to confirm a force close in a fee
spike. Each managed node therefore keeps a small confirmed reserve within a
configured range. JoinMarket never asks LND to fund anything from its own
wallet (ring channels are funded inside the CoinJoin), so the reserve is only
spent by LND's sweeper.

The reserve of mixdepth N's node is only ever funded from mixdepth N's
privacy cluster, and one transaction funds at most one node. An output is in
range when it brings a node below its minimum to a balance between the
minimum and the maximum:

- Equal output in range: when a CoinJoin from mixdepth N-1 to the next
  mixdepth has an amount in range for node N, its equal output pays to node
  N. This is preferred, since the output is mixed.
- Change in range: otherwise, when a CoinJoin's ordinary change for mixdepth
  N is in range for node N, that change pays to node N. Ring and buyout
  change is never redirected, and a taker sweep has none.
- Explicit: `jm-wallet fund-reserve N` sends from mixdepth N to a fresh
  Taproot address of node N, and does nothing when the node already holds its
  minimum. Setup suggests this per mixdepth, never for several mixdepths at
  once.

Unconfirmed coins count toward the minimum, so a funding transaction still in
the mempool is not repeated, and no funding raises a node above the maximum.

Only managed nodes are funded by CoinJoin outputs; manual mode leaves the
operator's node wallets alone. Each node's reserve is read before a CoinJoin
(a taker) or every few minutes (a maker, so `!ioauth` never waits on LND), and
an unreachable node or an old reading funds nothing. A node address is handed
out once and the node is then left alone for a while, since the CoinJoin
paying it may still be negotiating or may fail; a later reading picks either
the received coins or a fresh address. A taker redirects only an equal output
it resolved itself for an internal destination, never a tumbler's planned
destination, and a maker only with a Taproot offer.

The reserve is advisory: a ring CoinJoin still runs below the minimum and
the status output and logs warn.

### Upgrade and Restore Safety

Nodes are only created or started by explicit commands, never because state
is missing:

- `jm-lightning init` creates a fresh epoch and one node per mixdepth. It
  refuses to run when a registry or node data already exists. A fresh epoch
  can never collide with an identity used elsewhere, so init is safe on any
  wallet; it does not recover nodes this seed ran before.
- `jm-lightning run` only starts nodes listed in the registry. A node whose
  data directory is missing is not started and is reported as needing
  recovery; it is never reinitialized as a repair. A malformed or unsupported
  registry is kept untouched and refused.
- Each node entry is written in a `provisioning` state before `InitWallet`.
  Because the epoch is local to this installation, an interrupted creation
  resumes with the same identity: without an LND wallet it initializes again,
  with one it unlocks and records the identity.

Restore is disaster recovery and requires explicit authorization that the
original installation is stopped (`jm-lightning restore BACKUP
--original-stopped`). It must be explicit because recovery necessarily
reconnects to the peers as the old identities. Every unresolved node in the
backup (active, retiring, or provisioning) is initialized with its channel
backup; its peers force close with the latest state and the node sweeps the
result to its mixdepth and retires. Normal operation continues on new nodes
under a fresh epoch. Recovery depends on the peers still having their channel
state, and covers settled balances, not in-flight payments. Copying a node data
directory to another machine defeats these checks and is unsupported.

## Payments

All market payments go through the nodes and stay inside one mixdepth's
privacy cluster:

- A taker pays from the node of the mixdepth its CoinJoin spends.
- A maker pays (bond rent) and invoices (sales) from the node of its current
  offer mixdepth.

There is no automatic fallback to another mixdepth's node. Ring channels are
private and their peers usually have no public channels, so invoices carry
explicit route hints for the node's incoming ring channels. When the node
cannot pay (no route or liquidity), an interactive run shows the invoice as
text and a terminal QR code for an external wallet; a non-interactive run
skips the purchase.

## Taker: Automatic PoDLE Purchase

`[taker] external_podle_mode` gains `prefer`: buy a commitment from the
market, and fall back to a wallet commitment when nothing affordable is
available. With the Taproot switch on and Lightning enabled the default is
`prefer`; otherwise it remains `disabled`. `only` keeps its meaning (never
reveal a wallet UTXO). `jm-taker coinjoin --external-podle` overrides the
setting per run.

The purchase happens before maker selection, so the seller's makers are
excluded from the round:

1. Discover listings; keep sellers whose price is within the cap.
2. Request a quote stating the minimum backing value the round needs (the
   CoinJoin amount times `taker_utxo_amtpercent`, or the total input value
   for a sweep). The seller only quotes inventory that meets it.
3. Pay from the source mixdepth's node, wait for the delivery, verify and
   import it.

The default price cap is twice the fee of a one-input, one-output P2TR
transaction at the 3-block fee estimate: the cost of creating a fresh UTXO,
which also yields three new wallet commitments. The cap includes routing
fees. A run buys at most a few commitments, because an allocated commitment is
consumed even if the round later fails.

Each purchase is recorded before every step that can leave money in flight,
so a commitment paid for before a crash is collected on the next run instead
of being bought twice, and no second seller is paid while an earlier payment
is unresolved. A purchased commitment stays bound to the mixdepth whose node
paid for it: rounds from other mixdepths skip it.

## Maker: Selling and Renting

The seller runs inside the maker process, using the wallet's own market keys
and its owned bond as seller authority. Setup enables it; it is never enabled
by an upgrade. It requires the wallet ledger activation already described in
the [credential market reference](../credential-market.md).

### PoDLE Sales From the Offer Mixdepth

The maker sells commitments for confirmed UTXOs of its current offer
mixdepth, each with its three NUMS indices. A maker never uses its own
commitments, and takers do not judge maker inputs by remaining PoDLE indices,
so selling them costs the maker nothing beyond revealing those UTXOs to the
buyer. The current offer mixdepth is the one offers are built from (the
largest offer balance). Its coins are stocked once they have five
confirmations (the default maker `taker_utxo_age`) and pass the same backing
check a buyer runs on delivery; bonds and frozen coins are never sold.

Only coins of the current offer mixdepth are quoted. Each invoice comes from
the node of the sold coin's own mixdepth, derived from the stored reservation
rather than from the current offer mixdepth, so neither a mixdepth switch nor
a restart can show a buyer two mixdepths together. Stock of other mixdepths
stays in the ledger but is not quoted. Earlier invoices settle or cancel on
the node that holds them, and a lookup that cannot reach every node is
treated as unknown rather than as absent. Authorization is per retarget
period, so the seller restarts when the period changes.

A sold UTXO is not locked. Before each attempt to settle a held payment, the
seller checks the coin's backing again and refunds the payment if the coin
was spent, keeping a prepared commitment consumed; an unreachable backend
delays the decision until the HTLC deadline refunds it. A reservation whose
coin was spent is still canceled on that coin's node.
A spend after settlement makes the buyer's commitment fail, which is rare
and cheap. The default price is half the taker's default cap and follows the
fee rate;
`podle_price_sats` fixes it. A missing package, bond, ledger, or node pauses
sales with one warning and is retried; it never stops the maker.

### Idle Bond Rental

The maker advertises its highest-value bond and rents its other owned bonds
for one retarget period at a time. A leased bond is never advertised by its
owner during the lease, which would be certificate equivocation. The default
price is a rate on the locked amount for the remaining duration of the lease.

Before choosing a bond, and every 30 seconds while making, the maker reads
the market store of its data directory (read-only). A bond is blocked from
the reservation of a hold for its lease until the invoice is confirmed
canceled or the lease ends (its certificate verifies through block
`(period + 1) * 2016`), and a bond stocked for rent in this period or the
next is blocked too, even before a sale. A blocked bond is never selected; a
configured bond that is blocked falls back to the best other one. When the
lease history is unknown (an unreadable store, a ledger intent without its
store, or a wallet ledger that is activating, rebinding or needs recovery),
or the last reading is older than three minutes, offers carry no bond proof
until it can be read again. This guard is defense in depth: a reading can be
obsolete as soon as it is taken, so the rental supervisor itself never stocks
or leases the advertised bond.

### Renting a Bond

A maker without a usable bond can rent one at startup and before its lease
expires, within a price cap. The renter certificate key is derived from the
seed and never written to disk, unlike the file-oriented import.

## Setup and Status

`jm-ng setup` is the interactive onboarding: backend and Tor checks, wallet
creation or import, the Taproot switch, managed nodes (install LND, create
the nodes, suggest reserve funding), role, and market options with
recommended defaults. Every prompt has a flag, and `--yes` accepts the
defaults for scripted use. The installer offers to run it after a fresh
installation.

`jm-wallet info` shows a Lightning section per mixdepth: node identity and
generation, sync state, reserve against its range, channel count, and
outbound and inbound liquidity. `jm-lightning status` shows the same data,
also as JSON.

## Persisted State

| State | Location | Absent means |
| --- | --- | --- |
| Node registry: epoch, nodes with state, identity, ports, close and sweep intents | `lightning/<fingerprint>/registry.json` | No managed nodes; nothing is created or started |
| LND data per node | `lightning/<fingerprint>/nodes/<epoch>-<mixdepth>-<generation>/` | That node is not started and needs recovery |
| Backups: registry copy and static channel backups | `lightning/<fingerprint>/backup/` | Nothing to restore from |
| Ring node bindings | existing `node-bindings.json` | Managed init writes them for its own new nodes |
| Market ledger, store, leases | existing wallet market ledger | No sales or leases |
| Rented bond with derived certificate key | existing bond registry, new entry kind | No rental; older entries keep raw keys |

A data directory without a market store is treated as one that never sold a
lease, so existing makers keep advertising their bond. This is a
compatibility policy, not a proof: a store deleted together with its wallet
ledger intent (or a standalone store, which has none) is not detected, and
leases sold from another data directory never are.

The registry is versioned; an unknown version or a malformed file is refused,
never treated as absent. Its writes are atomic. Backups live outside the LND
data directories so losing one does not lose the other.

An installation upgrading from 0.42.1 has none of this state and a P2WPKH
wallet, so nothing changes for it: no node is provisioned and nothing is
scanned. Development installations with manual channel ring endpoints keep
working, since explicit node configuration is manual mode. Managed mode never
adopts manual nodes into its ownership.

## Open Risks

- Routing between private ring nodes depends on the ring topology; a buyer
  whose node has no path to the seller falls back as described above.
- The reserve range is a judgment call: 25,000 sat covers one force close
  without HTLCs up to roughly 60 sat/vB, and several simultaneous force
  closes need more.
- Running one LND per mixdepth multiplies memory use and Tor circuits.
