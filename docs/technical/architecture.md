# Architecture

## System Overview

<figure markdown="span">
  ![JoinMarket NG Architecture](../media/architecture2.svg)
  <figcaption>JoinMarket NG Architecture</figcaption>
</figure>

## Components

The implementation separates concerns into distinct packages:

| Package | Purpose |
|---------|---------|
| `jmcore` | Core library: crypto, protocol definitions, models |
| `jmwallet` | Wallet: BIP32/39/84/86, UTXO management, signing |
| `jmswap` | Private-channel funding and channel-buyout escrow lifecycle |
| `directory_server` | Directory node: message routing, peer registry |
| `maker` | Maker bot: offer management, CoinJoin participation |
| `taker` | Taker bot: CoinJoin orchestration, maker selection |
| `tumbler` | Scheduler: multi-step CoinJoins and maker sessions |
| `jmwalletd` | Wallet daemon: JAM-compatible HTTP and WebSocket API |
| `orderbook_watcher` | Monitoring: orderbook visualization |
| `neutrino_server` (external) | Lightweight SPV server (BIP157/158) - [github.com/m0wer/neutrino-api](https://github.com/m0wer/neutrino-api) |

## Data Directory

JoinMarket NG uses a dedicated data directory for persistent files shared across sessions.

**Location:**

- Default: `~/.joinmarket-ng`
- Override: `--data-dir` CLI flag or `$JOINMARKET_DATA_DIR` environment variable
- Docker: `/home/jm/.joinmarket-ng` (mounted as volume)

**Structure:**

```
~/.joinmarket-ng/
├── config.toml            # Configuration file
├── cmtdata/
│   ├── commitmentlist     # PoDLE commitment blacklist (makers)
│   └── commitments.json   # PoDLE used commitments (takers)
├── state/
│   ├── maker.nick         # Current maker nick
│   ├── taker.nick         # Current taker nick
│   ├── directory.nick     # Current directory server nick
│   └── orderbook.nick     # Current orderbook watcher nick
├── history.csv            # Transaction history log (CoinJoins + plain sends)
├── wallets/
│   ├── default.mnemonic        # Encrypted BIP39 mnemonic (CLI wallets)
│   └── default.mnemonic.meta   # Sidecar: creation_height + cached wallet fingerprint
├── wallet_metadata_<fp>.jsonl  # Per-wallet UTXO/address metadata (fp = master-key fingerprint)
└── fidelity_bonds_<fp>.json    # Per-wallet fidelity bond registry (fp = master-key fingerprint)
```

The `<fp>` placeholder is the 8-char hex fingerprint of the wallet's master key,
matching `jm-wallet info`. Files with this suffix are scoped to a single wallet
so different wallets sharing the same data directory do not see each other's
bonds or address metadata. A pre-partition `fidelity_bonds.json` (without
fingerprint) is migrated into the per-wallet file automatically the first time
its owning wallet is opened; entries the migration cannot attribute remain in
the shared file until claimed.

**Shared Files:**

| File | Used By | Purpose |
|------|---------|---------|
| `cmtdata/commitmentlist` | Makers | Network-wide blacklisted PoDLE commitments |
| `cmtdata/commitments.json` | Takers | Locally used commitments (prevents reuse) |
| `history.csv` | Both | Transaction history with confirmation tracking (CoinJoins and plain sends; legacy name: `coinjoin_history.csv`, renamed in place on first read) |
| `state/*.nick` | All | Component nick files for self-CoinJoin protection |

**Nick State Files:**

Written at startup, deleted on shutdown. Used for:

- External monitoring of running bots
- Startup notifications with nick identification
- **Self-CoinJoin Protection**: Taker reads `state/maker.nick` to exclude own maker; maker reads `state/taker.nick` to reject own taker

**CoinJoin History:**

Records all CoinJoin transactions with:

- Pending transaction tracking (initially `success=False`, updated on confirmation)
- Automatic txid discovery for makers who didn't receive the final transaction
- Address blacklisting for privacy (addresses recorded before being shared with peers)
- CSV format for analysis: `jm-wallet history --stats`

## Wallet File Formats

Three wallet persistence formats with overlapping terminology are not
interoperable:

- **joinmarket-clientserver JMDAT:** the reference implementation stores wallet
  state in its JMDAT format, conventionally named `wallet.jmdat`. JoinMarket NG
  does not read that format.
- **JoinMarket NG native CLI:** `jm-wallet`, `jm-maker`, and `jm-taker` use an
  encrypted BIP39 mnemonic file conventionally named `*.mnemonic`, with state in
  fingerprint-keyed sidecars. Select a non-default file with `--mnemonic-file`.
- **JoinMarket NG daemon/JAM:** `jmwalletd` writes a versioned encrypted container
  identified by the `JMNG` magic bytes. JAM v1 currently uses `.jmdat` filenames
  for API compatibility, but these files do not contain JMDAT and cannot be
  opened by joinmarket-clientserver; `jmwalletd` likewise cannot open a reference
  JMDAT wallet.

The intended future native suffix for the daemon container is `.jmng`. Version 1
continues to use `.jmdat` naming for JAM compatibility until wallet-name aliases
or capability support exists. No suffix alias or migration is currently
implemented.

## Wallet Persistence Design ([issue #524](https://github.com/joinmarket-ng/joinmarket-ng/issues/524))

Per-wallet state is split across several files keyed by the 8-char BIP32
`m/0` fingerprint rather than packed into one encrypted container (the
reference implementation's JMDAT model). The split was chosen deliberately:

- **Concurrency:** a running maker, a CLI command, and `jmwalletd` can
  touch wallet state at the same time. Independent files with atomic
  per-file writes (and a flock sidecar for the metadata store) avoid the
  single-file lock contention and stale PID-lock recovery that the
  reference JMDAT format suffers from.
- **Passwordless reads:** the mnemonic password guards spending, not
  inspection. History, bond listing, and labels live outside the
  encrypted mnemonic so they can be read without decryption. The
  `.mnemonic.meta` sidecar caches the wallet fingerprint so even
  resolving "which wallet is active" needs no password.
- **Interoperability:** UTXO/label/freeze state is stored as BIP-329
  JSON Lines, importable by other wallets; UTXO state itself lives in the
  external Bitcoin Core descriptor wallet rather than being duplicated.
  Handed-out and user-reserved deposit addresses are recorded in the same
  file (`jm:reserved` address labels) so they are never reissued across
  restarts.

The metadata file also carries owner-qualified, temporary CoinJoin input
leases. All concurrently running JoinMarket NG processes that share a wallet
metadata file must be upgraded together when the lease format or semantics
change. An older binary cannot enforce ownership fields it does not understand,
so mixed-version concurrent access is unsupported; advisory locking alone
cannot make an old process apply new lease rules.

Active channel-ring records retain the owner token for their wallet input leases.
On startup and periodic reconciliation, makers and takers renew matching live
leases before attempting to restore missing or expired leases from that durable
record. After upgrade, existing owned records keep this restoration behavior;
no wallet or ring format migration is required. A live lease belonging to another
owner, a partially present set of leases, a corrupt record, or an older record
without an owner token blocks ring recovery. Missing metadata alone never
authorizes restoration, and recovery does not overwrite another owner's lease.
If a running maker cannot reconcile ring records or renew their input leases,
it stops accepting all fills (including ordinary offers) and exits with an
operator-recovery error. The existing leases still expire with time; this
fail-closed process exit does not make deleted journals or another process using
the same wallet safe. Stop every wallet-sharing process until the state is
resolved rather than restarting the maker or spending the inputs.

A maker invitation that never receives a ring plan is retired once its
ordinary CoinJoin session is gone and one ring phase timeout has passed since
the invitation. At that point the maker has sent only a signed hello (no plan,
LND operation, or signature), so it releases the invitation's own input leases.
Planned and later states are never expired this way.

`jm-maker ring-records` prints retained ring records read-only for recovery; see
[Experimental Ring Market](../experimental-ring-market.md#stop-conditions-and-recovery).
No command retires records automatically.

Ring deployments must budget the maker's `session_timeout_sec` and
`pre_sign_timeout_sec` for negotiation and cancellation. If the ordinary session
expires first, its authenticated message route is removed while durable ring
state can still require the inputs to remain locked.

A durably retired cancellation also releases the session's in-flight PoDLE
outpoint reservation, allowing a later round to authenticate with a fresh proof.
Used-proof checks still apply.

Active-wallet identity is resolved uniformly for all per-wallet read
commands (see [Wallet](wallet.md)): explicit fingerprint, then `--mnemonic-file`,
then the configured/default wallet's cached `.meta` fingerprint, then
single-wallet auto-detection. This single resolution path is what keeps
each wallet's history and bonds isolated
([#473](https://github.com/joinmarket-ng/joinmarket-ng/issues/473),
[#492](https://github.com/joinmarket-ng/joinmarket-ng/issues/492),
[#523](https://github.com/joinmarket-ng/joinmarket-ng/issues/523)).

Files that are intentionally **not** per-wallet: `cmtdata/*` (PoDLE
commitments are UTXO-derived and the blacklist is network-shared),
`state/*.nick` (per component, for self-CoinJoin protection),
`ignored_makers.txt`, and `config.toml`.

---

## Private Channel Rings

`private_channel_ring` is the single public capability for private Taproot
channel rings. It is negotiated per session; it is not an older cofunded-v1
capability and has no legacy fallback. The ring needs at least three channel
participants: the taker plus two ring-capable makers, or three ring-capable
makers when the taker does not participate. Ordinary makers can fill
other slots without joining the ring. The separate CoinJoin maker-count floor
still applies, and the default target remains eight to ten makers. When the
taker has no configured LND node, or explicitly opts out of joining despite
having one configured, a separate coordinator journal permits a three-maker
channel cycle with ordinary P2TR taker change. This maker-only
path remains experimental: interruption before an exact signed final
transaction cannot be automatically canceled after authenticated maker
sessions end. Do not use it with funds that require unattended recovery.
Coordinator messages use a separate per-round signing key, not the taker's
channel endpoint key, so the protocol signer alone does not identify a member
of the channel cycle. This does not hide a node whose Lightning identity was
already linked to a JoinMarket participant through other observations.
The default taker maker floor is two when participating, or three when not.
A maker enforces a minimum of three channel endpoints
regardless of the configured `minimum_makers`, but cannot determine which
endpoint, if any, is the taker. A scarce orderbook or observed Lightning peers
can still make participation inferable despite the separate coordinator key.
In a maker-only round with exactly three makers and no ordinary maker,
the manifest identifies all channel output indices, leaving the taker's change
as the only ordinary output visible to ring makers. This three-member minimum
is allowed by policy but does not hide that change from those makers. An
additional ordinary maker can remove the immediate one-output inference, not
guarantee anonymity against colluding makers.

Funding verification writes an exact unsigned transaction and PSBT intent
before calling LND. If LND's response is lost, the journal remains unresolved
and cannot cancel the shim automatically; operator-assisted recovery is required.
Older `prepared` records cannot prove whether verification already happened and
are likewise kept unresolved rather than canceled.
Maker-only coordinator records persist reached maker identities, selected inputs,
and signing intent independently of any local LND endpoint. After a taker crash,
reconciliation rebroadcasts only a durable final transaction or confirms the
exact manifested transaction; it cannot recreate the authenticated maker
sessions needed to cancel an unfinished round. Leave the input reservations
in place and obtain affirmative maker and chain evidence before any manual
release. A corrupt or partial coordinator journal stops taker startup, but
time-limited wallet leases can then expire: stop **every process** sharing the
wallet and do not spend from it until the journal and maker states are resolved.
Missing journal fields are not evidence that the round never started.

For a mixed transaction with `N` total participants and `R` ring participants,
the output set contains `N` equal P2TR outputs, `R` private channel outputs,
and `N-R` exact ordinary P2TR change outputs, for `2N` outputs total. Ordinary
makers receive only their usual CoinJoin messages and change output; they do
not receive ring messages or ring contact information. They must nevertheless
advertise an input hold long enough for ring setup; an unmodified reference
maker that sends legacy five-field `!ioauth` cannot safely serve as an ordinary
maker in a ring round. That format remains accepted for non-ring CoinJoins.

The setup deadline is `[taker.channel_ring].setup_timeout_seconds`, whose
default is 600 seconds. `[maker.channel_ring].hold_safety_margin_seconds`
defaults to 30 seconds, and `[maker.channel_ring].maker_setup_hold_seconds`
defaults to 660 seconds. The maker hold must cover the setup deadline, the
configured margin, and a further 30-second ring setup slack. Selected ordinary
makers must also retain their inputs until setup completes. If the taker specifies
exact input outpoints, both ring modes abort rather than silently select more
wallet inputs when authenticated fees exceed the estimate.

Ring makers currently cannot enforce their configured minimum *final* miner-fee
rate before releasing signatures: the ring path verifies the manifest and
chain-resolved prevouts but does not run the ordinary maker's estimated fee
check. An unsigned P2TR or P2WSH input does not bound its eventual witness
size, so adding a template estimate would not prove the signed transaction's
fee rate. An honest taker checks actual signed vsize before broadcasting; that
does not constrain a malicious taker holding maker signatures. Do not rely on
the maker fee-floor setting as a ring-parent relayability guarantee.

### Testing

The ring, maker-only ring, and buyout lifecycles run in Docker regtest fixtures:

```sh
docker build -t jm-buyout-lnd:v0.21.3-beta lnd
scripts/run-ring-e2e.sh ring
scripts/run-ring-e2e.sh ring-no-taker
```

A failed or interrupted fixture keeps its chain, journals, and channels for
review. `E2E_RESET=1` deletes one reviewed fixture; it is never a retry of an
uncertain signed round.
