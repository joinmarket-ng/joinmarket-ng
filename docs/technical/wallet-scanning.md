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

This reports whether a rescan is running, the oldest scanned timestamp
(the lower bound of time coverage), and the transaction count. If coverage
is incomplete, repair it with a single tool:

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
scan. Re-attach later with `jm-wallet info --scan-status`. A full rescan
from genesis can take 20+ minutes on mainnet.

### Wallet creation height

When the wallet's creation height is known (recorded in the mnemonic file),
every rescan, including the background full rescan, fidelity-bond recovery,
and `jm-wallet rescan`, is floored to that height. Coins cannot predate the
wallet, so blocks before the creation height are skipped, which can save
hours on mainnet. `--start-height` values below the creation height are
clamped up to it. To deliberately scan earlier blocks (for example, if the
recorded height is wrong), lower the wallet creation height first.

`jm-wallet generate` records the current chain tip as the creation height in
the `.mnemonic.meta` sidecar file (best-effort: the configured backend must
be reachable). This makes the first sync of a freshly generated wallet
near-instant: the descriptor import scans from the wallet's birthday instead
of the ~1 year smart-scan lookback. Wallets created via the daemon record
the creation height inside the wallet file. Imported/recovered mnemonics
have an unknown birthday, so their first sync scans the full smart-scan
window; progress is reported while Bitcoin Core runs that scan, and it is
safe to interrupt (the scan continues server-side).
