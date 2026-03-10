### HD Structure

HD path: `m/84'/coin'/mixdepth'/chain/index` (BIP84 P2WPKH, default)

- **Mixdepths**: 5 isolated accounts (0-4)
- **Chains**: External (0) for receiving, Internal (1) for change
- **Index**: Sequential address index
- **Coin type**: 0 for mainnet, 1 for testnet/regtest

### Taproot (P2TR) Support

JoinMarket NG supports BIP86 taproot (P2TR) wallets via the `--address-type p2tr` CLI flag
or the `address_type = "p2tr"` config setting.

**Derivation path:** `m/86'/coin'/mixdepth'/chain/index` (BIP86, key-path only)

The only difference from the default P2WPKH path is the BIP purpose: `86'` instead of `84'`.

**Descriptor format:** `tr(xpub/0/*)` (receive), `tr(xpub/1/*)` (change) --
compared to `wpkh(xpub/0/*)` / `wpkh(xpub/1/*)` for P2WPKH.

**Signing:** P2TR inputs use BIP340 Schnorr signatures (64 bytes, `SIGHASH_DEFAULT`).
The private key is tweaked per BIP341 before signing. The witness stack contains a
single element `[schnorr_sig]`, unlike P2WPKH which has two `[ecdsa_sig, pubkey]`.

**Interoperability:** When a P2TR maker receives a CoinJoin request from a legacy taker
that does not negotiate `address_type`, the maker falls back to P2WPKH addresses. This
ensures backward compatibility -- P2TR makers can serve both taproot-aware and legacy takers.

**Fidelity bonds** always use P2WSH (timelocked `OP_CLTV` scripts), regardless of the
wallet's `address_type`. The derivation branch is `2` (path: `m/purpose'/coin'/0'/2/index`),
where `purpose` follows the wallet type (86 for P2TR, 84 for P2WPKH). The address itself
is always P2WSH because the timelock script requires script-path spending.

### BIP39 Passphrase Support

JoinMarket NG supports the optional BIP39 passphrase ("25th word"):

**Important Distinction:**

- **File encryption password** (`--password`): Encrypts mnemonic file with AES
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

**Bitcoin Core Backend (Legacy):**

- Method: `scantxoutset` RPC (no wallet required)
- Requirements: Bitcoin Core v30+
- Sync: Slow (~90s per scan on mainnet)

Useful for one-off operations without persistent tracking.

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

Neutrino cannot access the mempool, affecting transaction verification:

| Policy | Behavior |
|--------|----------|
| `SELF` | Broadcast via own backend (always verifiable) |
| `RANDOM_PEER` | Try makers sequentially, fall back to self |
| `MULTIPLE_PEERS` | Broadcast to N makers simultaneously (default) |
| `NOT_SELF` | Try makers only, no fallback |

Confirmation monitoring uses block-based UTXO lookups.

### Periodic Wallet Rescan

Both maker and taker support periodic rescanning:

| Setting | Default | Description |
|---------|---------|-------------|
| `rescan_interval_sec` | 600 | How often to rescan |
| `post_coinjoin_rescan_delay` | 60 | Delay after CoinJoin (maker) |

**Maker:** After CoinJoin, rescans to detect balance changes and update offers automatically.

**Taker:** Rescans between schedule entries to track pending confirmations.

---
