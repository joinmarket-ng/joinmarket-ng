### System Overview

<figure markdown="span">
  ![JoinMarket NG Architecture](../media/architecture2.svg)
  <figcaption>JoinMarket NG Architecture</figcaption>
</figure>

### Components

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

### Data Directory

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
├── coinjoin_history.csv   # Transaction history log
└── fidelity_bonds.json    # Bond registry
```

**Shared Files:**

| File | Used By | Purpose |
|------|---------|---------|
| `cmtdata/commitmentlist` | Makers | Network-wide blacklisted PoDLE commitments |
| `cmtdata/commitments.json` | Takers | Locally used commitments (prevents reuse) |
| `coinjoin_history.csv` | Both | Transaction history with confirmation tracking |
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

---
