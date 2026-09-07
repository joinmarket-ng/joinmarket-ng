## Wallet Scanning

JoinMarket NG tracks your coins by importing address descriptors into
Bitcoin Core's descriptor wallet and asking Core which of those addresses
have been used. Two independent things decide whether a coin shows up, and
they are the source of most "missing balance" confusion.

### Two kinds of coverage

**Index coverage (how many addresses Core watches).** For each mixdepth and
chain (external/internal), Core watches a fixed range of address indices,
`[0, scan_range - 1]`. A coin received on an index beyond that range is
invisible until the range is widened. This is controlled by
`[wallet].scan_range` (default 1000).

**Time coverage (which blocks Core has scanned).** Even within the watched
index range, Core only knows about transactions in the blocks it has
actually scanned. A fresh import scans roughly the last year (smart scan)
and then catches up in the background. A coin in an older, unscanned block
is invisible until those blocks are scanned.

The address range that is actually in effect is whatever JoinMarket NG
imported into the node's descriptor wallet, not a value re-derived on every
run. JoinMarket NG widens it automatically as you use addresses, keeping a
buffer of `[wallet].gap_limit` empty addresses ahead of the highest used
one.

### The three settings

| Setting | Default | Meaning |
|---------|---------|---------|
| `scan_range` | 1000 | Initial address-index range imported per branch. Auto-expands as addresses are used. |
| `gap_limit` | 20 | BIP44 trailing-empty threshold: how many empty addresses to keep ahead of the highest used one (also the auto-expansion buffer). |
| `scan_lookback_blocks` | 52560 | How far back the initial smart scan looks (~1 year). A background full rescan follows. |

`scan_range` is about index coverage; `gap_limit` decides when that range
grows; `scan_lookback_blocks` is about initial time coverage. For normal
use the defaults are fine and you never touch them.

Fidelity bonds use a separate timelock branch that cannot be represented by
the regular ranged descriptors. The first synchronization of an imported CLI
wallet, and jmwalletd recovery of an `sw-fb` wallet, imports all 960 canonical
fidelity-bond address descriptors before scanning (Bitcoin Core), or scans and
backfills the same 960 addresses through the light-client watch list (neutrino).
Found bonds are recorded in the per-wallet registry, so normal maker startup
can select them without a separate recovery command. Completion is stored by
derived wallet fingerprint in the mnemonic's `.meta` sidecar, so different
BIP39 passphrases are recovered independently. Only newly imported wallets
explicitly marked pending start recovery automatically. Missing metadata on an
existing wallet does not trigger a recovery scan after an upgrade. Existing
registered bonds still participate in normal synchronization; use
`jm-wallet recover-bonds` to discover any missing historical bonds explicitly.

An INFO message about missing fidelity-bond recovery metadata means that this
installation has no recorded recovery coverage for the wallet. It does not identify
the wallet as coming from the reference implementation, nor prove that recovery
is needed or that a previous scan failed. No completion marker is filled in
automatically. This can occur with wallets created or imported before recovery
metadata was introduced.

A regular history scan, including the default roughly one-year smart scan or a
full block rescan, does not by itself prove that all 960 fidelity-bond addresses
were searched. Routine bond synchronization covers only registered bonds. Explicit
`jm-wallet recover-bonds` derives all 960 addresses and, with Bitcoin Core, scans
from genesis (or the recorded wallet creation height), regardless of the smart-scan
lookback setting.

If full fidelity-bond recovery already completed and only its metadata is missing,
you can record that fact without repeating the scan:

```bash
jm-wallet recover-bonds --mark-scanned --mnemonic-file /path/to/wallet.mnemonic
```

Use the same BIP39 passphrase as that wallet, adding `--prompt-bip39-passphrase`
when needed. The command displays the selected file and derived fingerprint and
requires confirmation (default: no). It works offline and only writes the
fingerprint-scoped completion marker, preserving other metadata. It does not
discover bonds, verify coverage, or stop any running scan. Do not confirm based
only on a regular history scan or while recovery is still running. An incorrect
confirmation can leave historical bonds undiscovered and suppress automatic
recovery for that wallet. If unsure, leave coverage unknown or explicitly run
`jm-wallet recover-bonds`; that command remains available even after marking
recovery complete.

Recovery records a started state before importing descriptors, and concurrent
recovery attempts for the same mnemonic file are refused. Completion is recorded
only after the recovery RPC succeeds and discovered bonds are persisted. If the
command exits, the connection fails, or recovery is aborted, its outcome remains
unconfirmed and routine synchronization does not automatically retry. Core may
continue scanning after the CLI exits. Check `jm-wallet info --scan-status`, then
use `jm-wallet recover-bonds` for an explicit retry when the existing scan ends.

For Bitcoin Core descriptor recovery, the API also accepts an optional
`scan_range` field (up to 10,000) for regular address-index coverage; it can
only widen coverage, never shrink it below the configured
`[wallet].scan_range`. Neutrino regular-address discovery continues to follow
the BIP44 `gap_limit`. `jm-wallet recover-bonds` remains available for an
explicit rescan.

For Neutrino, newly derived regular addresses are historically backfilled before
they are considered empty. JoinMarket scans up to 100 indices per branch in each
historical pass, persists that coverage, then evaluates the normal gap-sized
batches without repeating the deep rescan. On neutrino-api 1.4.0+ with transaction
history enabled, spent-only receive addresses count as used and discovery
continues until the full trailing gap is empty. Older servers retain the UTXO-only
fallback; upgrade before recovering a wallet with substantial prior activity.

When Neutrino adds the 960 recovery candidates after its initial sync,
JoinMarket requests a forced historical rescan so persisted global coverage
does not hide transactions for those newly watched addresses. Servers without
the force capability fall back to requesting one block below the persisted
coverage floor; reliable completion confirmation requires a neutrino-api
version that exposes `GET /v1/rescan/status` (v0.7.0+).

Completed coverage for registered bond addresses is stored as wallet-scoped
hashes, so later processes re-register those addresses without repeating the
historical rescan. Bitcoin Core descriptor wallets obtain the equivalent behavior
from their persistent imported `addr()` descriptors.

### The 1,000,000 index limit

Bitcoin Core's `importdescriptors` rejects any descriptor whose range spans
more than 1,000,000 indices with the error `Range is too large`. A range of
`[0, N]` therefore allows at most 1,000,000 addresses per branch (indices
0 through 999,999), so `scan_range` and `--scan-depth` are capped at
1,000,000.

JoinMarket NG enforces this cap for you: values above the limit (whether in
`[wallet].scan_range`, via `jm-wallet rescan --scan-depth`, or through
automatic range expansion) are clamped down to 1,000,000 with a warning
rather than being sent to Core and failing the whole import. Earlier versions
forwarded oversized ranges unchanged, so every descriptor came back with
`Range is too large` and the wallet was left without any new coverage.

If a wallet genuinely has coins beyond index 999,999 on a single branch, a
single descriptor cannot track them. This is extremely unlikely in practice;
reach out before attempting a workaround.

### Diagnosing and repairing coverage

When the wallet proposes an address you have already used, or a known
balance is missing, check coverage:

```bash
jm-wallet info --scan-status
```

This reports current rescan activity, the oldest active descriptor import
timestamp, and the transaction count. Descriptor timestamps are not changed by
later block rescans and do not prove historical scan coverage. Unavailable scan
status is reported as unknown, not idle. A background scan that is no longer
running may have completed, failed, or been aborted. If known history is missing,
repair it with a single tool:

```bash
# Time-coverage repair: re-scan blocks against the current address range.
jm-wallet rescan                  # from genesis
jm-wallet rescan --start-height H # from a known height

# Index-coverage repair: widen the address range, then rescan from genesis.
# Use this once for wallets migrated from legacy joinmarket-clientserver
# whose used addresses sit beyond the default range. Capped at 1,000,000
# (Bitcoin Core's per-descriptor range limit).
jm-wallet rescan --scan-depth 10000

# Widen the range but only rescan from a known height to save time. Use
# this when you know all your coins are no older than block H.
jm-wallet rescan --scan-depth 10000 --start-height H
```

Rescans are read-only and run server-side in Bitcoin Core, so they are
safe to interrupt: pressing Ctrl-C stops only the progress polling, not the
scan. Re-attach later with `jm-wallet info --scan-status`. A full rescan can
take a long time on mainnet. Duration varies substantially with the Bitcoin
node and its storage performance. When `jm-wallet rescan` remains attached
through completion, it performs a final wallet sync and reconstructs imported
wallet history automatically. If polling was interrupted, opening CoinJoin
History in the TUI performs the sync and reconstruction after Core finishes.

### Wallet creation height

When the wallet's creation height is known (recorded in the mnemonic file),
every rescan, including the background full rescan, fidelity-bond recovery,
and `jm-wallet rescan`, is floored to that height. Coins cannot predate the
wallet, so blocks before the creation height are skipped, reducing the amount
of blockchain history Bitcoin Core must scan. `--start-height` values below
the creation height are clamped up to it. To deliberately scan earlier blocks
(for example, if the recorded height is wrong), lower the wallet creation
height first.

`jm-wallet generate` records the current chain tip as the creation height in
the `.mnemonic.meta` sidecar file (best-effort: the configured backend must
be reachable). This reduces the first-sync scan window for a freshly generated
wallet: the descriptor import scans from the wallet's birthday instead of the
~1 year smart-scan lookback. Generated wallets are also marked as not requiring
fidelity-bond recovery, even when the creation-height lookup fails. Wallets
created via the daemon record the creation height inside the wallet file.
Imported/recovered mnemonics have an unknown birthday, so their one-time bond
recovery may scan from genesis and can take a long time. The configured
`scan_start_height` and `scan_lookback_blocks` control the initial smart import,
not an explicit full recovery or repair rescan. Incomplete bond recovery requires
an explicit retry rather than restarting during the next balance query.
