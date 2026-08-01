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
| `jmwallet` | Wallet: BIP32/39/84, UTXO management, signing |
| `directory_server` | Directory node: message routing, peer registry |
| `maker` | Maker bot: offer management, CoinJoin participation |
| `taker` | Taker bot: CoinJoin orchestration, maker selection |
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

## Wallet Persistence Design (issue #524)

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

Active-wallet identity is resolved uniformly for all per-wallet read
commands (see `wallet.md`): explicit fingerprint, then `--mnemonic-file`,
then the configured/default wallet's cached `.meta` fingerprint, then
single-wallet auto-detection. This single resolution path is what keeps
each wallet's history and bonds isolated (issues #473, #492, #523).

Files that are intentionally **not** per-wallet: `cmtdata/*` (PoDLE
commitments are UTXO-derived and the blacklist is network-shared),
`state/*.nick` (per component, for self-CoinJoin protection),
`ignored_makers.txt`, and `config.toml`.

---
