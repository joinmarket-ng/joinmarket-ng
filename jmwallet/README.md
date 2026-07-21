# JoinMarket Wallet Library

Modern HD wallet for JoinMarket with support for Bitcoin Core nodes and lightweight Neutrino SPV.

## Features

- **Wallet Creation**: Generate wallets from mnemonic seed phrases
- **Address Management**: Derive and track addresses across mixdepths
- **Reserved Addresses**: Set aside and label deposit addresses (`jm-wallet address`) so handed-out addresses are never reissued and are shown with their label in `jm-wallet info --extended`
- **Transaction Building**: Construct and sign Bitcoin transactions
- **Coin Selection**: Privacy-preserving coin selection algorithms
- **Fidelity Bonds**: Create and manage fidelity bonds for maker reputation
- **Balance Tracking**: Monitor balances across multiple mixdepths
- **History Recovery**: Reconstruct and persist best-effort CoinJoin, send, and deposit
  history from on-chain data when importing a wallet from seed

## Imported Wallet History

After a seed import, `jm-wallet` reconstructs confirmed transaction history once
the backend's historical scan is complete. Reconstructed rows are persisted in
`history.csv` with `source=onchain`; rows recorded by live maker, taker, and send
flows use `source=protocol` and always take precedence for the same transaction.

Run `jm-wallet reconstruct-history` to rebuild the guessed rows explicitly. The
command preserves protocol-recorded rows and marks reconstructed entries with `*`
in `jm-wallet history` output. Set `wallet.reconstruct_history = false` to disable
automatic reconstruction.

CoinJoin detection, amount, and peer count come from the repeated equal-output
structure. Maker/taker role and fees cannot be proven from public chain data, so
those values are best-effort guesses. Counterparty nicknames and the split between
taker maker-fees and mining fees cannot be recovered.

The explicit rebuild waits for any active Bitcoin Core full rescan before changing
the CSV. Reconstructed maker guesses are not included in the legacy yield-generator
earnings report because that format cannot carry provenance and expects exact fees.
`jm-wallet history --stats` does include reconstructed rows and marks its totals as
containing estimates.

## Documentation

For full documentation, see [jmwallet Documentation](https://joinmarket-ng.github.io/joinmarket-ng/README-jmwallet/).

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
│ list-bonds                   List fidelity bonds from the local registry     │
│                              (offline, no blockchain access).                │
│ generate-bond-address        Generate a fidelity bond (timelocked P2WSH)     │
│                              address.                                        │
│ import-bond                  Manually import a fidelity bond into the        │
│                              registry.                                       │
│ sync-bonds                   Refresh funded status of bonds already in the   │
│                              registry (fast).                                │
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
│ reconstruct-history          Rebuild guessed CoinJoin/send/deposit history   │
│                              from on-chain data.                             │
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
│ showseed                     Display the BIP39 seed words (mnemonic) of an   │
│                              existing wallet.                                │
│ rescan                       Rescan the blockchain to repair a descriptor    │
│                              wallet's coverage.                              │
│ address                      Manage deposit addresses: reserve, label,       │
│                              release, and list.                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet list-bonds --help</code></summary>

```

 Usage: jm-wallet list-bonds [OPTIONS]

 List fidelity bonds from the local registry (offline, no blockchain access).

 This command only reads the per-wallet registry; it never scans the
 blockchain. Registered-but-unfunded bonds (created with
 generate-bond-address or import-bond but not yet funded) are shown with an
 UNFUNDED status. Funded status and values reflect the last on-chain sync.

 To refresh funded status from the blockchain, use 'jm-wallet sync-bonds'
 (fast, known bonds) or 'jm-wallet recover-bonds' (full discovery scan). The
 per-wallet registry is selected by the fingerprint derived from
 --mnemonic-file, taken from --wallet-fingerprint, the configured wallet, or
 auto-detected when only one wallet's registry exists in the data dir.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH  Select the per-wallet bond registry │
│                                          by deriving its fingerprint from    │
│                                          this mnemonic file. This does NOT   │
│                                          scan the blockchain; use 'jm-wallet │
│                                          recover-bonds' to discover bonds    │
│                                          on-chain.                           │
│                                          [env var: MNEMONIC_FILE]            │
│ --prompt-bip39-passphrase                Prompt for BIP39 passphrase         │
│ --wallet-fingerprint               TEXT  Select the per-wallet bond registry │
│                                          by its 8-char hex BIP32 master      │
│                                          fingerprint. Use this instead of    │
│                                          --mnemonic-file when you already    │
│                                          know the fingerprint (e.g. from     │
│                                          'jm-wallet info'). When neither     │
│                                          --mnemonic-file nor this flag is    │
│                                          provided and exactly one wallet has │
│                                          a registry in the data directory,   │
│                                          that wallet is selected             │
│                                          automatically.                      │
│ --data-dir                         PATH  Data directory (default:            │
│                                          ~/.joinmarket-ng or                 │
│                                          $JOINMARKET_DATA_DIR)               │
│                                          [env var: JOINMARKET_DATA_DIR]      │
│ --config-file                      PATH  Config file path (decoupled from    │
│                                          data dir). Defaults to              │
│                                          <data-dir>/config.toml              │
│                                          [env var: JOINMARKET_CONFIG_FILE]   │
│ --funded-only                            Show only funded bonds              │
│ --active-only                            Show only active bonds              │
│ --json                     -j            Output as JSON                      │
│ --log-level                -l      TEXT  Log level                           │
│ --help                                   Show this message and exit.         │
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
│                                             [env var: JOINMARKET_DATA_DIR]   │
│ --config-file                      PATH     Config file path (decoupled from │
│                                             data dir). Defaults to           │
│                                             <data-dir>/config.toml           │
│                                             [env var:                        │
│                                             JOINMARKET_CONFIG_FILE]          │
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
│                                             [env var: JOINMARKET_DATA_DIR]   │
│ --config-file                      PATH     Config file path (decoupled from │
│                                             data dir). Defaults to           │
│                                             <data-dir>/config.toml           │
│                                             [env var:                        │
│                                             JOINMARKET_CONFIG_FILE]          │
│ --log-level                -l      TEXT     Log level                        │
│ --help                                      Show this message and exit.      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet sync-bonds --help</code></summary>

```

 Usage: jm-wallet sync-bonds [OPTIONS]

 Refresh funded status of bonds already in the registry (fast).

 Syncs only the bond addresses already recorded in the per-wallet registry
 and updates their on-chain UTXO info (value, confirmations). Unlike
 recover-bonds, this does NOT scan all 960 possible timelocks, so it is the
 quick way to reflect a funding transaction after creating a bond with
 generate-bond-address. Use recover-bonds instead when you need to discover
 bonds whose addresses are not yet in the registry.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH  [env var: MNEMONIC_FILE]            │
│ --prompt-bip39-passphrase                Prompt for BIP39 passphrase         │
│ --network                  -n      TEXT  Bitcoin network                     │
│ --backend                  -b      TEXT  Backend: descriptor_wallet |        │
│                                          neutrino                            │
│ --rpc-url                          TEXT  [env var: BITCOIN_RPC_URL]          │
│ --neutrino-url                     TEXT  [env var: NEUTRINO_URL]             │
│ --data-dir                         PATH  Data directory (default:            │
│                                          ~/.joinmarket-ng or                 │
│                                          $JOINMARKET_DATA_DIR)               │
│                                          [env var: JOINMARKET_DATA_DIR]      │
│ --config-file                      PATH  Config file path (decoupled from    │
│                                          data dir). Defaults to              │
│                                          <data-dir>/config.toml              │
│                                          [env var: JOINMARKET_CONFIG_FILE]   │
│ --log-level                -l      TEXT  Log level                           │
│ --help                                   Show this message and exit.         │
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
│ --backend                  -b      TEXT  Backend: descriptor_wallet |        │
│                                          neutrino                            │
│ --rpc-url                          TEXT  [env var: BITCOIN_RPC_URL]          │
│ --neutrino-url                     TEXT  [env var: NEUTRINO_URL]             │
│ --data-dir                         PATH  Data directory (default:            │
│                                          ~/.joinmarket-ng or                 │
│                                          $JOINMARKET_DATA_DIR)               │
│                                          [env var: JOINMARKET_DATA_DIR]      │
│ --config-file                      PATH  Config file path (decoupled from    │
│                                          data dir). Defaults to              │
│                                          <data-dir>/config.toml              │
│                                          [env var: JOINMARKET_CONFIG_FILE]   │
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
│ --locktime            -L      INTEGER  Locktime as Unix timestamp            │
│                                        [default: 0]                          │
│ --locktime-date       -d      TEXT     Locktime as date (YYYY-MM, must be    │
│                                        1st of month)                         │
│ --network             -n      TEXT     [default: mainnet]                    │
│ --data-dir                    PATH     Data directory (default:              │
│                                        ~/.joinmarket-ng or                   │
│                                        $JOINMARKET_DATA_DIR)                 │
│                                        [env var: JOINMARKET_DATA_DIR]        │
│ --no-save                              Do not save the bond to the registry  │
│ --wallet-fingerprint          TEXT     8-char hex master key fingerprint of  │
│                                        the JoinMarket wallet that will       │
│                                        operate this bond. Run 'jm-wallet     │
│                                        info --mnemonic-file <wallet>' on the │
│                                        hot wallet to look it up. Required    │
│                                        because each wallet has its own bond  │
│                                        registry (fidelity_bonds_<fp>.json)   │
│                                        under the shared data directory.      │
│ --log-level           -l      TEXT     [default: INFO]                       │
│ --help                                 Show this message and exit.           │
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
│ --bond-address              TEXT  Bond address to associate keypair with     │
│                                   (saves to registry)                        │
│ --data-dir                  PATH  Data directory (default: ~/.joinmarket-ng  │
│                                   or $JOINMARKET_DATA_DIR)                   │
│                                   [env var: JOINMARKET_DATA_DIR]             │
│ --wallet-fingerprint        TEXT  8-char hex master key fingerprint of the   │
│                                   JoinMarket wallet that will operate this   │
│                                   bond. Run 'jm-wallet info --mnemonic-file  │
│                                   <wallet>' on the hot wallet to look it up. │
│                                   Required because each wallet has its own   │
│                                   bond registry (fidelity_bonds_<fp>.json)   │
│                                   under the shared data directory.           │
│ --log-level                 TEXT  [default: INFO]                            │
│ --help                            Show this message and exit.                │
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
│ --cert-pubkey                 TEXT     Certificate public key (hex)          │
│ --validity-periods            INTEGER  Certificate validity in 2016-block    │
│                                        periods from now (1=~2wk, 52=~2yr)    │
│                                        [default: 52]                         │
│ --data-dir                    PATH     Data directory (default:              │
│                                        ~/.joinmarket-ng or                   │
│                                        $JOINMARKET_DATA_DIR)                 │
│                                        [env var: JOINMARKET_DATA_DIR]        │
│ --config-file                 PATH     Config file path (decoupled from data │
│                                        dir). Defaults to                     │
│                                        <data-dir>/config.toml                │
│                                        [env var: JOINMARKET_CONFIG_FILE]     │
│ --network             -n      TEXT     Bitcoin network                       │
│ --backend             -b      TEXT     Backend: descriptor_wallet | neutrino │
│ --rpc-url                     TEXT     [env var: BITCOIN_RPC_URL]            │
│ --neutrino-url                TEXT     [env var: NEUTRINO_URL]               │
│ --current-block               INTEGER  Current block height override for     │
│                                        offline/air-gapped workflows. Skips   │
│                                        all network block-height lookups.     │
│ --wallet-fingerprint          TEXT     8-char hex master key fingerprint of  │
│                                        the JoinMarket wallet that will       │
│                                        operate this bond. Run 'jm-wallet     │
│                                        info --mnemonic-file <wallet>' on the │
│                                        hot wallet to look it up. Required    │
│                                        because each wallet has its own bond  │
│                                        registry (fidelity_bonds_<fp>.json)   │
│                                        under the shared data directory.      │
│ --log-level                   TEXT     [default: INFO]                       │
│ --help                                 Show this message and exit.           │
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
│ --cert-pubkey                 TEXT     Certificate pubkey (hex)              │
│ --cert-signature              TEXT     Certificate signature (base64)        │
│ --cert-expiry                 INTEGER  Certificate expiry as ABSOLUTE period │
│                                        number (from                          │
│                                        prepare-certificate-message)          │
│                                        [default: 0]                          │
│ --data-dir                    PATH     Data directory (default:              │
│                                        ~/.joinmarket-ng or                   │
│                                        $JOINMARKET_DATA_DIR)                 │
│                                        [env var: JOINMARKET_DATA_DIR]        │
│ --config-file                 PATH     Config file path (decoupled from data │
│                                        dir). Defaults to                     │
│                                        <data-dir>/config.toml                │
│                                        [env var: JOINMARKET_CONFIG_FILE]     │
│ --skip-verification                    Skip signature verification (not      │
│                                        recommended)                          │
│ --network             -n      TEXT     Bitcoin network                       │
│ --backend             -b      TEXT     Backend: descriptor_wallet | neutrino │
│ --rpc-url                     TEXT     [env var: BITCOIN_RPC_URL]            │
│ --neutrino-url                TEXT     [env var: NEUTRINO_URL]               │
│ --current-block               INTEGER  Current block height override for     │
│                                        offline/air-gapped workflows. Skips   │
│                                        all network block-height lookups.     │
│ --wallet-fingerprint          TEXT     8-char hex master key fingerprint of  │
│                                        the JoinMarket wallet that will       │
│                                        operate this bond. Run 'jm-wallet     │
│                                        info --mnemonic-file <wallet>' on the │
│                                        hot wallet to look it up. Required    │
│                                        because each wallet has its own bond  │
│                                        registry (fidelity_bonds_<fp>.json)   │
│                                        under the shared data directory.      │
│ --log-level                   TEXT     [default: INFO]                       │
│ --help                                 Show this message and exit.           │
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

 HOT WALLETS: If the bond was created with 'generate-bond-address' (key derived
 from this wallet's seed), you do NOT need a PSBT. After it expires, spend it
 directly with: jm-wallet send <destination> --select-utxos

 REQUIREMENTS:
 - The bond must exist in the registry (created with 'create-bond-address')
 - The bond must be funded (use 'jm-wallet sync-bonds'
   to update UTXO info), unless using --test-unfunded for a dry-run signer test
 - The locktime must have expired (or be close enough for your use case)

 SIGNING:

 Most hardware wallets (Trezor, Coldcard, BitBox02, KeepKey) CANNOT sign
 CLTV timelock P2WSH scripts -- their firmware rejects custom witness
 scripts. Blockstream Jade DOES support arbitrary witness scripts and may
 work via HWI (scripts/sign_bond_psbt.py). Ledger only supports this with
 the legacy Bitcoin app (2.0.x and earlier); the current app (2.1+) has
 been reported to reject bond PSBTs. Specter DIY signs via QR PSBT
 exchange instead.

 Option A - Mnemonic signing (works with any device):
 1. Run: python scripts/sign_bond_mnemonic.py <psbt_base64>
 2. Enter your BIP39 mnemonic when prompted (hidden input)
 3. Broadcast: bitcoin-cli sendrawtransaction <signed_hex>

 Option B - HWI signing (Jade; Ledger legacy app only):
 1. Install HWI: pip install -U hwi  (>= 3.1.0 for newer device models)
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
│                                        [env var: JOINMARKET_DATA_DIR]        │
│ --wallet-fingerprint          TEXT     8-char hex master key fingerprint of  │
│                                        the JoinMarket wallet that will       │
│                                        operate this bond. Run 'jm-wallet     │
│                                        info --mnemonic-file <wallet>' on the │
│                                        hot wallet to look it up. Required    │
│                                        because each wallet has its own bond  │
│                                        registry (fidelity_bonds_<fp>.json)   │
│                                        under the shared data directory.      │
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
│ --backend       -b      TEXT  Backend: descriptor_wallet | neutrino          │
│ --neutrino-url          TEXT  [env var: NEUTRINO_URL]                        │
│ --data-dir              PATH  Data directory (default: ~/.joinmarket-ng or   │
│                               $JOINMARKET_DATA_DIR)                          │
│                               [env var: JOINMARKET_DATA_DIR]                 │
│ --config-file           PATH  Config file path (decoupled from data dir).    │
│                               Defaults to <data-dir>/config.toml             │
│                               [env var: JOINMARKET_CONFIG_FILE]              │
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

 Still-locked fidelity bonds are shown as [FB-LOCKED] and cannot be toggled
 (they are already unspendable until their timelock expires). Expired
 fidelity bonds behave like regular UTXOs: they can be frozen/unfrozen, and
 "unfreeze all" will unfreeze them.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH     Path to mnemonic file            │
│                                             [env var: MNEMONIC_FILE]         │
│ --prompt-bip39-passphrase                   Prompt for BIP39 passphrase      │
│                                             interactively                    │
│ --network                  -n      TEXT     Bitcoin network                  │
│ --backend                  -b      TEXT     Backend: descriptor_wallet |     │
│                                             neutrino                         │
│ --rpc-url                          TEXT     [env var: BITCOIN_RPC_URL]       │
│ --neutrino-url                     TEXT     [env var: NEUTRINO_URL]          │
│ --mixdepth                 -m      INTEGER  Filter to a specific mixdepth    │
│                                             (0-4)                            │
│ --data-dir                         PATH     Data directory (default:         │
│                                             ~/.joinmarket-ng or              │
│                                             $JOINMARKET_DATA_DIR)            │
│                                             [env var: JOINMARKET_DATA_DIR]   │
│ --config-file                      PATH     Config file path (decoupled from │
│                                             data dir). Defaults to           │
│                                             <data-dir>/config.toml           │
│                                             [env var:                        │
│                                             JOINMARKET_CONFIG_FILE]          │
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

 By default the active wallet's entries are shown. The wallet is
 selected (in priority order) from ``--wallet-fingerprint``,
 ``--mnemonic-file`` (with optional ``--prompt-bip39-passphrase``),
 or auto-detected when ``history.csv`` contains exactly one wallet.
 Pass ``--all-wallets`` to disable per-wallet filtering entirely.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --limit                    -n      INTEGER  Max entries to show              │
│ --role                     -r      TEXT     Filter by role                   │
│                                             (maker/taker/send/deposit)       │
│ --stats                    -s               Show statistics only             │
│ --csv                                       Output as CSV                    │
│ --data-dir                         PATH     Data directory (default:         │
│                                             ~/.joinmarket-ng or              │
│                                             $JOINMARKET_DATA_DIR)            │
│                                             [env var: JOINMARKET_DATA_DIR]   │
│ --config-file                      PATH     Config file path (decoupled from │
│                                             data dir). Defaults to           │
│                                             <data-dir>/config.toml           │
│                                             [env var:                        │
│                                             JOINMARKET_CONFIG_FILE]          │
│ --mnemonic-file            -f      PATH     Path to mnemonic file. When      │
│                                             provided, the history is         │
│                                             filtered to entries belonging to │
│                                             this wallet (matched by BIP32    │
│                                             master fingerprint). Required    │
│                                             when multiple wallets share the  │
│                                             same data directory (issue #473) │
│                                             unless --wallet-fingerprint is   │
│                                             passed instead.                  │
│                                             [env var: MNEMONIC_FILE]         │
│ --prompt-bip39-passphrase                   Prompt for the BIP39 passphrase  │
│                                             when deriving the wallet         │
│                                             fingerprint from                 │
│                                             --mnemonic-file. Required when   │
│                                             the wallet was created with a    │
│                                             BIP39 passphrase, otherwise the  │
│                                             derived fingerprint will not     │
│                                             match any recorded history.      │
│ --wallet-fingerprint               TEXT     Filter history to this 8-char    │
│                                             hex BIP32 master fingerprint.    │
│                                             Use this instead of              │
│                                             --mnemonic-file when you already │
│                                             know the fingerprint (e.g.       │
│                                             printed by 'jm-wallet info').    │
│                                             When neither this flag nor       │
│                                             --mnemonic-file is given and     │
│                                             history contains exactly one     │
│                                             wallet, that wallet is selected  │
│                                             automatically.                   │
│ --all-wallets                               Show entries from all wallets    │
│                                             that have ever written to this   │
│                                             data directory, including legacy │
│                                             rows without a fingerprint.      │
│ --log-level                -l      TEXT     Log level                        │
│ --help                                      Show this message and exit.      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet reconstruct-history --help</code></summary>

```

 Usage: jm-wallet reconstruct-history [OPTIONS]

 Rebuild guessed CoinJoin/send/deposit history from on-chain data.

 Enumerates the wallet's confirmed transactions, classifies each with the
 equal-output CoinJoin heuristic (guessing role, fees, and peer count),
 and stores the result as history rows tagged ``source="onchain"``. Rows
 recorded at protocol time are never modified; transactions they already
 cover are skipped. By default previously reconstructed rows are purged
 first so the guessed portion is rebuilt from scratch.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file    -f                     PATH              [env var:        │
│                                                             MNEMONIC_FILE]   │
│ --prompt-bip39-p…                                           Prompt for BIP39 │
│                                                             passphrase       │
│ --max-transactio…                         INTEGER RANGE     Safety cap on    │
│                                           [x>=1]            transactions     │
│                                                             classified in    │
│                                                             one pass         │
│                                                             [default: 1000]  │
│ --keep-existing        --purge-existi…                      Keep previously  │
│                                                             reconstructed    │
│                                                             (on-chain) rows  │
│                                                             instead of       │
│                                                             purging and      │
│                                                             rebuilding them. │
│                                                             Protocol-record… │
│                                                             rows are always  │
│                                                             kept either way. │
│                                                             [default:        │
│                                                             purge-existing]  │
│ --network          -n                     TEXT              Bitcoin network  │
│ --backend          -b                     TEXT              Backend:         │
│                                                             descriptor_wall… │
│                                                             | neutrino       │
│ --rpc-url                                 TEXT              [env var:        │
│                                                             BITCOIN_RPC_URL] │
│ --neutrino-url                            TEXT              [env var:        │
│                                                             NEUTRINO_URL]    │
│ --data-dir                                PATH              Data directory   │
│                                                             (default:        │
│                                                             ~/.joinmarket-ng │
│                                                             or               │
│                                                             $JOINMARKET_DAT… │
│                                                             [env var:        │
│                                                             JOINMARKET_DATA… │
│ --config-file                             PATH              Config file path │
│                                                             (decoupled from  │
│                                                             data dir).       │
│                                                             Defaults to      │
│                                                             <data-dir>/conf… │
│                                                             [env var:        │
│                                                             JOINMARKET_CONF… │
│ --log-level        -l                     TEXT              Log level        │
│ --help                                                      Show this        │
│                                                             message and      │
│                                                             exit.            │
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
│ --mnemonic-file            -f      PATH  [env var: MNEMONIC_FILE]            │
│ --prompt-bip39-passphrase                Prompt for BIP39 passphrase         │
│ --wallet-fingerprint               TEXT  Select the per-wallet bond registry │
│                                          by its 8-char hex BIP32 master      │
│                                          fingerprint. Use this instead of    │
│                                          --mnemonic-file when you already    │
│                                          know the fingerprint (e.g. from     │
│                                          'jm-wallet info'). When neither is  │
│                                          provided and exactly one wallet has │
│                                          a registry in the data directory,   │
│                                          that wallet is selected             │
│                                          automatically.                      │
│ --data-dir                         PATH  Data directory (default:            │
│                                          ~/.joinmarket-ng or                 │
│                                          $JOINMARKET_DATA_DIR)               │
│                                          [env var: JOINMARKET_DATA_DIR]      │
│ --config-file                      PATH  Config file path (decoupled from    │
│                                          data dir). Defaults to              │
│                                          <data-dir>/config.toml              │
│                                          [env var: JOINMARKET_CONFIG_FILE]   │
│ --json                     -j            Output as JSON                      │
│ --log-level                -l      TEXT  [default: WARNING]                  │
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
│ --amount               -a                    INTEGER  Amount in sats (0 for  │
│                                                       sweep)                 │
│                                                       [default: 0]           │
│ --mnemonic-file        -f                    PATH     [env var:              │
│                                                       MNEMONIC_FILE]         │
│ --prompt-bip39-passp…                                 Prompt for BIP39       │
│                                                       passphrase             │
│ --mixdepth             -m                    INTEGER  Source mixdepth        │
│                                                       [default: 0]           │
│ --fee-rate                                   FLOAT    Manual fee rate in     │
│                                                       sat/vB (e.g. 1.5).     │
│                                                       Mutually exclusive     │
│                                                       with --block-target.   │
│                                                       Defaults to 3-block    │
│                                                       estimation.            │
│ --block-target                               INTEGER  Target blocks for fee  │
│                                                       estimation (1-1008).   │
│                                                       Defaults to 3.         │
│ --network              -n                    TEXT     Bitcoin network        │
│ --backend              -b                    TEXT     Backend:               │
│                                                       descriptor_wallet |    │
│                                                       neutrino               │
│ --rpc-url                                    TEXT     [env var:              │
│                                                       BITCOIN_RPC_URL]       │
│ --neutrino-url                               TEXT     [env var:              │
│                                                       NEUTRINO_URL]          │
│ --broadcast                --no-broadcast             Broadcast the          │
│                                                       transaction (use       │
│                                                       --no-broadcast to      │
│                                                       skip)                  │
│                                                       [default: broadcast]   │
│ --yes                  -y                             Skip confirmation      │
│                                                       prompt                 │
│ --select-utxos         -s                             Interactively select   │
│                                                       UTXOs (fzf-like TUI)   │
│ --data-dir                                   PATH     Data directory         │
│                                                       (default:              │
│                                                       ~/.joinmarket-ng or    │
│                                                       $JOINMARKET_DATA_DIR)  │
│                                                       [env var:              │
│                                                       JOINMARKET_DATA_DIR]   │
│ --config-file                                PATH     Config file path       │
│                                                       (decoupled from data   │
│                                                       dir). Defaults to      │
│                                                       <data-dir>/config.toml │
│                                                       [env var:              │
│                                                       JOINMARKET_CONFIG_FIL… │
│ --log-level            -l                    TEXT     Log level              │
│ --help                                                Show this message and  │
│                                                       exit.                  │
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

 By default, saves to <data-dir>/wallets/default.mnemonic with password
 protection. The data directory is taken from --data-dir, the
 JOINMARKET_DATA_DIR environment variable, or ~/.joinmarket-ng (in that
 order of precedence).

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
│ --data-dir                                     PATH     Data directory       │
│                                                         (default:            │
│                                                         ~/.joinmarket-ng or  │
│                                                         $JOINMARKET_DATA_DI… │
│                                                         When --output is not │
│                                                         given, the wallet is │
│                                                         saved under          │
│                                                         <data-dir>/wallets/… │
│                                                         [env var:            │
│                                                         JOINMARKET_DATA_DIR] │
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

 By default, saves to <data-dir>/wallets/default.mnemonic with password
 protection. The data directory is taken from --data-dir, the
 JOINMARKET_DATA_DIR environment variable, or ~/.joinmarket-ng (in that
 order of precedence). Use --no-save to only display the mnemonic without
 saving.

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
│ --force            -f                                   Overwrite existing   │
│                                                         file without         │
│                                                         confirmation         │
│ --data-dir                                     PATH     Data directory       │
│                                                         (default:            │
│                                                         ~/.joinmarket-ng or  │
│                                                         $JOINMARKET_DATA_DI… │
│                                                         When --output is not │
│                                                         given, the wallet is │
│                                                         saved under          │
│                                                         <data-dir>/wallets/… │
│                                                         [env var:            │
│                                                         JOINMARKET_DATA_DIR] │
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
│ --backend              -b                     TEXT     Backend:              │
│                                                        descriptor_wallet |   │
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
│ --scan-status                                          Print Bitcoin Core's  │
│                                                        wallet scan/coverage  │
│                                                        diagnostics and exit  │
│                                                        (descriptor wallet    │
│                                                        only). Use it when    │
│                                                        the wallet proposes   │
│                                                        already-used          │
│                                                        addresses; if         │
│                                                        coverage is           │
│                                                        incomplete, repair it │
│                                                        with `jm-wallet       │
│                                                        rescan`. See the      │
│                                                        wallet scanning docs. │
│ --data-dir                                    PATH     Data directory        │
│                                                        (default:             │
│                                                        ~/.joinmarket-ng or   │
│                                                        $JOINMARKET_DATA_DIR) │
│                                                        [env var:             │
│                                                        JOINMARKET_DATA_DIR]  │
│ --config-file                                 PATH     Config file path      │
│                                                        (decoupled from data  │
│                                                        dir). Defaults to     │
│                                                        <data-dir>/config.to… │
│                                                        [env var:             │
│                                                        JOINMARKET_CONFIG_FI… │
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

<details>
<summary><code>jm-wallet showseed --help</code></summary>

```

 Usage: jm-wallet showseed [OPTIONS]

 Display the BIP39 seed words (mnemonic) of an existing wallet.

 Reads the encrypted ``.mnemonic`` file produced by ``jm-wallet generate``
 (or any compatible wallet) and prints the seed words to stdout.

 SECURITY:
 - The seed words give full control over all funds. Never share them, never
   type them into a website, never store them in cloud sync.
 - Only run this command in a private setting. Output goes to stdout in
   plaintext; redirect carefully.
 - The password is required when the mnemonic file is encrypted.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ *  --mnemonic-file  -f                   PATH  Path to the mnemonic file     │
│                                                [env var: MNEMONIC_FILE]      │
│                                                [required]                    │
│    --password       -p                   TEXT  Password for an encrypted     │
│                                                mnemonic file. If not given,  │
│                                                the MNEMONIC_PASSWORD env var │
│                                                is used, otherwise an         │
│                                                interactive prompt is shown.  │
│                                                [env var: MNEMONIC_PASSWORD]  │
│    --numbered           --no-numbered          Print each seed word on its   │
│                                                own line, prefixed with its   │
│                                                index.                        │
│                                                [default: numbered]           │
│    --yes            -y                         Skip the interactive 'Are you │
│                                                sure?' confirmation. Use with │
│                                                care.                         │
│    --help                                      Show this message and exit.   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet rescan --help</code></summary>

```

 Usage: jm-wallet rescan [OPTIONS]

 Rescan the blockchain to repair a descriptor wallet's coverage.

 Two kinds of gap can leave the wallet unaware of its own coins:

 - Time coverage: Bitcoin Core has not scanned far enough back. Plain
   `jm-wallet rescan` (optionally `--start-height H`) re-scans blocks
   against the current descriptor range.
 - Index coverage: a used address sits beyond the imported address range
   (common for wallets migrated from legacy joinmarket-clientserver). Pass
   `--scan-depth N` to widen the range to N per branch, then rescan.
   `--scan-depth` can be combined with `--start-height H` to widen the
   range and only rescan from height H (defaults to genesis).

 Rescans are slow (20+ minutes on mainnet from genesis) but read-only. The
 scan runs server-side in Bitcoin Core, so Ctrl-C only stops the progress
 polling, not the scan; re-attach later with `jm-wallet info --scan-status`.
 See docs/technical/wallet-scanning.md.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH     Path to mnemonic file            │
│                                             [env var: MNEMONIC_FILE]         │
│ --prompt-bip39-passphrase                   Prompt for BIP39 passphrase      │
│                                             interactively                    │
│ --network                  -n      TEXT     Bitcoin network                  │
│ --rpc-url                          TEXT     [env var: BITCOIN_RPC_URL]       │
│ --start-height                     INTEGER  Block height to rescan from      │
│                                             (default: 0 = genesis). The      │
│                                             wallet's recorded creation       │
│                                             height is used as a floor when   │
│                                             available, so values below it    │
│                                             are clamped up automatically.    │
│                                             Honored both on its own and      │
│                                             together with --scan-depth.      │
│                                             [default: 0]                     │
│ --scan-depth                       INTEGER  Widen the descriptor             │
│                                             address-index range to N per     │
│                                             branch before rescanning         │
│                                             (re-imports descriptors). Use    │
│                                             this once for a wallet whose     │
│                                             used addresses sit beyond the    │
│                                             configured .scan_range. See the  │
│                                             wallet scanning docs.            │
│ --data-dir                         PATH     Data directory (default:         │
│                                             ~/.joinmarket-ng or              │
│                                             $JOINMARKET_DATA_DIR)            │
│                                             [env var: JOINMARKET_DATA_DIR]   │
│ --config-file                      PATH     Config file path (decoupled from │
│                                             data dir). Defaults to           │
│                                             <data-dir>/config.toml           │
│                                             [env var:                        │
│                                             JOINMARKET_CONFIG_FILE]          │
│ --log-level                -l      TEXT     Log level                        │
│ --help                                      Show this message and exit.      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-wallet address --help</code></summary>

```

 Usage: jm-wallet address [OPTIONS] COMMAND [ARGS]...

 Manage deposit addresses: reserve, label, release, and list.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file            -f      PATH  Path to mnemonic file               │
│                                          [env var: MNEMONIC_FILE]            │
│ --prompt-bip39-passphrase                Prompt for BIP39 passphrase         │
│                                          interactively                       │
│ --network                  -n      TEXT  Bitcoin network                     │
│ --backend                  -b      TEXT  Backend: descriptor_wallet |        │
│                                          neutrino                            │
│ --rpc-url                          TEXT  [env var: BITCOIN_RPC_URL]          │
│ --neutrino-url                     TEXT  [env var: NEUTRINO_URL]             │
│ --data-dir                         PATH  Data directory (default:            │
│                                          ~/.joinmarket-ng or                 │
│                                          $JOINMARKET_DATA_DIR)               │
│                                          [env var: JOINMARKET_DATA_DIR]      │
│ --config-file                      PATH  Config file path (defaults to       │
│                                          <data-dir>/config.toml)             │
│                                          [env var: JOINMARKET_CONFIG_FILE]   │
│ --log-level                -l      TEXT  Log level                           │
│ --help                                   Show this message and exit.         │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ new      Generate a fresh deposit address, reserve it, and optionally label  │
│          it.                                                                 │
│ label    Reserve an existing deposit address and attach a label to it.       │
│ release  Remove a reservation/label so the address is no longer set aside.   │
│ list     List reserved deposit addresses with their mixdepth and label.      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<!-- AUTO-GENERATED HELP END: jm-wallet -->
