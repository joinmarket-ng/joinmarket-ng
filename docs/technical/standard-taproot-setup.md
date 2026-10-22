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

- Lightning peer traffic through Tor, with a per-node onion ring endpoint.
- Bitcoin Core backend in RPC polling mode, so no ZMQ setup is needed.
- Private production Taproot channels and SCID aliases (the ring
  requirements), no public announcements, no REST listener.
- Ports chosen once at creation and stored in the node registry.

It unlocks each wallet through the unlock RPC with a password derived from the
seed under its own SLIP-21 label (`"lightning unlock v1"`, scoped like the node
key), so no LND password is stored on disk or passed on a command line. The
supervisor keeps only these scoped derivation capabilities, not the seed. It
restarts crashed nodes with backoff, keeps the static channel backups current,
and stops the nodes cleanly. Once owned shutdown begins, caller cancellation
is deferred until the child is stopped and reaped. This is process cleanup,
not proof that payments or channel obligations have resolved.
`jm-lightning install-lnd` downloads the upstream release and
checks it against SHA-256 digests pinned in the source. Operators who prefer
Docker use manual mode instead.

LND's Bitcoin Core RPC is direct plaintext HTTP, not Tor or HTTPS. Use loopback
or an explicitly secured tunnel; `tor.active` protects peer traffic, not this
RPC connection. Managed launches reject HTTPS, onion RPC hostnames, inline URL
credentials, and URL routing components that LND cannot preserve. Supply RPC
credentials separately or use the cookie path. This does not restrict the
ordinary wallet backend's supported transports or change explicit remote HTTP
operator choices.

Rejected but parseable Core transport settings do not prevent authenticated
reads of existing managed nodes using recorded TLS and macaroon credentials.
Other invalid settings can still prevent construction of that read context.
Launch refusal happens before stopping a stale process, modifying node data,
or spawning a replacement. It does not retroactively secure an already-running
node's RPC connection; correct that node's backend configuration deliberately.

The supervisor discovers explicitly registered runnable nodes while it runs,
so a replacement can start beside a retiring node. Registry changes stop an
owned process only for an explicit `retired` entry for the same wallet and
node identity. Missing,
unreadable, or unsupported registry state pauses discovery but leaves existing
LND processes and their backup loops running. This does not replace the maker
or taker settlement monitors, which must also remain running until their
outcomes are known. An established node missing its wallet still requires
explicit recovery and a supervisor restart; polling never reinitializes it.

Managed launches enforce sticky inbound-channel quarantine for retiring and
recovering identities. The private per-node `quarantine.json` binds the request
to the full wallet identity, network, and node generation. Later launches use
LND's `maxpendingchannels=0`, even if registry state rolls back to `active`.
Unknown marker data is preserved and also restricts inbound opens; it is not
treated as a valid owned request. Existing active installations without a
marker retain their affirmative active policy.

The request and process launch share a lock, but recording a request does not
retroactively quarantine an already running LND. Effective quarantine requires
a separately verified restart before any channel closure. This launch policy
does not close channels, sweep coins, or establish retirement completion.

Automatic maker-owned supervision is not implemented. Do not attach the
current LND supervisor to the maker CLI's exit stack: unwinding that stack stops
application monitors, while exiting the supervisor stops its children. Neither
action proves that accepted work has completed. An independent LND process alone
also does not preserve the application's settlement supervision.

Any future integrated owner must retain the complete accepted population:

| Accepted work | Required application ownership |
| --- | --- |
| Validated fills, including unbound sessions, detached handlers, and signed rounds awaiting push | Original maker session, generation, keys, and input-lock owner |
| Ring preparation, signing, open channels, and unresolved restart records | Original participant journal, node binding, and ring reconciliation |
| Prepared buyout sessions, including unbound entries and completed sessions still watched for reorgs | Original buyout runtime, journal, endpoint, and settlement monitor |
| Reserve claims handed to a CoinJoin | The accepting session's transaction ownership; reserve refresh and claim timers are not completion receipts |
| Market reservations, published quotes, held payments, and prepared deliveries | Original seller ledger, signing authority, invoice owner, and settlement monitor |
| Outgoing bond-lease requests, payment attempts, and unresolved delivery | Original purchaser records, payer identity, and payment review; stopping the buyer does not leave an independent review task |

Before such an owner may stop LND, new admission must remain excluded across
restart, all accepted work must remain covered by its original live monitors,
and affirmative evidence must clear that entire population. Missing, partial,
unreadable, or ambiguous evidence means retaining ownership, not stopping.
Transient admission gates, empty journals, elapsed timers, and task completion
do not establish these conditions. External shared-wallet and taker actors also
need coverage; the maker CLI's resource stack is not a complete actor inventory.
No complete cross-actor clearance receipt currently exists, so neither stricter
startup readiness nor the admission APIs authorize automatic financial
maintenance. Existing installations retain their explicit supervisor commands,
registry formats, and startup behavior.

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
- A maker invoices PoDLE sales from its current offer mixdepth's node.
- Bond leases are paid and invoiced from mixdepth 0's node, where bonds live.

There is no automatic fallback to another mixdepth's node. Ring peers usually
have no public channels, so managed maker-market invoices add explicit hints for validated,
locally recorded incoming ring channels. LND's automatic private hints omit
unadvertised peers. Invoice holders can see the selected incoming peer and
channel, but the channel is not publicly announced. Missing, older, or invalid
ring provenance grants no additional hint, and existing invoices keep their
original contents and owner. When the node cannot pay (no route or liquidity),
an interactive run shows the invoice as text and a terminal QR code for an
external wallet; a non-interactive run skips the purchase.

Managed payments resolve the current `active` node of the same mixdepth for
each fresh attempt and verify its public key and network before paying. A
retiring, recovering, missing, or unreadable source does not authorize new
payments and does not trigger the external-invoice fallback. That fallback
remains available after an authorized active node reports a definite routing
failure. Manual-node behavior is unchanged.

Outstanding tracked payments are looked up by their saved original payer
public key and payment hash, even when there is no active replacement. The
lookup never crosses mixdepths or substitutes the replacement's history.
Previously validated endpoint snapshots remain usable if metadata becomes
unreadable during that process; after restart an authoritative binding is
required. Missing history stays unknown, never failed. Retain the original
LND history and application settlement monitors until outcomes are terminal.
These admission checks alone do not prove retirement quiescence.

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

With `[maker] rent_bonds`, the maker advertises its highest-value bond and
rents its other owned bonds for one retarget period at a time. A leased bond
is never advertised by its owner during the lease, which would be certificate
equivocation. The seller runtime of the PoDLE sales serves each rented bond
under its own seller authority and nick, so listings never link two bonds,
and invoices leases from mixdepth 0's node, where bonds live. The price is an
annual rate on the locked amount (`bond_rent_rate`) for the rest of the
period, repriced as the period runs down. New bonds are admitted only in the
first half of a period, so a maker starting late waits for the next one, and
only bonds whose key is in the wallet and that stay locked well past the lease
are admitted. The margin is a full period at the target block interval, so a
timelock can only end before its lease if the lease's blocks average more
than twice that interval. A bond served or stocked in a period stays served by
its own authority until the period ends, even if it would no longer be
admitted, so a held payment for its lease is always settled or refunded.

Existing wallets retain conservative advertisement initialization: unknown
history initialized in period P blocks new rental admission through P+1.
Uninterrupted fresh guided activation instead publishes complete empty history,
bound to the full wallet identity and Bitcoin network, before activating its
ledger. Only that witnessed generation can avoid the initial delay for idle
bonds; imports, restarts, and old publication records grant no such authority.
Proof admission and typed rental stocking share one cross-process lock through
their respective durable writes. Legacy raw inventory insertion retains its
compatibility contract, not this stronger admission guarantee.

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

The opposite direction needs a record, because a restart can select another
bond. Before its first proof, and on every refresh, the maker durably records
the advertised bond as advertised through the next period (a proof can be
sent just after a period boundary, before the maker sees the new block). The
record is created when first needed, and every bond counts as advertised
through the period after that one, which covers earlier runs and versions that
kept no record, with the same boundary margin. A bond is admitted for a period
only after every period it may have been advertised in. If the record cannot
be written, or is corrupt, offers carry no bond proof and no bond is newly
admitted; deleting a corrupt record is safe and only pauses rental for two
periods at most. The record is kept even
with rental off, so turning rental on later is safe. Running an older version
on the same data directory is not: it neither reads leases nor writes the
record.

### Renting a Bond

With `[maker] lease_bond`, a maker without a usable bond (none owned, all
leased out, or only an expired certificate) leases one while it runs. A lease
lasts the rest of the current retarget period, because a certificate is sold
for one period only, so a new one is bought after each boundary and offers
carry no bond proof in between. Owned bonds are chosen only at startup, so a
lease never replaces one, and an expired certificate no longer stops a maker
with leasing on. Leases are paid from mixdepth 0's node, where bonds live.

Listings are tried from the highest price within `lease_bond_max_sats`
(routing fees included) down. Listings carry no bond value, so a quote is
accepted only when its price is within `lease_bond_max_rate` a year of the
quoted bond's locked amount, checked on chain before paying. Selection is by
price: under the rate cap a higher price only raises the smallest bond it can
buy, it does not guarantee the largest one.

Each purchase is recorded under `market/bond-purchases/` with fresh random
buyer and renter certificate keys before the request is sent, so a paid lease
is collected after a crash. At most one lease is paid for per period:

- A payment stays unresolved until mixdepth 0's node reports it succeeded or
  failed, and no other seller is paid until then, even across a period
  boundary: a held payment can settle until its HTLC times out, and an
  expired certificate does not change that. Delivery (valid or invalid) is
  independent of settlement. The original paying node's identity and payment
  hash are saved before paying, and checked during recovery. A changed node,
  missing history, or an empty status response never proves payment failure.
  Retain authoritative LND history and keep settlement monitors running;
  incomplete outcomes intentionally remain fail-closed.
- A lease paid for in the current period blocks another purchase in that
  period, whether it was delivered usable or not. A seller whose paid lease
  proved unusable is not bought from again.
- A delivered lease keeps its certificate key in its record through its
  period (and until payment resolves), so a maker that could not switch it in
  gets it again instead of buying a second one.
- An unreadable or unknown record blocks every purchase until it is reviewed,
  since it proves neither that nothing was paid nor that a payment resolved.

Deriving the renter key from the seed would not help recovery, since the
owner's certificate signature cannot be recovered from the seed either. A
delivered lease is imported into the bond registry as an external entry. A
lease of the same bond for a later period renews the certificate only of an
entry whose current certificate was leased here, as the purchase records
prove; a cold owner's own certificate or a `jm-market import` entry is never
replaced.

## Setup and Status

`jm-ng setup --configure-only` provides the configuration stage without the
shell menu or whiptail. It preserves unrelated settings, validates coupled
choices before one atomic replacement, and supports a no-write preview.
Taproot remains explicit opt-in. Every prompt has a flag, and `--yes` accepts
configuration defaults without asserting wallet freshness or creating nodes.

The wallet stage requires an explicit existing/generate/import choice.
Noninteractive use selects `--wallet-action` and supplies new-wallet passwords
through `--wallet-password-file`; imports use `--import-mnemonic-file`.
`--yes` alone never creates a wallet. New wallets require encryption and never
overwrite existing files or metadata. Newly supplied passwords and seed words
are not stored in configuration. Switching wallets is refused until any configured
mnemonic password or BIP39 passphrase is deliberately resolved.

The versioned setup journal distinguishes intent, prepared encrypted bytes,
and completed publication. `--resume-wallet-publication` never regenerates a
missing wallet or infers that an existing wallet is unused. Resume preserves
changed recovery metadata; missing import metadata needs explicit
`--confirm-import-recovery`. Durable setup publication currently supports
POSIX filesystems. If configuration selection fails afterward, retain the
saved wallet and journal rather than generating again.

Generation does not authorize disclosing seed words into redirected output,
logs, or screencasts. Seed display requires a private-backup confirmation on a
terminal or explicit `--show-generated-seed`; stop recording first. Without
disclosure, the encrypted wallet is preserved and seed backup remains pending.
Existing-wallet selection does not rewrite recovery metadata or register an
identity.

`--role` selects wallet, maker, taker, or both for guided options and command
guidance, not service startup. Maker sales, idle owned-bond rental, and paid
bond leasing are independent choices. Paid leasing remains opt-in and uses the
configured cost/rate caps. Taker commitment sourcing uses the existing policy
for the selected pit. Existing preferences are preserved unless selected for
editing; role selection never turns on Taproot implicitly.

In the full flow, new buying/leasing permissions and caps wait for verified
wallet identity rather than applying to whichever wallet was previously
configured. Seller-enabling settings additionally wait for ledger readiness.

Seller-enabling settings in the full flow wait for a verified ledger binding.
`--activate-market-ledger` is separate consent, not supplied by `--yes` or a
seller-role choice. Guided activation is limited to uninterrupted generation in
the current invocation, with no existing `market` or `cmtdata` entry, even an
empty directory or dangling alias. Generation is necessary but not sufficient:
`--market-history-excluded-since-generation` affirms that all wallet consumers
and shared-history writers, including restarts, have remained excluded since
generation and will stay excluded through activation. This is an operator
precondition, not a technical admission barrier. It is explained before keys
are generated and must not be confused with node-initialization quiescence.

BIP39 identity is resolved before local activation and reused for node setup.
Only the public prompt preference is recorded, never the passphrase. Fresh
activation precedes seed disclosure and backend/node activity. Refusal, failure,
or disclosure ends that invocation's automatic activation eligibility; private
seed backup remains available. Preserve any partial ledger and use explicit
history inspection/recovery, never generate a replacement as rollback.

Fresh activation publishes bound advertisement history without replacement
before activating a ledger that independently requires that history. Missing,
corrupt, mismatched, or v1 replacement history blocks new proof and rental
admission, without recreating or downgrading it. Preserve partial artifacts.
Coupled recovery and directory rebinding for this new mode are not yet supported
and are explicitly refused. Existing valid legacy ledgers and v1 history keep
their previous behavior on upgrade, without automatic promotion or migration.
The previous ledger decoder rejects the new internal mode; this is not a claim
that arbitrary older software can safely operate on it.

Required-history failure closes admission without stopping an existing seller's
settlement monitor. After a restart, the maker can instead start a closed,
reconciliation-only runtime from the verified ledger and original configured
invoice endpoints, without a current owned bond or replacement authorization.
Unverified original signing identities remain unknown. This mode starts no
relay: a durably prepared package is not proof that its buyer received it.
Retain original node histories and do not treat monitoring as retirement clearance.

Imported, existing, and resumed wallets receive already-ready binding checks
only. Missing, disabled, standalone, activating, recovery-required, or unavailable
state supplies no fresh authority and receives no implicit activation. A ready
binding is not clearance of old payment obligations. Configuration-only and
preview modes perform no ledger or wallet-secret access; configured flags are
not a claim that history is ready. Live seller startup revalidates its binding
and eligibility independently.

After wallet selection, `--create-managed-nodes` requests fresh managed node
identities, not restoration. `--install-lnd` separately authorizes a missing
pinned release to be downloaded through Tor. Existing verified binaries are
reused. The stage checks the configured Core genesis and sync state, plus Tor
control authentication and bootstrap, before allocating nodes. These checks
do not import JoinMarket descriptors or trigger wallet recovery.

`--applications-quiesced` affirms that consumers of these nodes and automatic
restarts are excluded during initialization. Setup does not stop or drain other
applications; retain monitors for existing obligations. Noninteractive runs
must also choose `--bip39-passphrase-file` or `--no-bip39-passphrase` explicitly
unless the same selected wallet already has a configured passphrase. Neither
assertion is supplied by `--yes`.

Do not concurrently edit configuration, mnemonic contents, identity selection,
or data-directory aliases during initialization. Snapshot checks detect common
conflicts; they are not transactional exclusion from other writers.

Guided creation refuses existing registries, partial provisioning, backups,
bindings, and orphaned node data. Preserve them for explicit inspection and
recovery rather than creating replacements. Individual nodes and bindings are
published durably before the group completes; a partial result is not rolled
back. Keep consumers excluded until interrupted initialization is recovered.

Successful initialization stops its new nodes and prints a separately invoked
foreground supervisor command. It does not start a temporary supervisor, maker,
taker, or market. Persistent LND supervision does not replace application
settlement monitors. Automatic maker-owned supervision remains deferred until
admission can stop while recovery monitors continue running.

The seller runtime's admission-only boundary can stop new listing/quote
callbacks while keeping the same ledger ownership, keys, invoice clients, and
settlement task alive. It waits for already-admitted callbacks without
canceling them; canceling that wait leaves admission closed. Transport caches
may still replay existing quotes, and existing quotes remain payable. Callback
completion does not resolve uncertain external RPC effects. This transient
boundary is not integrated into automatic maintenance and never authorizes
node shutdown or history removal. Restart retains the existing startup behavior
and creates a new admission-open runtime, not durable maintenance exclusion.

The maker sales supervisor can also latch producer closure so refresh, retry,
or period changes cannot reopen admission or replace the retained seller.
Closure first closes the owned seller's gate, then quiesces producer work;
canceling the wait does not reopen either gate, but requires retrying the
producer boundary. Explicit terminal stop remains separate. Teardown already
started before closure, or a failing startup's own cleanup, cannot be undone
and does not prove monitoring coverage. This is not ring admission control or
automatic node maintenance, and adds no durable exclusion across restarts.
Independently canceled worker threads may still finish existing writes; this
boundary is not a filesystem-write barrier.

Maker fill-admission closure rejects fresh fills before commitment allocation
and waits for fill handlers accepted before its first suspension. It does not
withdraw offers or stop sessions. Accepted sessions can still authenticate,
consume reserves, and create their first ring record afterward, using their
original resources. The entire accepted session remains outstanding, including
one with no durable ring record yet. An empty handler counter or record list
therefore grants no financial quiescence or node-retirement authority. This
gate is also transient and remains closed if its waiter is canceled.

The separate `close_offer_admission()` operation closes both fill and offer
publication gates before waiting, stages withdrawals for every retained identity,
then drains accepted fill handlers and attempts connected-directory delivery.
Offline withdrawals remain queued and reconnects send cancellations, not new
offers. In-flight public or private messages may still land; a returned call is
not proof that every peer removed its cached offer. Accepted sessions keep their
original routing and resources. Market sellers require their own admission
closure, and neither boundary supplies node-retirement clearance.
Retained noncurrent identities may lack a reconnect worker; queued cancellation
alone therefore does not guarantee eventual delivery.

Role guidance prints separate actor commands and mixdepth-specific reserve
funding instructions; it never submits a reserve transaction or starts actors.
After a fresh software installation, the installer offers guided setup with a
default-no prompt. `--run-setup` attempts it only with an interactive terminal;
`--skip-setup` suppresses the offer. Installer `--yes` and piped input never
authorize wallet, ledger, or node effects. Noninteractive runs print the exact
installed command instead. Updates and any pre-existing virtual-environment
path, including a file or dangling alias, never receive automatic setup.
Software freshness says nothing about existing wallet or history state.

Setup runs only after installation succeeds, using that installation's
entrypoint and explicit data/config paths. Older releases without onboarding
support are not invoked speculatively. Setup failure or cancellation preserves
the completed installation and any partial wallet/node state; installer success
does not mean setup succeeded. Inspect partial state before retrying. Stopped
initialized nodes are not reported as a running Lightning service.

`jm-wallet info` shows a Lightning section per mixdepth: node identity and
generation, sync state, reserve against its range, channel count, and
outbound and inbound liquidity. `jm-lightning status` shows the same data,
also as JSON.

## Persisted State

Managed-node paths below are relative to
`<data_dir>/lightning/<network>/<wallet_identity[:16]>/`. The registry retains
the full wallet identity; the path prefix is not sufficient ownership proof.

| State | Location | Absent means |
| --- | --- | --- |
| Node registry: epoch, nodes with state, identity, ports, close and sweep intents | `registry.json` | No launch authority; nothing is created or started |
| LND data per node | `nodes/<epoch>-<mixdepth>-<generation>/` | Established nodes need recovery; only affirmative provisioning may resume creation |
| Sticky quarantine intent | `nodes/<epoch>-<mixdepth>-<generation>/quarantine.json` | No quarantine request; the validated registry lifecycle still controls launch authority |
| Backups: registry copy and static channel backups | `backup/` | No local backup evidence; not proof that recovery is unnecessary |
| Ring node bindings | existing `node-bindings.json` | Managed init writes them for its own new nodes |
| Market ledger, store, leases | existing wallet market ledger | No local sales evidence; absence alone cannot clear retirement obligations |
| Advertised bonds per period | `market/advertised-bonds.json` | Legacy history initializes conservatively; required bound history blocks new issuance when absent, never recreates fresh history |
| Bond lease purchases, with their keys until their period ends or as evidence of an unusable paid lease | `<data_dir>/market/bond-purchases/` | No local purchase evidence; not proof of no prior payment or lease provenance |
| Imported leased bond and random renter certificate key | wallet-specific fidelity bond registry | No locally recorded credential; the owner's signature and renter key cannot be recovered from the seed alone |

A legacy data directory without a market store is treated as one that never sold a
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
