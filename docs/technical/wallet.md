## Wallet

### HD Structure

HD path: `m/84'/coin_type'/mixdepth'/chain/index` (BIP84 P2WPKH)

`coin_type` is `0` on mainnet and `1` on testnet/signet/regtest.

- **Mixdepths**: 5 isolated accounts (0-4)
- **Chains**: External (0) for receiving, Internal (1) for change
- **Index**: Sequential address index

### BIP39 Passphrase Support

JoinMarket NG supports the optional BIP39 passphrase ("25th word"):

**Important Distinction:**

- **File encryption password** (`--password`): Encrypts mnemonic file with AES (Fernet, key derived via Argon2id; legacy files using PBKDF2 are still readable)
- **BIP39 passphrase** (`--bip39-passphrase`): Used in seed derivation per BIP39

The passphrase is provided when **using** the wallet, not when importing:

```bash
# Import only stores mnemonic (no passphrase)
jm-wallet import --words 24

# Passphrase provided at usage time:
jm-wallet info --prompt-bip39-passphrase
jm-wallet info --bip39-passphrase "my phrase"
BIP39_PASSPHRASE="my phrase" jm-wallet info
```

**Security Notes:**

- Empty passphrase (`""`) is valid and different from no passphrase
- Passphrase is case-sensitive and whitespace-sensitive
- Can be set in `[wallet] bip39_passphrase` in `config.toml`, but this is discouraged because it places the passphrase next to the encrypted mnemonic; prefer `--prompt-bip39-passphrase` or the `BIP39_PASSPHRASE` env variable.

### Wallet File Encryption

Encrypted mnemonic files written by `jmwalletd` use a versioned binary format:

```
[ magic "JMNG" 4B ][ ver 1B ][ kdf_id 1B ][ m_cost u32 BE ][ t_cost u32 BE ][ p_cost u8 ][ salt 16B ][ Fernet token ]
```

Defaults are Argon2id with OWASP 2024 baseline parameters (memory 19 MiB,
time cost 2, parallelism 1). KDF parameters are stored per file so they
can be raised over time without breaking older wallets.

Wallet files written by older builds use a legacy layout with no magic
header (raw 16-byte salt followed by a Fernet token whose key was
derived via PBKDF2-HMAC-SHA256 with 600,000 iterations). These files
remain loadable. They are not silently re-encrypted: to migrate an
existing wallet to Argon2id, create a new wallet and move funds, or
trigger a re-save through any future password-change flow.

### UTXO Selection

**Taker Selection:**

- **Normal**: Minimum UTXOs to cover `cj_amount + fees`
- **Sweep** (`--amount=0`): All UTXOs, zero change (best privacy)

```bash
jm-taker coinjoin --amount=0 --mixdepth=0 --destination=INTERNAL
```

**Maker Merge Algorithms:**

| Algorithm | Behavior |
|-----------|----------|
| `default` | Minimum UTXOs only |
| `gradual` | Minimum + 1 small UTXO |
| `greedy` | All UTXOs from mixdepth |
| `random` | Minimum + 0-2 random UTXOs |

```bash
jm-maker start --merge-algorithm=greedy
```

Privacy tradeoff: More inputs = faster consolidation but reveals UTXO clustering.

### Forced Address-Reuse Auto-Freeze

When a UTXO arrives on a wallet address that was previously used and is now
empty, it is automatically frozen during sync so it is never co-spent in a
CoinJoin (which would link the wallet's coins via the common-input-ownership
heuristic). This defends against forced address-reuse (dust) attacks, where an
adversary sends a small payment to a spent address hoping it gets merged into a
later transaction. See https://en.bitcoin.it/wiki/Privacy#Forced_address_reuse.

Only re-funding of an already-spent (empty) used address is frozen. Coins
arriving on an address that still holds funds are left spendable, because the
privacy-correct action there is to fully spend all coins on that address
together. A freshly arrived UTXO on a brand-new address is never frozen, and a
pre-existing UTXO is never auto-frozen. Frozen reuse UTXOs are labeled in the
metadata store and can be released with `jm-wallet unfreeze` (an explicit
unfreeze is never overridden by a later sync). Fidelity bonds are exempt.

To decide that an address was "spent empty", the wallet relies on having
positively observed that address holding a coin, not merely on the persisted
used-address set. This avoids a false positive where a legitimate first-use coin
only becomes visible on a later sync (for example while a background descriptor
rescan is still catching up, after a transient RPC failure, or following a
descriptor-range upgrade): such a coin is left spendable rather than mistaken
for forced reuse.

These observations (the set of addresses seen funded and the set of outpoints
seen unspent) are persisted in the BIP-329 metadata store as JoinMarket
extensions (`jm:funded` address records and a `jm_seen` flag on output records,
both ignored by other consumers) and reseeded at startup, so the defense
survives restarts: an address emptied before a restart and refunded after it is
still frozen. The persisted seen-outpoint set also keeps the guarantees intact
across restarts, namely that coins which predate the restart are left spendable
(they are in the seen set) and that a genuinely late-discovered first-use coin
is not frozen (its address was never persisted as observed-funded).

The `[wallet] max_sats_freeze_reuse` setting controls the threshold: `-1`
(default) freezes all such reuse UTXOs, a positive `N` freezes only those with
value `<= N` sats, and `0` disables the behavior. This is based on the legacy
joinmarket-clientserver `POLICY.max_sats_freeze_reuse` option (joinmarket-ng
additionally restricts freezing to spent-empty addresses).

### Address Status Labels

The wallet display annotates each funded address with a status: `deposit`
(external coin received from outside), `cj-out` (an equal-amount CoinJoin
output), `cj-change` (our change inside a CoinJoin, which is deanonymising and
shown distinctly), and `non-cj-change` (ordinary, non-CoinJoin change). These
are derived primarily from the per-wallet CoinJoin history file, which records
the output and change addresses of every CoinJoin this wallet performed as
maker or taker.

A funded address is instead labeled `reused` (a privacy warning that takes
precedence over the labels above) when it has been paid to more than once:
either it currently holds more than one UTXO, or it holds a single UTXO that the
forced-address-reuse defense auto-froze (funds that landed on an
already-used-then-emptied address). This mirrors the legacy
joinmarket-clientserver `reused` status.

A wallet imported or recovered from seed has no such history file, so every
coin would otherwise fall back to `deposit` (external branch) or
`non-cj-change` (internal branch), even when it actually came from a CoinJoin.
To recover the correct labels, the wallet reconstructs them from on-chain data:
for each funded coin without a local-history classification it fetches the
transaction that created it and applies the same equal-output heuristic the
legacy joinmarket-clientserver uses (a transaction is a CoinJoin when its most
frequent output value repeats more than once and the count of those equal
outputs matches the number of other outputs, with `+1` slack for one
no-change participant). The derived origin (`cj_out` / `cj_change` / `deposit`
/ `non_cj_change`) is persisted into the BIP-329 metadata store, so the work is
done once and the display then surfaces the true status.

The reconstruction is best-effort and bounded: it runs once per process during
the bond-aware sync, skips addresses the local history already classifies (those
remain authoritative) and addresses classified on a previous run, dedupes work
per transaction, and degrades silently to the `deposit` / `non-cj-change`
fallback when the backend cannot return a transaction. A blockchain rescan
re-runs it so coins surfaced by the rescan are classified too. Only the imported
backlog needs this; coins received while running are either this wallet's own
CoinJoins (recorded in history) or genuine deposits.

### Backend Systems

**Descriptor Wallet Backend (Recommended):**

- Method: `importdescriptors` + `listunspent` RPC
- Requirements: Bitcoin Core v24+
- Storage: ~900 GB + small wallet file
- Sync: Fast after initial descriptor import
- **Smart Scan**: Scans ~1 year of blocks initially, full rescan in background

Trade-off: Addresses stored in Core wallet file - never use with third-party node.

**Neutrino Backend:**

- Method: BIP157/158 compact block filters
- Requirements: [neutrino-api server](https://github.com/m0wer/neutrino-api)
- Storage: ~500 MB
- Sync: Minutes instead of days

**Decision Matrix:**

- Use DescriptorWallet if: You run a full node (recommended)
- Use BitcoinCore if: Simple one-off UTXO queries
- Use Neutrino if: Limited storage, fast setup needed

**Neutrino Broadcast Strategy:**

Neutrino's broadcast and verification behavior depends on whether the
connected `neutrino-api` server exposes the watched-only mempool
tracker (`mempool_enabled: true` on `/v1/status`).

| Policy | With mempool tracker | Without mempool tracker (legacy) |
|--------|----------------------|---------------------------------|
| `SELF` | Broadcast via own backend, verify via mempool, then confirmation | Broadcast via own backend (always verifiable on chain) |
| `RANDOM_PEER` | Try makers sequentially, verify via mempool, fall back to self | Forced to all-makers fan-out (see below) |
| `MULTIPLE_PEERS` | Broadcast to N makers simultaneously (default), verify via mempool | Forced to all-makers fan-out |
| `NOT_SELF` | Try makers only, verify via mempool, no fallback | Forced to all-makers fan-out, no fallback |

When mempool access is unavailable (legacy server, or operator opt-out
via `bitcoin.neutrino_include_mempool = false`), all non-`SELF`
policies fan out the `!push` to every available maker simultaneously.
This avoids the privacy-leaking self-broadcast fallback when an
individual maker is offline (issue #482); confirmation is then
established via block-based UTXO lookups.

When the tracker is available, neutrino behaves like the descriptor
wallet backend: it can confirm that a maker actually broadcast the
transaction and short-circuit the fan-out. `jm-wallet info
--extended` also annotates addresses with `(unconfirmed)` for
mempool UTXOs.

### Periodic Wallet Rescan

Both maker and taker support periodic rescanning:

| Setting | Default | Description |
|---------|---------|-------------|
| `rescan_interval_sec` | 600 | How often to rescan |
| `post_coinjoin_rescan_delay` | 60 | Delay after CoinJoin (maker) |

**Maker:** After CoinJoin, rescans to detect balance changes and update offers automatically.

**Taker:** Rescans between schedule entries to track pending confirmations.

For how address-index coverage and block-time coverage interact, and how to
diagnose and repair missing balances, see
[Wallet Scanning](wallet-scanning.md).

### Multiple Wallets in One Data Directory

JoinMarket-NG records every CoinJoin (as taker or maker) in a single
`history.csv` file inside the data directory (legacy installs may still
have it under the old name `coinjoin_history.csv`; the wallet renames it
in place on first read). Each row is tagged with
the BIP32 master fingerprint (`wallet_fingerprint`, first 4 bytes of `m/0`),
so commands like `jm-wallet history` and `jm-wallet info` filter to the
correct wallet automatically when a mnemonic is supplied.

The same fingerprint scopes the fidelity bond registry on disk as
`fidelity_bonds_<fingerprint>.json` (issue #492). Both `jm-wallet
list-bonds` and `jm-wallet registry-show` read this per-wallet file.

Both `jmwalletd` and the `jm-wallet` CLI read this registry and run a
bond-aware sync that scans the registered bond addresses on the timelock
branch (`.../2/...`), which is not part of the standard wallet descriptor
set. `jmwalletd` does this on wallet open/recover and on each
`/wallet/{name}/utxos` and `/wallet/{name}/display` request; the CLI does it
for `jm-wallet info`, `send`, `freeze`, and `sync-bonds`. For descriptor-wallet
backends it imports the bond `addr()` descriptors, detecting which are missing
by the actual `addr()` descriptor set (not a descriptor count, which
over-counts the base wallet) and rescanning so an already-funded bond is found.
For light-client (Neutrino) backends it forces a historical rescan of the bond
addresses so a bond funded before the address was watched is still found.
Funded bonds are then returned by the UTXO API with a `locktime` field
(matching legacy joinmarket-clientserver) so frontends such as JAM recognize
them. Without this the bond branch is never queried and funded bonds would be
invisible (the coins would appear to "disappear", or the bond address would
show as locked with a 0 sat balance).

For descriptor-wallet backends, sync also self-heals bonds that have no
registry entry at all. Every fidelity bond address is deterministically
derivable from the seed (`m/84'/coin'/0'/2/<timenumber>`, one address per
timenumber, 960 total), so if Bitcoin Core already tracks a bond UTXO -- for
example from a previous `recover-bonds` run, or a legacy registry entry that
the per-wallet migration could not claim (mismatched `pubkey`/`path`) -- sync
re-derives the canonical address, recognizes the UTXO, and writes it into the
per-wallet registry automatically. This closes the gap where a wallet's
displayed bond count (which reads the registry with the legacy-file fallback
enabled for `jm-wallet info`) could disagree with what sync actually counted
(which never uses that fallback): the funded bond is recovered on the next
sync either way, without requiring `recover-bonds` or `import-bond` to be run
manually.

To pick a wallet, the offline commands `history`, `list-bonds` and
`registry-show` accept the following inputs (in priority order):

1. `--wallet-fingerprint <fp>` (8-char hex, printed by `jm-wallet info`).
   Use this when you already know the fingerprint and want to skip
   mnemonic decryption.
2. `--mnemonic-file <file>` together with `--prompt-bip39-passphrase`
   (or `BIP39_PASSPHRASE` env / `[wallet] bip39_passphrase` config)
   when the wallet was created with a BIP39 passphrase. Without the
   matching passphrase the derived fingerprint will not match any
   recorded data, so the commands will appear "empty".
3. The configured active wallet (`MNEMONIC_FILE` env, `[wallet]
   mnemonic_file` in `config.toml`, or `wallets/default.mnemonic`).
   Its fingerprint is read from the companion `.meta` sidecar without
   decrypting the mnemonic; for a legacy wallet that has no cached
   fingerprint yet, the mnemonic is decrypted once and the derived
   fingerprint is written back to the sidecar so later reads stay
   passwordless. This is what makes `jm-wallet history` show the active
   wallet's CoinJoins rather than another wallet's (issue #523).
4. Auto-detection when the data directory contains exactly one
   wallet's data (one fingerprint in `history.csv` for `history`, one
   `fidelity_bonds_*.json` file for `list-bonds` / `registry-show`).
   The selected fingerprint is logged.

When several wallets are present and none of the above identifies one,
the commands abort and list the known fingerprints so the user can
pick. Pass `--all-wallets` to `jm-wallet history` to disable filtering
entirely (also surfaces legacy rows written before per-wallet tagging).
When the active wallet is selected and rows belonging to other wallets
(or legacy untagged rows) are hidden, `jm-wallet history` prints how
many were excluded and reminds you to pass `--all-wallets` to see them,
so the scoping is never silent.

The cached fingerprint is the wallet identity computed with the BIP39
passphrase in effect when it was first resolved. If you use the same
mnemonic file under several different BIP39 passphrases, pass
`--wallet-fingerprint` explicitly for the non-cached identities.

Recommended practice is still to give each wallet its own data directory via
the `JOINMARKET_DATA_DIR` env variable or the `--data-dir` flag. This keeps
config, logs, and the order registry per-wallet, and avoids cases where one
wallet sees pending entries created by another (still tracked correctly, just
visually noisy).

Legacy entries written before per-wallet tagging have an empty fingerprint and
are hidden from filtered views; pass `--all-wallets` to `jm-wallet history` to
see them.

### Viewing the Seed (`jm-wallet showseed`)

`jm-wallet showseed -f <mnemonic-file>` prints the BIP39 seed words after
prompting for the password (when the file is encrypted). The command is
intentionally guarded by a `y/N` confirmation; pass `--yes` to skip it in
scripts. Seed words give full control of the funds: only run the command in
a private setting, and never paste the output anywhere.

### Transaction Signing

All private-key access used to produce transaction signatures is centralized in
the wallet via `WalletService.sign_input`. Higher-level components (the taker
and maker CoinJoin sessions, the reusable `direct_send` helper, and the
`jm-wallet send` command) select inputs and assemble transactions, then ask the
wallet to sign each input. They receive a `SignedInput` (signature, public key,
and witness stack) and never read private keys directly.

Keeping signing in one place narrows the security-critical surface: P2WPKH and
timelocked P2WSH (fidelity bond) signing logic lives in a single audited method
instead of being duplicated across callers.

---
