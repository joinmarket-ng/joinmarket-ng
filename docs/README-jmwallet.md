# JoinMarket Wallet Library (jmwallet)

Modern HD wallet for JoinMarket with support for Bitcoin Core and Neutrino backends.

## Installation

Use the [Installation guide](install.md) for setup, backend configuration, and Tor notes.

## Quick Start

### 1) Create or import a wallet

```bash
# Create a new encrypted wallet (default path)
jm-wallet generate

# Import an existing mnemonic interactively
jm-wallet import
```

The mnemonic is shown once during generation. Store it offline; it is your wallet backup.

### 2) Check balances and addresses

```bash
jm-wallet info
```

JoinMarket uses 5 mixdepths. Keep mixdepths isolated and avoid merging across mixdepths outside CoinJoin.

### 3) Send funds

```bash
# Sweep amount=0, otherwise set sats with --amount
jm-wallet send <destination_address> --amount 100000
```

Use `--select-utxos` on `jm-wallet send` for manual coin control.

## Backends

Configure backend in `~/.joinmarket-ng/config.toml` (details in [Installation](install.md#configure-backend)).

- `descriptor_wallet` (recommended): fast repeated sync with your own Bitcoin Core node.
- `scantxoutset`: no Core wallet import, slower scans.
- `neutrino`: lightweight setup with compact filters.

Security note: only use `descriptor_wallet` with a node you control.

For backend internals and tradeoffs, see [Technical Wallet Notes](technical/wallet.md#backend-systems).

## Fidelity Bonds

Wallet commands support generating, listing, recovering, certifying, and spending fidelity bonds.

- Concepts and wire-level details: [Technical Privacy Notes](technical/privacy.md#fidelity-bonds)
- Cold-wallet workflow and hardware-wallet caveats: [Cold Wallet Setup](technical/privacy.md#cold-wallet-setup)

## Command Help

The full CLI reference below is auto-generated from command `--help` output.

<!-- AUTO-GENERATED HELP START: jm-wallet -->

<details>
<summary><code>jm-wallet --help</code></summary>

```

 Usage: jm-wallet [OPTIONS] COMMAND [ARGS]...

 JoinMarket Wallet Management

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --install-completion          Install completion for the current shell.      │
│ --show-completion             Show completion for the current shell, to copy │
│                               it or customize the installation.              │
│ --help                        Show this message and exit.                    │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ list-bonds                   List all fidelity bonds in the wallet.          │
│ generate-bond-address        Generate a fidelity bond (timelocked P2WSH)     │
│                              address.                                        │
│ import-bond                  Manually import a fidelity bond into the        │
│                              registry.                                       │
│ recover-bonds                Recover fidelity bonds by scanning all 960      │
│                              possible timelocks.                             │
│ create-bond-address          Create a fidelity bond address from a public    │
│                              key (cold wallet workflow).                     │
│ generate-hot-keypair         Generate a hot wallet keypair for fidelity bond │
│                              certificates.                                   │
│ prepare-certificate-message  Prepare certificate message for signing with    │
│                              hardware wallet (cold wallet support).          │
│ import-certificate           Import a certificate signature for a fidelity   │
│                              bond (cold wallet support).                     │
│ spend-bond                   Generate a PSBT to spend a cold storage         │
│                              fidelity bond after locktime expires.           │
│ debug-info                   Print privacy-friendly diagnostic information   │
│                              for troubleshooting.                            │
│ freeze                       Interactively freeze/unfreeze UTXOs to exclude  │
│                              them from coin selection.                       │
│ history                      View CoinJoin transaction history.              │
│ registry-show                Show detailed information about a specific      │
│                              fidelity bond.                                  │
│ send                         Send a simple transaction from wallet to an     │
│                              address.                                        │
│ import                       Import an existing BIP39 mnemonic phrase to     │
│                              create/recover a wallet.                        │
│ generate                     Generate a new BIP39 mnemonic phrase with       │
│                              secure entropy.                                 │
│ info                         Display wallet information and balances by      │
│                              mixdepth.                                       │
│ verify-password              Verify that a password can decrypt an encrypted │
│                              mnemonic file.                                  │
│ validate                     Validate a mnemonic phrase.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet list-bonds --help</code></summary>

```

 Usage: jm-wallet list-bonds [OPTIONS]

 List all fidelity bonds in the wallet.

 Without --mnemonic-file: shows bonds from the local registry (offline, fast).
 With --mnemonic-file: scans the blockchain for bonds and updates the registry.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH     [env var: MNEMONIC_FILE]         │
│ --prompt-bip39-passphrase                   Prompt for BIP39 passphrase      │
│ --network                  -n      TEXT     Bitcoin network                  │
│ --backend                  -b      TEXT     Backend: scantxoutset |          │
│                                             descriptor_wallet | neutrino     │
│ --rpc-url                          TEXT     [env var: BITCOIN_RPC_URL]       │
│ --locktime                 -L      INTEGER  Locktime(s) to scan for          │
│ --data-dir                         PATH     Data directory (default:         │
│                                             ~/.joinmarket-ng or              │
│                                             $JOINMARKET_DATA_DIR)            │
│ --funded-only                               Show only funded bonds (offline  │
│                                             mode)                            │
│ --active-only                               Show only active bonds (offline  │
│                                             mode)                            │
│ --json                     -j               Output as JSON (offline mode)    │
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
│ --mnemonic-file            -f      PATH     [env var: MNEMONIC_FILE]         │
│ --prompt-bip39-passphrase                   Prompt for BIP39 passphrase      │
│ --locktime                 -L      INTEGER  Locktime as Unix timestamp       │
│                                             [default: 0]                     │
│ --locktime-date            -d      TEXT     Locktime as YYYY-MM (must be 1st │
│                                             of month)                        │
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
<summary><code>jm-wallet import-bond --help</code></summary>

```

 Usage: jm-wallet import-bond [OPTIONS]

 Manually import a fidelity bond into the registry.

 Use this when you know the exact derivation path and locktime of a bond
 that was not discovered automatically. The bond address and keys are
 derived from your mnemonic.

 Examples:
     jm-wallet import-bond --locktime-date 2026-02
     jm-wallet import-bond --path "m/84'/0'/0'/2/73:1740787200"
     jm-wallet import-bond --timenumber 73

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH     [env var: MNEMONIC_FILE]         │
│ --prompt-bip39-passphrase                   Prompt for BIP39 passphrase      │
│ --locktime                 -L      INTEGER  Locktime as Unix timestamp       │
│                                             [default: 0]                     │
│ --locktime-date            -d      TEXT     Locktime as YYYY-MM (must be 1st │
│                                             of month)                        │
│ --timenumber               -t      INTEGER  Timenumber (0-959). Auto-derived │
│                                             if omitted.                      │
│ --path                     -p      TEXT     Full derivation path with        │
│                                             locktime, e.g.                   │
│                                             m/84'/0'/0'/2/73:1740787200      │
│ --network                  -n      TEXT                                      │
│ --data-dir                         PATH     Data directory (default:         │
│                                             ~/.joinmarket-ng or              │
│                                             $JOINMARKET_DATA_DIR)            │
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

 Each timenumber (0-959) maps to exactly one address, matching the
 reference JoinMarket implementation.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH  [env var: MNEMONIC_FILE]            │
│ --prompt-bip39-passphrase                Prompt for BIP39 passphrase         │
│ --network                  -n      TEXT  Bitcoin network                     │
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
│ --cert-pubkey               TEXT     Certificate public key (hex)            │
│ --validity-periods          INTEGER  Certificate validity in 2016-block      │
│                                      periods from now (1=~2wk, 52=~2yr)      │
│                                      [default: 52]                           │
│ --data-dir                  PATH     Data directory (default:                │
│                                      ~/.joinmarket-ng or                     │
│                                      $JOINMARKET_DATA_DIR)                   │
│ --network           -n      TEXT     Bitcoin network                         │
│ --backend           -b      TEXT     Backend: scantxoutset |                 │
│                                      descriptor_wallet | neutrino            │
│ --rpc-url                   TEXT     [env var: BITCOIN_RPC_URL]              │
│ --neutrino-url              TEXT     [env var: NEUTRINO_URL]                 │
│ --mempool-api               TEXT     Mempool API URL for fetching block      │
│                                      height. Only used when no Bitcoin node  │
│                                      backend is configured. Example:         │
│                                      http://localhost:8999/api               │
│ --current-block             INTEGER  Current block height override for       │
│                                      offline/air-gapped workflows. Skips all │
│                                      network block-height lookups.           │
│ --log-level                 TEXT     [default: INFO]                         │
│ --help                               Show this message and exit.             │
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

 If --cert-pubkey is not provided, it will be loaded from the bond registry.
 The certificate private key is loaded from the bond registry, or requested via
 an interactive hidden prompt if unavailable there.

 The signature should be the base64 output from Sparrow's message signing tool,
 using the 'Standard (Electrum)' format.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    address      TEXT  Bond address [required]                              │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --cert-pubkey                TEXT     Certificate pubkey (hex)               │
│ --cert-signature             TEXT     Certificate signature (base64)         │
│ --cert-expiry                INTEGER  Certificate expiry as ABSOLUTE period  │
│                                       number (from                           │
│                                       prepare-certificate-message)           │
│                                       [default: 0]                           │
│ --data-dir                   PATH     Data directory (default:               │
│                                       ~/.joinmarket-ng or                    │
│                                       $JOINMARKET_DATA_DIR)                  │
│ --skip-verification                   Skip signature verification (not       │
│                                       recommended)                           │
│ --network            -n      TEXT     Bitcoin network                        │
│ --backend            -b      TEXT     Backend: scantxoutset |                │
│                                       descriptor_wallet | neutrino           │
│ --rpc-url                    TEXT     [env var: BITCOIN_RPC_URL]             │
│ --neutrino-url               TEXT     [env var: NEUTRINO_URL]                │
│ --mempool-api                TEXT     Mempool API URL for validating cert    │
│                                       expiry. Only used when no Bitcoin node │
│                                       backend is configured. Example:        │
│                                       http://localhost:8999/api              │
│ --current-block              INTEGER  Current block height override for      │
│                                       offline/air-gapped workflows. Skips    │
│                                       all network block-height lookups.      │
│ --log-level                  TEXT     [default: INFO]                        │
│ --help                                Show this message and exit.            │
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
 - The bond must be funded (use 'registry-sync' to update UTXO info),
   unless using --test-unfunded for a dry-run signer test
 - The locktime must have expired (or be close enough for your use case)

 SIGNING:

 Most hardware wallets (Trezor, Coldcard, BitBox02, KeepKey) CANNOT sign
 CLTV timelock P2WSH scripts -- their firmware rejects custom witness
 scripts. Ledger and Blockstream Jade DO support arbitrary witness scripts
 and may work via HWI (scripts/sign_bond_psbt.py).

 Option A - Mnemonic signing (works with any device):
 1. Run: python scripts/sign_bond_mnemonic.py <psbt_base64>
 2. Enter your BIP39 mnemonic when prompted (hidden input)
 3. Broadcast: bitcoin-cli sendrawtransaction <signed_hex>

 Option B - HWI signing (Ledger and Jade only):
 1. Install HWI: pip install -U hwi
 2. Connect and unlock your hardware wallet
 3. Run: python scripts/sign_bond_psbt.py <psbt_base64>

 See docs/technical/privacy.md for strategies to reduce mnemonic exposure
 (dedicated BIP39 passphrase, BIP-85 derived keys, air-gapped signing).

 NOTE: Sparrow Wallet also cannot sign CLTV timelock scripts.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    bond_address      TEXT  Bond P2WSH address to spend [required]          │
│ *    destination       TEXT  Destination address for the funds [required]    │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --fee-rate            -f      FLOAT    Fee rate in sat/vB [default: 1.0]     │
│ --master-fingerprint  -m      TEXT     Master key fingerprint (4 bytes hex,  │
│                                        e.g. 'aabbccdd'). Found in Sparrow:   │
│                                        Settings -> Keystore -> Master        │
│                                        fingerprint. Enables Sparrow and HWI  │
│                                        to identify the signing key.          │
│ --derivation-path     -p      TEXT     BIP32 derivation path of the key used │
│                                        for the bond (e.g.                    │
│                                        "m/84'/0'/0'/0/0"). This is the path  │
│                                        of the address whose pubkey was used  │
│                                        in 'create-bond-address'. Check       │
│                                        Sparrow: Addresses tab -> right-click │
│                                        the address -> Copy -> Derivation     │
│                                        Path.                                 │
│ --output              -o      PATH     Save PSBT to file (default: stdout    │
│                                        only)                                 │
│ --test-unfunded                        Allow generating a test PSBT even     │
│                                        when the bond is unfunded, using a    │
│                                        synthetic UTXO for signer             │
│                                        compatibility testing.                │
│ --test-utxo-value             INTEGER  Synthetic UTXO value in sats when     │
│                                        using --test-unfunded (default:       │
│                                        100000).                              │
│                                        [default: 100000]                     │
│ --data-dir                    PATH     Data directory (default:              │
│                                        ~/.joinmarket-ng or                   │
│                                        $JOINMARKET_DATA_DIR)                 │
│ --log-level           -l      TEXT     [default: INFO]                       │
│ --help                                 Show this message and exit.           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet debug-info --help</code></summary>

```

 Usage: jm-wallet debug-info [OPTIONS]

 Print privacy-friendly diagnostic information for troubleshooting.

 Outputs system details, package versions, and backend status.
 No wallet keys, addresses, balances, or transaction data is included.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --network       -n      TEXT  Bitcoin network                                │
│ --backend       -b      TEXT  Backend: scantxoutset | descriptor_wallet |    │
│                               neutrino                                       │
│ --neutrino-url          TEXT  [env var: NEUTRINO_URL]                        │
│ --log-level     -l      TEXT  Log level                                      │
│ --help                        Show this message and exit.                    │
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
│                                             [env var: MNEMONIC_FILE]         │
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
│ --mnemonic-file            -f      PATH     [env var: MNEMONIC_FILE]         │
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
 or set the MNEMONIC environment variable.

 By default, saves to ~/.joinmarket-ng/wallets/default.mnemonic with password
 protection.

 Examples:
     jm-wallet import                          # Interactive input, 24 words
     jm-wallet import --words 12               # Interactive input, 12 words
     MNEMONIC="word1 word2 ..." jm-wallet import  # Via env var
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
│ --mnemonic-file        -f                     PATH     Path to mnemonic file │
│                                                        [env var:             │
│                                                        MNEMONIC_FILE]        │
│ --prompt-bip39-passp…                                  Prompt for BIP39      │
│                                                        passphrase            │
│                                                        interactively         │
│ --network              -n                     TEXT     Bitcoin network       │
│ --backend              -b                     TEXT     Backend: scantxoutset │
│                                                        | descriptor_wallet | │
│                                                        neutrino              │
│ --rpc-url                                     TEXT     [env var:             │
│                                                        BITCOIN_RPC_URL]      │
│ --neutrino-url                                TEXT     [env var:             │
│                                                        NEUTRINO_URL]         │
│ --extended             -e                              Show detailed address │
│                                                        view with derivations │
│ --gap                  -g                     INTEGER  Max address gap to    │
│                                                        show in extended view │
│                                                        [default: 6]          │
│ --show-empty               --no-show-empty             In --extended view,   │
│                                                        show addresses with   │
│                                                        zero balance. When    │
│                                                        disabled (default),   │
│                                                        empty addresses are   │
│                                                        hidden except for the │
│                                                        first unused one per  │
│                                                        branch so you still   │
│                                                        have a fresh receive  │
│                                                        address.              │
│                                                        [default:             │
│                                                        no-show-empty]        │
│ --data-dir                                    PATH     Data directory        │
│                                                        (default:             │
│                                                        ~/.joinmarket-ng or   │
│                                                        $JOINMARKET_DATA_DIR) │
│ --log-level            -l                     TEXT     Log level             │
│ --help                                                 Show this message and │
│                                                        exit.                 │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet verify-password --help</code></summary>

```

 Usage: jm-wallet verify-password [OPTIONS]

 Verify that a password can decrypt an encrypted mnemonic file.

 Exits with status 0 if the password is correct, 1 otherwise.
 Intended for scripting (e.g. the TUI) to validate a password before
 storing it in config.toml. No mnemonic content is printed.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ *  --mnemonic-file  -f                 PATH  Path to encrypted mnemonic file │
│                                              [env var: MNEMONIC_FILE]        │
│                                              [required]                      │
│    --password       -p                 TEXT  Password to verify. If not      │
│                                              provided, read from             │
│                                              MNEMONIC_PASSWORD env or        │
│                                              prompt.                         │
│                                              [env var: MNEMONIC_PASSWORD]    │
│    --prompt             --no-prompt          Prompt for password if not      │
│                                              provided via flag/env.          │
│                                              [default: prompt]               │
│    --help                                    Show this message and exit.     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet validate --help</code></summary>

```

 Usage: jm-wallet validate [OPTIONS]

 Validate a mnemonic phrase.

 Provide a mnemonic via --mnemonic-file, the MNEMONIC environment variable,
 or enter it interactively when prompted.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file  -f      PATH  Path to mnemonic file                         │
│                                [env var: MNEMONIC_FILE]                      │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<!-- AUTO-GENERATED HELP END: jm-wallet -->
