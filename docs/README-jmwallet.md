# JoinMarket Wallet Library (jmwallet)

Modern HD wallet for JoinMarket with support for Bitcoin Core nodes and lightweight Neutrino SPV.

## Installation

See [Installation](install.md) for complete installation instructions including:

- Automated installation with `install.sh`
- Virtual environment setup
- Backend setup (Bitcoin Core or Neutrino)

**Quick install** (if you already have the repo):

```bash
cd joinmarket-ng
source jmvenv/bin/activate  # If you used install.sh
# OR create venv: python3 -m venv jmvenv && source jmvenv/bin/activate
cd jmwallet
pip install -e ../jmcore .
```

## Quick Start

### 1. Create or Import a Wallet

**Option A: Generate a new wallet** with password protection:

```bash
# Generate with defaults (saves to ~/.joinmarket-ng/wallets/default.mnemonic with password)
jm-wallet generate

# Or specify a custom name and/or location
jm-wallet generate --output ~/.joinmarket-ng/wallets/my-wallet.mnemonic

# Generate without saving (display only)
jm-wallet generate --no-save --no-prompt-password
```

**IMPORTANT**: The mnemonic is displayed once during generation. Write it down and store it securely offline - it's your only backup if you lose the encrypted file!

**Option B: Import an existing mnemonic** (e.g., migrating from reference implementation):

```bash
# Interactive word-by-word input with Tab completion (saves to ~/.joinmarket-ng/wallets/default.mnemonic with password)
jm-wallet import

# Or pass mnemonic via environment variable (for scripting)
MNEMONIC="your twelve or twenty-four word mnemonic phrase ..." jm-wallet import

# Import a 12-word mnemonic
jm-wallet import --words 12
```

The import command features:

- Tab completion (where readline is available)
- Auto-completion when only one BIP39 word matches your prefix
- Suggestions when multiple words match
- Mnemonic checksum validation

**Note**: By default, the wallet is saved and password-protected. Use `--no-save` to skip saving or `--no-prompt-password` to skip password protection (not recommended for production).

### 2. Choose Your Backend

JoinMarket NG supports three blockchain backends with different trade-offs:

#### Option A: Descriptor Wallet (Recommended - Fast & Efficient)

**Best for**: Running a maker bot or frequent operations with your own Bitcoin Core node.

Uses Bitcoin Core's descriptor wallet feature to persistently track your addresses. After one-time setup, syncs are nearly instant using `listunspent` instead of scanning the entire UTXO set.

**Performance**: ~1 second per sync (vs ~90 seconds with scantxoutset)

**Requirements**: Bitcoin Core v24+ with your own node

**Security Note**: ⚠️ **Never use with a third-party node!** Your wallet addresses are stored in Bitcoin Core's wallet files. Funds are safe, but your addresses and balances are visible to whoever controls the node.

Create an environment file:

```bash
cat > ~/.joinmarket-ng/bitcoin.env << EOF
export BITCOIN__RPC_URL=http://127.0.0.1:8332
export BITCOIN__RPC_USER=your_rpc_user
export BITCOIN__RPC_PASSWORD=your_rpc_password
EOF
chmod 600 ~/.joinmarket-ng/bitcoin.env
```

Check wallet balance (first run will import descriptors):

```bash
source ~/.joinmarket-ng/bitcoin.env
jm-wallet info \
  --mnemonic-file ~/.joinmarket-ng/wallets/my-wallet.mnemonic \
  --backend descriptor_wallet

# Or if using default wallet:
jm-wallet info --backend descriptor_wallet
```

The first run imports your wallet descriptors into Bitcoin Core (one-time ~5 second operation). Subsequent syncs are nearly instant.

#### Option B: Neutrino (Lightweight SPV)

**Best for**: Limited storage or fast initial sync.

Lightweight SPV backend using BIP157/158 compact block filters.

**Storage**: ~500 MB (vs ~900 GB for full node)

**Privacy**: High (downloads filters, not addresses)

Start Neutrino server with Docker:

```bash
docker run -d \
  --name neutrino \
  -p 8334:8334 \
  -v neutrino-data:/data/neutrino \
  -e NETWORK=mainnet \
  -e LOG_LEVEL=info \
  ghcr.io/m0wer/neutrino-api
```

**Note**: Pre-built binaries are also available in the [m0wer/neutrino-api](https://github.com/m0wer/neutrino-api/releases) releases.

Check wallet balance:

```bash
jm-wallet info \
  --mnemonic-file ~/.joinmarket-ng/wallets/my-wallet.mnemonic \
  --backend neutrino

# Or if using default wallet:
jm-wallet info --backend neutrino
```

#### Backend Comparison

| Feature | Descriptor Wallet | Full Node (Legacy) | Neutrino |
|---------|-------------------|-------------------|----------|
| **Sync Speed** | ~1s | ~90s | ~5s |
| **Storage** | ~900 GB | ~900 GB | ~500 MB |
| **Setup** | One-time import | None | External server |
| **Privacy** | High (own node) | High (own node) | High (filters) |
| **Mempool** | ✅ Yes | ✅ Yes | ❌ No |

### 3. View Your Addresses

The wallet info command displays your balance across 5 mixdepths:

```
Total Balance: 10,500,000 sats (0.10500000 BTC)

Balance by mixdepth:
  Mixdepth 0:       5,000,000 sats  |  bc1q...
  Mixdepth 1:       3,000,000 sats  |  bc1q...
  Mixdepth 2:       2,500,000 sats  |  bc1q...
  Mixdepth 3:               0 sats  |  bc1q...
  Mixdepth 4:               0 sats  |  bc1q...
```

**Privacy Note**: Never merge coins across mixdepths outside of CoinJoin!

## CLI Commands

### Generate Wallet

```bash
# Generate and save encrypted wallet to default location (recommended)
jm-wallet generate

# Generate and save to custom name and/or location
jm-wallet generate --output ~/my-wallet.mnemonic

# Just display (not saved - for testing only)
jm-wallet generate --no-save --no-prompt-password

# 12-word mnemonic instead of 24
jm-wallet generate --words 12
```

**Note**:

- Default name and location: `~/.joinmarket-ng/wallets/default.mnemonic`
- By default, the wallet is saved and password-protected
- Use `--no-save` to skip saving, `--no-prompt-password` to skip encryption (not recommended)

### Import Existing Mnemonic

```bash
# Interactive input with Tab completion and auto-complete
jm-wallet import

# Import to specific file
jm-wallet import --output ~/my-wallet.mnemonic

# Pass mnemonic via environment variable (for scripting)
MNEMONIC="word1 word2 ..." jm-wallet import --no-prompt-password

# Import 12-word mnemonic
jm-wallet import --words 12

# Force overwrite existing file
jm-wallet import --force
```

### View Balance

```bash
# Using default wallet
jm-wallet info --backend neutrino

# Using specific wallet file
jm-wallet info --mnemonic-file ~/my-wallet.mnemonic --backend neutrino

# Bitcoin Core (with environment file)
source ~/.joinmarket-ng/bitcoin.env
jm-wallet info --backend descriptor_wallet
```

### List Fidelity Bonds

```bash
# Using default wallet
jm-wallet list-bonds

# Using specific wallet file
jm-wallet list-bonds --mnemonic-file ~/my-wallet.mnemonic
```

## Cold Wallet Fidelity Bonds

For maximum security, fidelity bonds can use a certificate chain that keeps the bond UTXO private key completely offline in a hardware wallet. The bond private key never touches any internet-connected device.

### Workflow

1. **Get public key from Sparrow Wallet**:

   - Open Sparrow Wallet with your hardware wallet
   - Go to Addresses tab
   - Find/create address at path `m/84'/0'/0'/2/0` (fidelity bond path)
   - Right-click the address and select "Copy Public Key"

2. **Create bond address** (online - NO private keys needed):

   ```bash
   jm-wallet create-bond-address <pubkey_from_step_1> \
     --locktime-date "2026-01"
   ```
   Fund this address with Bitcoin to create the bond.

3. **Generate hot wallet keypair** (on online machine):

   ```bash
   jm-wallet generate-hot-keypair
   ```
   Save both the private and public keys securely.

4. **Prepare certificate message** (online - NO private keys needed):
   ```bash
   jm-wallet prepare-certificate-message <bond_address> \
     --cert-pubkey <hot_pubkey_from_step_3> \
     --cert-expiry-blocks 104832
   ```

5. **Sign with hardware wallet** (using Sparrow):
   - Open Sparrow Wallet and connect your hardware wallet
   - Go to Tools -> Sign/Verify Message
   - Select the address matching your bond's public key
   - Paste the hex message from step 4
   - Sign and copy the signature

6. **Import certificate** (on online machine):
   ```bash
   jm-wallet import-certificate <bond_address> \
     --cert-pubkey <hot_pubkey_from_step_3> \
     --cert-privkey <hot_privkey_from_step_3> \
     --cert-signature <signature_from_hardware_wallet> \
     --cert-expiry 52
   ```

7. **Run maker** - it will automatically use the certificate.

### Security Benefits

- **Complete cold storage**: Bond private keys NEVER leave the hardware wallet
- **No mnemonic exposure**: No mnemonics or private keys needed on online systems
- **Public key only**: Bond address created from public key extracted from Sparrow
- **Time-limited**: Certificate expires after ~2 years (configurable)
- **Revocable**: If hot wallet is compromised, only the certificate is at risk, not the bond funds
- **Renewable**: Sign a new message when the certificate expires

See [Technical Documentation](technical/index.md) for detailed documentation.

### All Commands

For detailed help on any command, see the auto-generated help sections below.

<!-- AUTO-GENERATED HELP START: jm-wallet -->

<details>
<summary><code>jm-wallet --help</code></summary>

```

 Usage: jm-wallet [OPTIONS] COMMAND [ARGS]...

 JoinMarket Wallet Management

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ list-bonds                    List all fidelity bonds in the wallet.         │
│ generate-bond-address         Generate a fidelity bond (timelocked P2WSH)    │
│                               address.                                       │
│ recover-bonds                 Recover fidelity bonds by scanning all 960     │
│                               possible timelocks.                            │
│ create-bond-address           Create a fidelity bond address from a public   │
│                               key (cold wallet workflow).                    │
│ generate-hot-keypair          Generate a hot wallet keypair for fidelity     │
│                               bond certificates.                             │
│ prepare-certificate-message   Prepare certificate message for signing with   │
│                               hardware wallet (cold wallet support).         │
│ import-certificate            Import a certificate signature for a fidelity  │
│                               bond (cold wallet support).                    │
│ spend-bond                    Generate a PSBT to spend a cold storage        │
│                               fidelity bond after locktime expires.          │
│ freeze                        Interactively freeze/unfreeze UTXOs to exclude │
│                               them from coin selection.                      │
│ history                       View CoinJoin transaction history.             │
│ registry-list                 List all fidelity bonds in the registry.       │
│ registry-show                 Show detailed information about a specific     │
│                               fidelity bond.                                 │
│ registry-sync                 Sync fidelity bond funding status from the     │
│                               blockchain.                                    │
│ send                          Send a simple transaction from wallet to an    │
│                               address.                                       │
│ import                        Import an existing BIP39 mnemonic phrase to    │
│                               create/recover a wallet.                       │
│ generate                      Generate a new BIP39 mnemonic phrase with      │
│                               secure entropy.                                │
│ info                          Display wallet information and balances by     │
│                               mixdepth.                                      │
│ validate                      Validate a BIP39 mnemonic phrase.              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet list-bonds --help</code></summary>

```

 Usage: jm-wallet list-bonds [OPTIONS]

 List all fidelity bonds in the wallet.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH                                      │
│ --prompt-bip39-passphrase                   Prompt for BIP39 passphrase      │
│ --network                  -n      TEXT     Bitcoin network                  │
│ --backend                  -b      TEXT     Backend: scantxoutset |          │
│                                             descriptor_wallet | neutrino     │
│ --rpc-url                          TEXT     [env var: BITCOIN_RPC_URL]       │
│ --locktime                 -L      INTEGER  Locktime(s) to scan for          │
│ --log-level                -l      TEXT     Log level                        │
│ --help                                      Show this message and exit.      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet generate-bond-address --help</code></summary>

```

 Usage: jm-wallet generate-bond-address [OPTIONS]

 Generate a fidelity bond (timelocked P2WSH) address.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH                                      │
│ --prompt-bip39-passphrase                   Prompt for BIP39 passphrase      │
│ --locktime                 -L      INTEGER  Locktime as Unix timestamp       │
│                                             [default: 0]                     │
│ --locktime-date            -d      TEXT     Locktime as YYYY-MM (must be 1st │
│                                             of month)                        │
│ --index                    -i      INTEGER  Address index [default: 0]       │
│ --network                  -n      TEXT                                      │
│ --data-dir                         PATH     Data directory (default:         │
│                                             ~/.joinmarket-ng or              │
│                                             $JOINMARKET_DATA_DIR)            │
│ --no-save                                   Do not save the bond to the      │
│                                             registry                         │
│ --log-level                -l      TEXT     Log level                        │
│ --help                                      Show this message and exit.      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet recover-bonds --help</code></summary>

```

 Usage: jm-wallet recover-bonds [OPTIONS]

 Recover fidelity bonds by scanning all 960 possible timelocks.

 This command scans the blockchain for fidelity bonds at all valid
 timenumber locktimes (Jan 2020 through Dec 2099). Use this when
 recovering a wallet from mnemonic and you don't know which locktimes
 were used for fidelity bonds.

 The scan checks address index 0 by default (most wallets only use index 0).
 Use --max-index to scan more addresses per locktime if needed.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH                                      │
│ --prompt-bip39-passphrase                   Prompt for BIP39 passphrase      │
│ --network                  -n      TEXT     Bitcoin network                  │
│ --backend                  -b      TEXT     Backend: scantxoutset |          │
│                                             descriptor_wallet | neutrino     │
│ --rpc-url                          TEXT     [env var: BITCOIN_RPC_URL]       │
│ --neutrino-url                     TEXT     [env var: NEUTRINO_URL]          │
│ --max-index                -i      INTEGER  Max address index per locktime   │
│                                             to scan (default 1)              │
│                                             [default: 1]                     │
│ --data-dir                         PATH     Data directory (default:         │
│                                             ~/.joinmarket-ng or              │
│                                             $JOINMARKET_DATA_DIR)            │
│ --log-level                -l      TEXT     Log level                        │
│ --help                                      Show this message and exit.      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet create-bond-address --help</code></summary>

```

 Usage: jm-wallet create-bond-address [OPTIONS] PUBKEY

 Create a fidelity bond address from a public key (cold wallet workflow).

 This command creates a timelocked P2WSH bond address from a public key WITHOUT
 requiring your mnemonic or private keys. Use this for true cold storage
 security.

 WORKFLOW:
 1. Use Sparrow Wallet (or similar) with your hardware wallet
 2. Navigate to your wallet's receive addresses
 3. Find or create an address at the fidelity bond derivation path
 (m/84'/0'/0'/2/0)
 4. Copy the public key from the address details
 5. Use this command with the public key to create the bond address
 6. Fund the bond address from any wallet
 7. Use 'prepare-certificate-message' and hardware wallet signing for
 certificates

 Your hardware wallet never needs to be connected to this online tool.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    pubkey      TEXT  Public key (hex, 33 bytes compressed) [required]      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --locktime       -L      INTEGER  Locktime as Unix timestamp [default: 0]    │
│ --locktime-date  -d      TEXT     Locktime as date (YYYY-MM, must be 1st of  │
│                                   month)                                     │
│ --network        -n      TEXT     [default: mainnet]                         │
│ --data-dir               PATH     Data directory (default: ~/.joinmarket-ng  │
│                                   or $JOINMARKET_DATA_DIR)                   │
│ --no-save                         Do not save the bond to the registry       │
│ --log-level      -l      TEXT     [default: INFO]                            │
│ --help                            Show this message and exit.                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet generate-hot-keypair --help</code></summary>

```

 Usage: jm-wallet generate-hot-keypair [OPTIONS]

 Generate a hot wallet keypair for fidelity bond certificates.

 This generates a random keypair that will be used for signing nick messages
 in the fidelity bond proof. The private key stays in the hot wallet, while
 the public key is used to create a certificate signed by the cold wallet.

 The certificate chain is:
   UTXO keypair (cold) -> signs -> certificate (hot) -> signs -> nick proofs

 If --bond-address is provided, the keypair is saved to the bond registry
 and will be automatically used when importing the certificate.

 SECURITY:
 - The hot wallet private key should be stored securely
 - If compromised, an attacker can impersonate your bond until cert expires
 - But they CANNOT spend your bond funds (those remain in cold storage)

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --bond-address        TEXT  Bond address to associate keypair with (saves to │
│                             registry)                                        │
│ --data-dir            PATH  Data directory (default: ~/.joinmarket-ng or     │
│                             $JOINMARKET_DATA_DIR)                            │
│ --log-level           TEXT  [default: INFO]                                  │
│ --help                      Show this message and exit.                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet prepare-certificate-message --help</code></summary>

```

 Usage: jm-wallet prepare-certificate-message [OPTIONS] BOND_ADDRESS

 Prepare certificate message for signing with hardware wallet (cold wallet
 support).

 This generates the message that needs to be signed by the bond UTXO's private
 key.
 The message can then be signed using a hardware wallet via tools like Sparrow
 Wallet.

 IMPORTANT: This command does NOT require your mnemonic or private keys.
 It only prepares the message that you will sign with your hardware wallet.

 If --cert-pubkey is not provided and the bond already has a hot keypair saved
 in the registry (from generate-hot-keypair --bond-address), it will be used.

 The certificate message format for Sparrow is plain ASCII text:
   "fidelity-bond-cert|<cert_pubkey_hex>|<cert_expiry>"

 Where cert_expiry is the ABSOLUTE period number (current_period +
 validity_periods).
 The reference implementation validates that current_block < cert_expiry *
 2016.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    bond_address      TEXT  Bond P2WSH address [required]                   │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --cert-pubkey             TEXT     Certificate public key (hex)              │
│ --validity-periods        INTEGER  Certificate validity in 2016-block        │
│                                    periods from now (1=~2wk, 52=~2yr)        │
│                                    [default: 52]                             │
│ --data-dir                PATH     Data directory (default: ~/.joinmarket-ng │
│                                    or $JOINMARKET_DATA_DIR)                  │
│ --mempool-api             TEXT     Mempool API URL for fetching block height │
│                                    [default: https://mempool.space/api]      │
│ --log-level               TEXT     [default: INFO]                           │
│ --help                             Show this message and exit.               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet import-certificate --help</code></summary>

```

 Usage: jm-wallet import-certificate [OPTIONS] ADDRESS

 Import a certificate signature for a fidelity bond (cold wallet support).

 This imports a certificate generated with 'prepare-certificate-message' into
 the
 bond registry, allowing the hot wallet to use it for making offers.

 IMPORTANT: The --cert-expiry value must match EXACTLY what was used in
 prepare-certificate-message. This is an ABSOLUTE period number, not a
 duration.

 If --cert-pubkey and --cert-privkey are not provided, they will be loaded from
 the bond registry (from a previous 'generate-hot-keypair --bond-address'
 call).

 The signature should be the base64 output from Sparrow's message signing tool,
 using the 'Standard (Electrum)' format.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    address      TEXT  Bond address [required]                              │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --cert-pubkey              TEXT     Certificate pubkey (hex)                 │
│ --cert-privkey             TEXT     Certificate private key (hex)            │
│ --cert-signature           TEXT     Certificate signature (base64)           │
│ --cert-expiry              INTEGER  Certificate expiry as ABSOLUTE period    │
│                                     number (from                             │
│                                     prepare-certificate-message)             │
│                                     [default: 0]                             │
│ --data-dir                 PATH     Data directory (default:                 │
│                                     ~/.joinmarket-ng or                      │
│                                     $JOINMARKET_DATA_DIR)                    │
│ --skip-verification                 Skip signature verification (not         │
│                                     recommended)                             │
│ --mempool-api              TEXT     Mempool API URL for fetching block       │
│                                     height                                   │
│                                     [default: https://mempool.space/api]     │
│ --log-level                TEXT     [default: INFO]                          │
│ --help                              Show this message and exit.              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet spend-bond --help</code></summary>

```

 Usage: jm-wallet spend-bond [OPTIONS] BOND_ADDRESS DESTINATION

 Generate a PSBT to spend a cold storage fidelity bond after locktime expires.

 This creates a Partially Signed Bitcoin Transaction (PSBT) that can be signed
 using HWI (hardware wallet) or the mnemonic signing script (software wallet).

 The PSBT includes the witness script (CLTV timelock) needed to spend the bond.

 REQUIREMENTS:
 - The bond must exist in the registry (created with 'create-bond-address')
 - The bond must be funded (use 'registry-sync' to update UTXO info)
 - The locktime must have expired (or be close enough for your use case)

 SIGNING OPTIONS:

 A) Hardware wallet (HWI):
 1. Run this command with --master-fingerprint and --derivation-path
 2. Install HWI: pip install hwi
 3. Connect and unlock your hardware wallet
 4. Run: python scripts/sign_bond_psbt.py <psbt_base64>

 B) Mnemonic (software signing):
 1. Run: python scripts/sign_bond_mnemonic.py <psbt_base64>
 2. Enter your BIP39 mnemonic when prompted (hidden input)
 3. Broadcast: bitcoin-cli sendrawtransaction <signed_hex>

 The --master-fingerprint and --derivation-path flags embed BIP32 key origin
 info into the PSBT, allowing HWI to identify which key to use on the device.
 The mnemonic script can also use BIP32 info from the PSBT, or accept a
 --derivation-path argument directly.

 NOTE: Sparrow Wallet cannot sign CLTV timelock scripts (P2WSH with custom
 witness scripts). Use one of the signing options above.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    bond_address      TEXT  Bond P2WSH address to spend [required]          │
│ *    destination       TEXT  Destination address for the funds [required]    │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --fee-rate            -f      FLOAT  Fee rate in sat/vB [default: 1.0]       │
│ --master-fingerprint  -m      TEXT   Master key fingerprint (4 bytes hex,    │
│                                      e.g. 'aabbccdd'). Found in Sparrow:     │
│                                      Settings -> Keystore -> Master          │
│                                      fingerprint. Enables Sparrow and HWI to │
│                                      identify the signing key.               │
│ --derivation-path     -p      TEXT   BIP32 derivation path of the key used   │
│                                      for the bond (e.g. "m/84'/0'/0'/0/0").  │
│                                      This is the path of the address whose   │
│                                      pubkey was used in                      │
│                                      'create-bond-address'. Check Sparrow:   │
│                                      Addresses tab -> right-click the        │
│                                      address -> Copy -> Derivation Path.     │
│ --output              -o      PATH   Save PSBT to file (default: stdout      │
│                                      only)                                   │
│ --data-dir                    PATH   Data directory (default:                │
│                                      ~/.joinmarket-ng or                     │
│                                      $JOINMARKET_DATA_DIR)                   │
│ --log-level           -l      TEXT   [default: INFO]                         │
│ --help                               Show this message and exit.             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet freeze --help</code></summary>

```

 Usage: jm-wallet freeze [OPTIONS]

 Interactively freeze/unfreeze UTXOs to exclude them from coin selection.

 Opens a TUI where you can toggle the frozen state of individual UTXOs.
 Frozen UTXOs are persisted in BIP-329 format and excluded from all
 automatic coin selection (taker, maker, and sweep operations).
 Changes take effect immediately on each toggle.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH     Path to mnemonic file            │
│ --prompt-bip39-passphrase                   Prompt for BIP39 passphrase      │
│                                             interactively                    │
│ --network                  -n      TEXT     Bitcoin network                  │
│ --backend                  -b      TEXT     Backend: scantxoutset |          │
│                                             descriptor_wallet | neutrino     │
│ --rpc-url                          TEXT     [env var: BITCOIN_RPC_URL]       │
│ --neutrino-url                     TEXT     [env var: NEUTRINO_URL]          │
│ --mixdepth                 -m      INTEGER  Filter to a specific mixdepth    │
│                                             (0-4)                            │
│ --data-dir                         PATH     Data directory (default:         │
│                                             ~/.joinmarket-ng or              │
│                                             $JOINMARKET_DATA_DIR)            │
│ --log-level                -l      TEXT     Log level                        │
│ --help                                      Show this message and exit.      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet history --help</code></summary>

```

 Usage: jm-wallet history [OPTIONS]

 View CoinJoin transaction history.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --limit     -n      INTEGER  Max entries to show                             │
│ --role      -r      TEXT     Filter by role (maker/taker)                    │
│ --stats     -s               Show statistics only                            │
│ --csv                        Output as CSV                                   │
│ --data-dir          PATH     Data directory (default: ~/.joinmarket-ng or    │
│                              $JOINMARKET_DATA_DIR)                           │
│ --help                       Show this message and exit.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet registry-list --help</code></summary>

```

 Usage: jm-wallet registry-list [OPTIONS]

 List all fidelity bonds in the registry.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --data-dir             PATH  Data directory (default: ~/.joinmarket-ng or    │
│                              $JOINMARKET_DATA_DIR)                           │
│ --funded-only  -f            Show only funded bonds                          │
│ --active-only  -a            Show only active (funded & not expired) bonds   │
│ --json         -j            Output as JSON                                  │
│ --log-level    -l      TEXT  [default: WARNING]                              │
│ --help                       Show this message and exit.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet registry-show --help</code></summary>

```

 Usage: jm-wallet registry-show [OPTIONS] ADDRESS

 Show detailed information about a specific fidelity bond.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    address      TEXT  Bond address to show [required]                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --data-dir           PATH  Data directory (default: ~/.joinmarket-ng or      │
│                            $JOINMARKET_DATA_DIR)                             │
│ --json       -j            Output as JSON                                    │
│ --log-level  -l      TEXT  [default: WARNING]                                │
│ --help                     Show this message and exit.                       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet registry-sync --help</code></summary>

```

 Usage: jm-wallet registry-sync [OPTIONS]

 Sync fidelity bond funding status from the blockchain.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH                                      │
│ --prompt-bip39-passphrase                Prompt for BIP39 passphrase         │
│ --network                  -n      TEXT                                      │
│ --backend                  -b      TEXT  Backend: scantxoutset |             │
│                                          descriptor_wallet | neutrino        │
│ --rpc-url                          TEXT  [env var: BITCOIN_RPC_URL]          │
│ --neutrino-url                     TEXT  [env var: NEUTRINO_URL]             │
│ --data-dir                         PATH  Data directory (default:            │
│                                          ~/.joinmarket-ng or                 │
│                                          $JOINMARKET_DATA_DIR)               │
│ --log-level                -l      TEXT  Log level                           │
│ --help                                   Show this message and exit.         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet send --help</code></summary>

```

 Usage: jm-wallet send [OPTIONS] DESTINATION

 Send a simple transaction from wallet to an address.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    destination      TEXT  Destination address [required]                   │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --amount                   -a      INTEGER  Amount in sats (0 for sweep)     │
│                                             [default: 0]                     │
│ --mnemonic-file            -f      PATH                                      │
│ --prompt-bip39-passphrase                   Prompt for BIP39 passphrase      │
│ --mixdepth                 -m      INTEGER  Source mixdepth [default: 0]     │
│ --fee-rate                         FLOAT    Manual fee rate in sat/vB (e.g.  │
│                                             1.5). Mutually exclusive with    │
│                                             --block-target. Defaults to      │
│                                             3-block estimation.              │
│ --block-target                     INTEGER  Target blocks for fee estimation │
│                                             (1-1008). Defaults to 3.         │
│ --network                  -n      TEXT     Bitcoin network                  │
│ --backend                  -b      TEXT     Backend: scantxoutset |          │
│                                             descriptor_wallet | neutrino     │
│ --rpc-url                          TEXT     [env var: BITCOIN_RPC_URL]       │
│ --neutrino-url                     TEXT     [env var: NEUTRINO_URL]          │
│ --broadcast                                 Broadcast the transaction        │
│                                             [default: True]                  │
│ --yes                      -y               Skip confirmation prompt         │
│ --select-utxos             -s               Interactively select UTXOs       │
│                                             (fzf-like TUI)                   │
│ --data-dir                         PATH     Data directory (default:         │
│                                             ~/.joinmarket-ng or              │
│                                             $JOINMARKET_DATA_DIR)            │
│ --log-level                -l      TEXT     Log level                        │
│ --help                                      Show this message and exit.      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet import --help</code></summary>

```

 Usage: jm-wallet import [OPTIONS]

 Import an existing BIP39 mnemonic phrase to create/recover a wallet.

 Enter your existing mnemonic interactively with autocomplete support,
 or pass it via the MNEMONIC environment variable.

 By default, saves to ~/.joinmarket-ng/wallets/default.mnemonic with password
 protection.

 Examples:
     jm-wallet import                          # Interactive input, 24 words
     jm-wallet import --words 12               # Interactive input, 12 words
     MNEMONIC="word1 word2 ..." jm-wallet import  # Via environment variable
     jm-wallet import -o my-wallet.mnemonic    # Custom output file

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --words            -w                          INTEGER  Number of words (12, │
│                                                         15, 18, 21, or 24)   │
│                                                         [default: 24]        │
│ --output           -o                          PATH     Output file path     │
│ --prompt-password      --no-prompt-password             Prompt for password  │
│                                                         interactively        │
│                                                         (default: prompt)    │
│                                                         [default:            │
│                                                         prompt-password]     │
│ --force            -f                                   Overwrite existing   │
│                                                         file without         │
│                                                         confirmation         │
│ --help                                                  Show this message    │
│                                                         and exit.            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet generate --help</code></summary>

```

 Usage: jm-wallet generate [OPTIONS]

 Generate a new BIP39 mnemonic phrase with secure entropy.

 By default, saves to ~/.joinmarket-ng/wallets/default.mnemonic with password
 protection.
 Use --no-save to only display the mnemonic without saving.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --words            -w                          INTEGER  Number of words (12, │
│                                                         15, 18, 21, or 24)   │
│                                                         [default: 24]        │
│ --save                 --no-save                        Save to file         │
│                                                         (default: save)      │
│                                                         [default: save]      │
│ --output           -o                          PATH     Output file path     │
│ --prompt-password      --no-prompt-password             Prompt for password  │
│                                                         interactively        │
│                                                         (default: prompt)    │
│                                                         [default:            │
│                                                         prompt-password]     │
│ --help                                                  Show this message    │
│                                                         and exit.            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet info --help</code></summary>

```

 Usage: jm-wallet info [OPTIONS]

 Display wallet information and balances by mixdepth.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH     Path to mnemonic file            │
│ --prompt-bip39-passphrase                   Prompt for BIP39 passphrase      │
│                                             interactively                    │
│ --network                  -n      TEXT     Bitcoin network                  │
│ --backend                  -b      TEXT     Backend: scantxoutset |          │
│                                             descriptor_wallet | neutrino     │
│ --rpc-url                          TEXT     [env var: BITCOIN_RPC_URL]       │
│ --neutrino-url                     TEXT     [env var: NEUTRINO_URL]          │
│ --extended                 -e               Show detailed address view with  │
│                                             derivations                      │
│ --gap                      -g      INTEGER  Max address gap to show in       │
│                                             extended view                    │
│                                             [default: 6]                     │
│ --data-dir                         PATH     Data directory (default:         │
│                                             ~/.joinmarket-ng or              │
│                                             $JOINMARKET_DATA_DIR)            │
│ --log-level                -l      TEXT     Log level                        │
│ --help                                      Show this message and exit.      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet validate --help</code></summary>

```

 Usage: jm-wallet validate [OPTIONS] [MNEMONIC_ARG]

 Validate a BIP39 mnemonic phrase.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   mnemonic_arg      [MNEMONIC_ARG]  Mnemonic to validate                     │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file  -f      PATH  Path to mnemonic file                         │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<!-- AUTO-GENERATED HELP END: jm-wallet -->
