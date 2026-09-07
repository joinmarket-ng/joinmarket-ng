# Experimental JAM NG Flatpak

This experimental Flatpak package (`org.joinmarketng.JamNG`) bundles all services
(jmwalletd + JAM web UI, Tor, the Neutrino light client, and the orderbook
watcher) in a single sandboxed desktop application. It is intended for testing
the integration. For normal JoinMarket NG use, start with the [CLI](getting-started.md).
For a browser interface, use the upstream [JAM project](https://github.com/joinmarket-webui/jam).

## Build and install

Download the `.flatpak` bundle from the
[release page](https://github.com/joinmarket-ng/joinmarket-ng/releases), then install
that local file (replace the placeholder with its path):

```bash
flatpak install --user /path/to/release.flatpak
```

Or, from a source checkout with `flatpak-builder` installed:

```bash
flatpak-builder --user --install --force-clean build-dir flatpak/org.joinmarketng.JamNG.yml
```

## Run

```bash
flatpak run org.joinmarketng.JamNG            # mainnet (default)
flatpak run org.joinmarketng.JamNG --no-gui   # headless (opens the browser instead of the GUI)
```

On launch the app starts Tor, the Neutrino light client, jmwalletd, and the
orderbook watcher, then opens the JAM web UI. All ports are allocated
dynamically so the Flatpak never conflicts with other local services.

In JAM, create a new wallet or restore one from recovery words. Record the
recovery words and any BIP39 passphrase offline before depositing. Wait for the
backend to sync and check the selected network. See
[wallet concepts](technical/concepts.md) and [backup requirements](recover-wallet.md)
before using funds; the CLI import procedure is for native wallets, not JAM files.

## Running CLI commands

Use the `cli` pass-through to run any bundled CLI tool inside the sandbox with
the same environment (Tor, Neutrino, data directory) as the running services.
Start the app first, then in another terminal:

```bash
# General form
flatpak run org.joinmarketng.JamNG cli <command> [args...]

# Wallet info / balance
flatpak run org.joinmarketng.JamNG cli jm-wallet info \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic

# Fidelity bonds: refresh a registered bond's on-chain UTXO info
flatpak run org.joinmarketng.JamNG cli jm-wallet sync-bonds

# Fidelity bonds: rediscover bonds by scanning all timelocks (recovery)
flatpak run org.joinmarketng.JamNG cli jm-wallet recover-bonds
```

The `cli` runner reads the running instance's ports and credentials from
`<data-dir>/run/env`, so the app must be running for Neutrino-backed commands to
connect. `jmwalletd` already runs a bond-aware sync on every wallet `utxos` and
`display` request, so a funded fidelity bond shows up in JAM automatically after
a refresh; `sync-bonds` is only needed to update the offline registry view used
by `jm-wallet list-bonds`.

## Data directory

The Flatpak is sandboxed. All state lives in:

```
~/.var/app/org.joinmarketng.JamNG/.joinmarket-ng/
```

This is separate from the standard `~/.joinmarket-ng/` used by a non-Flatpak
install. Native `jm-wallet`, maker, and taker commands use `.mnemonic` files.
To use an existing JoinMarket NG CLI wallet in the Flatpak, copy it into the
Flatpak data directory:

```bash
cp ~/.joinmarket-ng/wallets/default.mnemonic \
   ~/.var/app/org.joinmarketng.JamNG/.joinmarket-ng/wallets/
```

JAM uses `jmwalletd`, whose encrypted `JMNG` container currently has a `.jmdat`
filename for JAM/API compatibility. That file is distinct from both the native
CLI `.mnemonic` file and joinmarket-clientserver's JMDAT format. A
joinmarket-clientserver `.jmdat` wallet cannot be copied in and opened by JAM or
passed to `jm-wallet`. JoinMarket NG daemon wallet files can be copied between
Flatpak and non-Flatpak `jmwalletd` installations without changing their names.

If a wallet is encrypted, you are prompted for its password when you select a
daemon wallet in the UI or use an encrypted mnemonic with a `jm-wallet` CLI
command.

## Other networks

Mainnet uses the base data directory directly. Signet and regtest are supported
for testing via `--network`, and each gets its own sub-directory so wallets and
state never mix:

```bash
flatpak run org.joinmarketng.JamNG --network signet
```

```
~/.var/app/org.joinmarketng.JamNG/.joinmarket-ng/          # mainnet
~/.var/app/org.joinmarketng.JamNG/.joinmarket-ng/signet/   # signet
~/.var/app/org.joinmarketng.JamNG/.joinmarket-ng/regtest/  # regtest
```

Pass the same `--network` flag to `cli` commands so they use the matching data
directory. Addresses derived from the same mnemonic differ between mainnet and
the test networks because the BIP32 coin-type differs (`0'` for mainnet, `1'`
for signet/testnet), so a wallet copied across networks shows different
addresses.
