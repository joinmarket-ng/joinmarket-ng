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
# whose used addresses sit beyond the default range.
jm-wallet rescan --scan-depth 10000
```

Rescans are read-only and run server-side in Bitcoin Core, so they are
safe to interrupt: pressing Ctrl-C stops only the progress polling, not the
scan. Re-attach later with `jm-wallet info --scan-status`. A full rescan
from genesis can take 20+ minutes on mainnet.
