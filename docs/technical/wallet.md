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
- **Not read from config file** to prevent accidental exposure

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

### Multiple Wallets in One Data Directory

JoinMarket-NG records every CoinJoin (as taker or maker) in a single
`history.csv` file inside the data directory (legacy installs may still
have it under the old name `coinjoin_history.csv`; the wallet renames it
in place on first read). Each row is tagged with
the BIP32 master fingerprint (`wallet_fingerprint`, first 4 bytes of `m/0`),
so commands like `jm-wallet history` and `jm-wallet info` filter to the
correct wallet automatically when a mnemonic is supplied.

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

---
