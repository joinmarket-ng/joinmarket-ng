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

- **Mnemonic**: Recovery words, which must be backed up independently of local files.
- **File encryption password**: Encrypts local seed material; it does not change wallet keys.
- **BIP39 passphrase** (`--prompt-bip39-passphrase`): Used in seed derivation per BIP39

By default, import/generate save mnemonic material without registering a derived
identity. Advanced onboarding is opt-in:

```toml
[wallet]
bip39_passphrase_enabled = true
```

With this setting, saved-wallet onboarding prompts for BIP39 when no credential is
supplied, repeats a nonempty passphrase, and asks to register/select the derived
identity. `--no-register-identity` keeps generation/import seed-only;
`--register-identity` explicitly requests onboarding without enabling the setting.

Upgraded wallets usually need no action (see below). Otherwise, register the
identity explicitly. These commands use the configured or default mnemonic file;
pass `--mnemonic-file` for another one:

```bash
jm-wallet identity register --prompt-bip39-passphrase
jm-wallet identity list
jm-wallet identity select <fingerprint>

# For a wallet intentionally using no BIP39 passphrase:
jm-wallet identity register --no-bip39-passphrase
```

The first registered identity is selected automatically. Registering another
one keeps the current selection unless `--select` is given.

When `--prompt-bip39-passphrase` prompts interactively, input stays hidden.
After you press Enter, the CLI displays whether the passphrase is set or empty
and the derived JoinMarket wallet fingerprint. It asks `Continue with this wallet?`
before loading or scanning the wallet; the default is Yes. Compare the fingerprint
with independent records for the intended wallet. Every passphrase derives a
valid wallet; a registered selection adds a local mismatch check before backend
activity, but the short fingerprint is not an authentication proof.

Environment and config passphrases retain precedence and do not prompt or require
confirmation, including when the prompt flag is supplied. Registered passphrase
wallets prompt when no credential is supplied even if onboarding is disabled.
Registered wallets without passphrases do not prompt. Unregistered wallets retain
legacy empty-passphrase behavior unless prompting is requested or enabled.

`.meta` remembers public identities and their passphrase requirements, never
passphrases. One mnemonic file can register several identities; registering or
deriving another identity never replaces an existing selection. `identity
register --select` explicitly combines registration and reselection. `--yes`
acknowledges metadata disclosure without the registration confirmation prompt.
`jm-wallet delete` rejects files holding several registered identities, since it
would otherwise remove their shared seed material. Retire those wallets and
handle the shared backup explicitly rather than deleting one as if it owned the
whole mnemonic.

Registered `history`, `list-bonds`, and `registry-show` reads, including explicit
`--mnemonic-file` reads, use the selected identity without unlocking. Explicit
prompt/credential requests still derive and validate the selection.
`--wallet-fingerprint` remains the direct passwordless override. Legacy explicit
mnemonic-file reads continue deriving rather than treating a cache as a binding.

On upgrade, earlier releases left the fingerprint of the last derived wallet in
`.meta`. The first unlock that derives the same fingerprint, from any credential
source, registers and selects that identity automatically (passphrase required
or not). Interactive and headless setups therefore keep working unchanged. Until
then, offline reads use the recorded fingerprint. A missing or different hint
adopts nothing: the wallet keeps legacy behavior with a warning, and `identity
register` confirms it. Missing metadata never means no passphrase and never
schedules a migration scan. Wallets generated or imported with onboarding
disabled record their passphrase-free fingerprint the same way.
Adoption and registration preserve birthdays, recovery markers, and history
namespaces. Corrupt or unsupported
metadata requires deliberate repair, not an automatic rewrite. Back up the
sidecar alongside the mnemonic file, but retain independent credential backups.

**Security Notes:**

- An empty passphrase (`""`) is valid and selects the wallet without a passphrase
- Passphrase is case-sensitive and whitespace-sensitive
- BIP39 uses NFKD Unicode normalization; passphrases are not trimmed.
- Identity metadata reveals that a remembered passphrase wallet exists and can
  help check passphrase guesses when the mnemonic is exposed. Do not register if
  that disclosure conflicts with your privacy requirements.
- Do not operate a registered passphrase wallet with older CLI versions: they do
  not enforce the identity binding. Keep a compatible version with the backup.
- Can be set in `[wallet] bip39_passphrase` in `config.toml`, but this is discouraged because it places the passphrase next to the encrypted mnemonic; prefer `--prompt-bip39-passphrase` or the `BIP39_PASSPHRASE` env variable.

### Wallet File Encryption

Encrypted wallet containers written by `jmwalletd` use the versioned `JMNG`
binary format:

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

New daemon containers with nonempty BIP39 passphrases use outer format version 2,
with the same KDF layout and encrypted expected-identity metadata. Older daemons
reject these files instead of opening the empty-passphrase wallet. Empty-passphrase
containers remain version 1. Existing files are never rewritten merely on unlock;
legacy containers remain identity-unconfirmed. This is an accidental rollback
guard, not protection against deliberate header tampering.

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
| `default` | Smallest sufficient single coin, otherwise largest-first; probabilistic random top-up to three inputs |
| `gradual` | Smallest-first prefix of coins below the funding target, pruning its smallest leading coins while still funded |
| `greedy` | Smallest-first funding prefix, pruning unnecessary preceding coins largest-first while retaining the crossing coin; exact matches keep the prefix |
| `greediest` | Unpruned smallest-first prefix of coins below the funding target |
| `random` | Shuffle coins until funded, prune individually unnecessary inputs in random order, then independently shuffle disclosure order |

The funding target includes the CoinJoin amount, the maker's advertised mining-fee
contribution, and a reserve for mandatory change, minus the maker's earned fee.
`gradual` and `greediest` fall back to the smallest sufficient single coin when
all subtarget coins together cannot fund the round. These three consolidation
policies follow the reference implementation's value-based rules. Neither
`greedy` nor `greediest` means spending every eligible coin, and their names do
not guarantee a particular input count or net consolidation.

The default policy balances frugal funding with inventory management. For an
authorized pool of **n eligible coins**, if `n > 2` and funding uses fewer than
three inputs, it tops up to **three total inputs** with probability `(n - 2) / n`.
Extra coin identities are sampled randomly without replacement; disclosure order
is also shuffled. With ten eligible coins the top-up probability is 80%, and with
thirty it is about 93%. There is no top-up for one or two eligible coins, and no
extra consolidation when funding already requires three or more inputs.

A maker normally receives two outputs, so its whole-wallet UTXO count changes
by `2 - input_count` for each successful round. For one-input funding, the default
allows growth at very small inventories, is count-neutral on average at four
eligible coins, and favors consolidation above four. Ten eligible coins give an
expected reduction of 0.6 UTXOs per successful round. This is not a per-mixdepth
ceiling: change returns to the source depth while the CoinJoin output enters the
next depth, and source selection is based on eligible value, not coin count.

All policies use only confirmed, unfrozen, unlocked coins, excluding fidelity
bonds by default. Mixdepth-zero provenance restrictions still apply: the maker
prefers its authorized rotation-lineage pool, otherwise uses just the smallest
sufficient unrelated coin. It never mixes that coin with lineage funds. Only
coins in the authorized, eligible pool count toward the default probability.

`random` randomizes identities, not just the number of extra inputs. For positive
funding targets, its pruning produces an **inclusion-minimal** selection: no
individual selected input can be removed while preserving funding. It always
retains at least one authentication input from a nonempty selection. It does not
guarantee the minimum input count,
uniform probabilities over funded subsets, consolidation, or a five-input cap.
NG takers default to at most 15 inputs per maker and may reject larger selections;
that rejection occurs after the maker has disclosed the selected outpoints.

```bash
jm-maker start --merge-algorithm=greedy
```

Privacy tradeoffs differ by observer. The coordinating taker learns the selected
co-owned inputs and both maker output addresses, including in a round that later
aborts. Random selection can reveal different coins over repeated probes. A
passive chain observer instead infers ownership from amounts, change reuse,
co-spends, and history. Extra inputs may increase consolidation and linkage;
smallest-sufficient selection is not universally better against subset-sum
analysis. Matching reference selection conventions reduces some software
differences, but deterministic value preferences and public sampler rules remain
probabilistic fingerprinting evidence. The default's inventory-dependent top-up
frequency can also disclose approximate eligible-pool size across repeated probes
when the source pool and funding requirements stay stable. This is an intentional
inventory-management tradeoff, not a way to hide the maker's inventory. Randomness
does not break change chains or guarantee anonymity. See the pinned research discussions
[Making JoinMarket makers harder to follow](https://gist.github.com/a228c625fcb6a27c32e298ec903dfc44/66f85345e8900638c1f42e6eed4bcc5ed0441423)
and [Collaborative Transaction Privacy](https://gist.github.com/nothingmuch/d84ba390d89b5b08897af2d95009c2a1/46193b29f7d78cf45282b13f03ca80e924f32f28).

**Upgrade behavior:** existing algorithm names remain accepted, but selection
changes on subsequent maker rounds. An absent setting still selects `default`,
now with the proportional top-up; `gradual` is no longer minimum-plus-one,
`greedy` no longer spends all eligible coins, and `random` no longer adds zero to
two smallest extras. `greediest` is new and older versions reject that setting.
There is no wallet-format migration, startup sweep, rescan, or metadata rewrite.
Consolidation occurs only during normal maker rounds, subject to the same locks
and provenance safeguards. Taker and plain-send coin selection are unchanged.

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

A funded address additionally carries a `reused` privacy warning when it has
been paid to more than once: either it currently holds more than one UTXO, or
it holds a single UTXO that the forced-address-reuse defense auto-froze (funds
that landed on an already-used-then-emptied address). This mirrors the legacy
joinmarket-clientserver `reused` status. The CLI keeps the underlying
classification visible by appending the warning to it (for example
`deposit (reused)` or `non-cj-change (reused)`); the JAM-compatible API keeps
reporting the plain `reused` status for backward compatibility, with the
underlying label exposed as `base_status` on `AddressInfo`.

A wallet imported or recovered from seed has no such history file, so every
coin would otherwise fall back to `deposit` (external branch) or
`non-cj-change` (internal branch), even when it actually came from a CoinJoin.
To infer likely labels, the wallet reconstructs them from on-chain data:
for each funded coin without a local-history classification it fetches the
transaction that created it and applies the same equal-output heuristic the
legacy joinmarket-clientserver uses (a transaction is a CoinJoin when its most
frequent output value repeats more than once and the count of those equal
outputs matches the number of other outputs, with `+1` slack for one
no-change participant). The derived origin (`cj_out` / `cj_change` / `deposit`
/ `non_cj_change`) is persisted into the BIP-329 metadata store, so the work is
done once and the display then surfaces the inferred status.

The reconstruction is best-effort and bounded: it runs during the bond-aware
sync, skips addresses the local history already classifies (those remain
authoritative) and addresses classified on a previous run, dedupes work per
transaction, fetches each transaction at most once per process (so a backend
that cannot return it is not re-queried on every sync), and degrades silently to
the `deposit` / `non-cj-change` fallback. A blockchain rescan clears that
per-process memory so coins surfaced by the rescan are classified too. Coins
received while running are picked up on the next sync, so a plain deposit is
labeled `deposit` without a restart.

The same pass also persists this wallet's own confirmed CoinJoin history into the
metadata store: every CoinJoin output address is labeled `jm:used:cj_out` and
every CoinJoin change address `jm:used:cj_change`, including addresses whose
coins have since been spent. Together with the on-chain classifications
(`jm:used:deposit`, `jm:used:non_cj_change`) this makes a BIP-329 export of
`wallet_metadata_<fp>.jsonl` self-describing when imported into another wallet
such as Sparrow, instead of carrying a bare `jm:used` marker per address. Pending
(unconfirmed) CoinJoin rows are not labeled until they confirm, and only
addresses derived by this wallet are written (a taker's external destination is
not).

The reconstructed `cj-out` status is display metadata, not spend authority. The
mixdepth 0 maker restriction starts with exact outpoints backed by this wallet's
successful maker or taker protocol history. It also admits CoinJoin change when
the authoritative protocol history proves recursively that every wallet input
came from that maker-rotation lineage. Plain-send change does not propagate the
lineage. Deposits, mixed ancestry, on-chain reconstruction, and incomplete
legacy rows fail closed and remain single-UTXO only. This deliberately accepts
false negatives after seed recovery: an unrelated payment can imitate the
equal-output shape of a CoinJoin, so a heuristic classification must not grant
permission to merge deposits. User labels likewise cannot grant or revoke the
exemption. Exact CoinJoin-output roots are persisted separately as the
`jm_coinjoin_output` BIP-329 extension.

### Backend Systems

**Descriptor Wallet Backend (Recommended):**

- Method: `importdescriptors` + `listunspent` RPC
- Requirements: Bitcoin Core v24+
- Storage: Bitcoin Core chain data and a descriptor wallet
- Sync: Fast after initial descriptor import
- **Smart Scan**: Scans ~1 year of blocks initially, full rescan in background

Trade-off: Addresses stored in Core wallet file - never use with third-party node.

**Neutrino Backend:**

- Method: BIP157/158 compact block filters
- Requirements: [neutrino-api server](https://github.com/m0wer/neutrino-api)
- Storage: Headers, compact filters, and wallet-related data; depends on retained history
- Sync: Initial header and filter sync, then wallet scanning; duration depends on hardware and history

**Decision Matrix:**

- Use `descriptor_wallet` with a Bitcoin Core node you control (recommended).
- Use `neutrino` when you need a lightweight backend without running Bitcoin Core.
- See [backend setup](../setup.md) for connection and authentication requirements.

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
individual maker is offline ([issue #482](https://github.com/joinmarket-ng/joinmarket-ng/issues/482)); confirmation is then
established via block-based UTXO lookups.

When the tracker is available, neutrino behaves like the descriptor
wallet backend: it can confirm that a maker actually broadcast the
transaction through `/v1/tx/{txid}` and short-circuit the fan-out.
Confirmed-output scans remain the fallback when a transaction leaves the
tracker as it confirms. `jm-wallet info
--extended` also annotates addresses with `(unconfirmed)` for
mempool UTXOs.

### Periodic Wallet Rescan

Both maker and taker support periodic rescanning:

| Setting | Default | Description |
|---------|---------|-------------|
| `rescan_interval_sec` | 600 | How often to rescan |
| `post_coinjoin_rescan_delay` | 60 | Delay after CoinJoin (maker) |

**Maker:** After CoinJoin, rescans to detect balance changes and update offers automatically.
The configured `offer_reannounce_delay_max` privacy delay applies to both public
announcements and private orderbook responses. Until it expires, peers see the
previous offers, while input reservations and live balance checks still protect
against unfillable requests. Failed announcements and withdrawals are retained
for retry on subsequent rescans or reconnection, using the same offer terms
without rerandomizing them. Different directories can receive an update at
different times when connections fail.

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

The history file is plaintext privacy-sensitive metadata. It can contain
wallet fingerprints, transaction IDs, amounts, fees, destination addresses,
counterparty counts, and failure details. Protect the data directory and its
backups accordingly. Deleting only the mnemonic does not remove this history;
`jm-wallet delete --delete-history` removes rows for the selected fingerprint
while preserving other wallets and unattributed legacy rows.

The same fingerprint scopes the fidelity bond registry on disk as
`fidelity_bonds_<fingerprint>.json` ([issue #492](https://github.com/joinmarket-ng/joinmarket-ng/issues/492)). Both `jm-wallet
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
   wallet's CoinJoins rather than another wallet's
   ([issue #523](https://github.com/joinmarket-ng/joinmarket-ng/issues/523)).
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
