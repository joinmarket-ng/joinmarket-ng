# Installation

This page covers the minimum path to install JoinMarket NG and run your first commands.

For day-to-day usage, continue with:

- [Wallet guide](README-jmwallet.md)
- [Taker guide](README-taker.md)
- [Maker guide](README-maker.md)

## Requirements

- Linux or macOS (Windows is supported via a manual install, see
  [Windows section](#windows-manual-install))
- Python 3.11+
- A Bitcoin backend:
  - `descriptor_wallet` (Bitcoin Core, recommended), or
  - `neutrino` (light client)

## Recommended Install (Linux/macOS)

```bash
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash
source ~/.joinmarket-ng/activate.sh
```

What this does:

- creates `~/.joinmarket-ng/venv`
- installs `jmcore`, `jmwallet`, `jm-maker`, and `jm-taker`
- creates `~/.joinmarket-ng/config.toml`
- installs/configures Tor unless you pass `--skip-tor`
- installs static shell completion scripts for bash and zsh (near-instant tab completion)

Common options:

```bash
# taker only
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --taker

# maker only
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --maker

# skip Tor setup
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --skip-tor

# update existing installation
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --update
```

## Updating

When you run `install.sh --update`, the installer:

- Upgrades all installed Python packages to the specified (or latest) version
- Resolves and installs any new or changed dependencies (so a swapped dependency, such as PyNaCl replacing libnacl, is installed rather than leaving the venv missing a module)
- Verifies the core libraries import cleanly after the update and prints actionable remediation if a runtime module is missing
- Checks your config for new settings: compares `config.toml` against the latest template and prints any new sections or keys that are available
- Refreshes shell completions and Tor configuration

Your existing config is never modified. If new settings are available, the installer prints them so you can add them manually from `config.toml.template`.

## Supply-chain Security

The installer protects the two distinct parts of an install differently.

JoinMarket NG's own code is verified against GPG signatures. The installer resolves the requested version to a commit hash, then refuses to proceed unless a trusted key (committed to `signatures/` in the repository) has signed a manifest whose `commit:` matches that exact hash. The install is then pinned to the verified commit, so a tag that gets repointed afterwards cannot smuggle in different code. You can bypass this with `--skip-verify` (not recommended); it is auto-enabled for `--dev` / `--version main`, which have no release signatures by design.

Third-party Python dependencies use a layered model so they stay both flexible and reproducible:

- Each package's `pyproject.toml` declares minimal version ranges, so a normal `pip install` keeps working across Python versions and resolves conflicts.
- Each package ships a lock file (`requirements.txt`) generated with `pip-compile --generate-hashes`. Because `pip-compile` records hashes for every distribution file of each pinned version (all wheel tags plus the sdist), the locks are portable across Python versions and platforms.
- By default the installer fetches the release's `requirements.txt` from the verified commit and installs dependencies with `pip --require-hashes`, so any tampered artifact is rejected. The git-based JoinMarket NG packages are then installed with `--no-deps` (pip cannot hash a git checkout).
- Hash verification is a hard requirement: if it cannot be satisfied (for example no pre-built wheel exists for your platform/Python, forcing an un-hashable source build), the install aborts rather than silently weakening integrity. Rerun with `--no-hash-deps` to make opting out an explicit, informed choice.
- `--no-hash-deps` skips hash verification and only version-pins (still preventing silent upstream upgrades), printing a security warning that hashes are not verified.

```bash
# default: hash-checked dependencies (no flag needed)
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash

# opt out of hash verification (version-pinning only)
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --no-hash-deps
```

The `requirements.txt` lock files are the single source of truth and are regenerated from `pyproject.toml` by `scripts/update-deps.sh`, so maintainers never hand-edit pins.

## Configure Backend

Edit `~/.joinmarket-ng/config.toml`.

If this is a manual/source install and the file does not exist yet:

```bash
mkdir -p ~/.joinmarket-ng/wallets
chmod 700 ~/.joinmarket-ng ~/.joinmarket-ng/wallets
curl -fsSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/jmcore/src/jmcore/data/config.toml.template -o ~/.joinmarket-ng/config.toml
```

### Bitcoin Core (`descriptor_wallet`, recommended)

```toml
[bitcoin]
backend_type = "descriptor_wallet"
rpc_url = "http://127.0.0.1:8332"
rpc_user = "your_rpc_user"
rpc_password = "your_rpc_password"
```

Cookie-based authentication (the default when `bitcoind` is started without
explicit `rpcuser`/`rpcpassword`) works as well and avoids keeping RPC
credentials in `config.toml`:

```toml
[bitcoin]
backend_type = "descriptor_wallet"
rpc_url = "http://127.0.0.1:8332"
rpc_cookie_file = "~/.bitcoin/.cookie"
```

`rpc_cookie_file` is mutually exclusive with `rpc_user`/`rpc_password`: set
one pair or the other, not both.

### Neutrino (light client)

```toml
[bitcoin]
backend_type = "neutrino"
neutrino_url = "https://127.0.0.1:8334"
neutrino_tls_cert = "~/.joinmarket-ng/neutrino/tls.cert"
neutrino_auth_token_file = "~/.joinmarket-ng/neutrino/auth_token"
```

JoinMarket NG does not generate this cert/token itself today. You need to
copy them from your neutrino-api instance once, then keep them in:

- `~/.joinmarket-ng/neutrino/tls.cert`
- `~/.joinmarket-ng/neutrino/auth_token`

Create the directory:

```bash
mkdir -p ~/.joinmarket-ng/neutrino
chmod 700 ~/.joinmarket-ng/neutrino
```

Neutrino server example (Docker):

On Linux, add your user to the Docker group once (skip if Docker already works without `sudo`):

```bash
sudo usermod -aG docker "$USER"
newgrp docker
```

```bash
docker run -d \
  --name neutrino \
  --restart unless-stopped \
  -p 8334:8334 \
  -v neutrino-data:/data/neutrino \
  -e NETWORK=mainnet \
  ghcr.io/m0wer/neutrino-api
```

Copy credentials from neutrino-api into JoinMarket NG config directory:

```bash
docker cp neutrino:/data/neutrino/tls.cert ~/.joinmarket-ng/neutrino/tls.cert
docker cp neutrino:/data/neutrino/auth_token ~/.joinmarket-ng/neutrino/auth_token
chmod 600 ~/.joinmarket-ng/neutrino/tls.cert ~/.joinmarket-ng/neutrino/auth_token
```

If you previously used `http://` neutrino:

1. Switch `neutrino_url` to `https://...`
2. Add `neutrino_tls_cert`
3. Add `neutrino_auth_token_file` (or `neutrino_auth_token`)
4. Restart JoinMarket NG

On low-power hardware, initial Neutrino sync can take significantly longer (for example, Raspberry Pi 4: ~20 minutes sync plus long prefetch).

## First Run

Create a wallet and inspect addresses:

```bash
jm-wallet generate
jm-wallet info
```

Then either:

```bash
# mix coins as taker
jm-taker coinjoin --amount 1000000 --destination INTERNAL

# or run maker bot
jm-maker start
```

## Manual Install (from source)

Use this for development or custom environments.

Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y git build-essential libffi-dev libsodium-dev pkg-config python3 python3-venv
```

macOS:

```bash
brew install libsodium pkg-config python3
```

Install packages:

```bash
git clone https://github.com/joinmarket-ng/joinmarket-ng.git
cd joinmarket-ng
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

python -m pip install -e ./jmcore
python -m pip install -e ./jmwallet
python -m pip install -e ./maker
python -m pip install -e ./taker
```

Shell completions are pre-generated and installed automatically by the installer.
For editable (development) installs, source the static scripts from the repo:

```bash
# bash
source completions/jm-wallet.bash
source completions/jm-maker.bash
source completions/jm-taker.bash
source completions/jmwalletd.bash

# zsh (add to .zshrc)
for f in completions/*.zsh; do source "$f"; done
```

To regenerate after CLI changes:

```bash
python scripts/generate_completions.py
```

## Tor Notes

- Taker and orderbook watcher require Tor SOCKS (`127.0.0.1:9050`)
- Maker additionally uses Tor control (`127.0.0.1:9051`) for ephemeral onion services
- If you edit Tor config, restart Tor (`sudo systemctl restart tor` on Linux, `brew services restart tor` on macOS)
- Directory server usually runs as a Tor hidden service in Docker (see [Directory Server](README-directory-server.md))

On Debian/Ubuntu maker setups, Tor cookie auth often requires `debian-tor` group access:

```bash
sudo usermod -aG debian-tor "$USER"
newgrp debian-tor
```

## Troubleshooting

- `jm-wallet: command not found`: run `source ~/.joinmarket-ng/activate.sh`
- build dependency errors on Linux: install `build-essential libffi-dev libsodium-dev pkg-config`
- Python venv issues: install `python3-venv`
- RPC failures: verify Bitcoin Core is reachable and credentials in `config.toml` are correct
- `RPC error -32601: Method not found` on `listwallets` (or any wallet
  RPC): your Bitcoin Core node has wallet support disabled. Make sure
  `bitcoind` is started without `-disablewallet=1` and that `disablewallet=1`
  is not set in `bitcoin.conf` (the descriptor wallet backend cannot work
  against a wallet-disabled node). Confirm with `bitcoin-cli listwallets`
  returning a JSON array.

### Tracking Wallet Sync Progress

After importing a wallet (especially one with a long history), the
`descriptor_wallet` backend asks Bitcoin Core to scan the chain for the
imported descriptors. This can take **minutes to several hours** depending
on the wallet depth and the node hardware -- spinning disks and
Raspberry Pi-class hosts are at the slow end.

`jm-wallet info` will report only the fidelity bond balance until the
underlying scan finishes. Before suspecting a bug, check that the scan
is actually still running.

The descriptor wallet inside Bitcoin Core is named deterministically from
the mnemonic fingerprint and network, in the form
`jm_<fingerprint>_<network>` (for example
`jm_abc12345_mainnet`). List the loaded wallets to find the active one,
then export it as a shell variable:

```bash
bitcoin-cli listwallets
WALLET=jm_abc12345_mainnet            # replace with your actual name
RPCWALLET="bitcoin-cli -rpcwallet=$WALLET"
```

**Is the node still scanning?** ``getwalletinfo`` reports a non-null
``scanning`` object while a scan is in flight, with a ``progress`` field
between 0.0 and 1.0:

```bash
$RPCWALLET getwalletinfo | jq '{scanning: .scanning, txcount: .txcount, balance: .balance}'
```

When ``scanning`` becomes ``false``, the scan is finished -- if balances
are still missing at that point it is a real problem rather than just
slowness.

**Are descriptors imported with the expected ranges?** A partially
imported wallet shows up here as a missing path or a smaller-than-expected
``range``:

```bash
$RPCWALLET listdescriptors | jq '.descriptors[] | {desc, range, active, internal}'
```

Each external/internal mixdepth pair adds two descriptors (`/0/N/*` and
`/1/N/*`). The ``range`` upper bound should be at least the deepest used
address index plus the gap-limit buffer. By default the imported range is
``scan_range`` (1000) and it auto-expands as addresses are used; the buffer
kept ahead of the highest used index is ``gap_limit`` (20). If used
addresses sit beyond the imported range (common for wallets migrated from
legacy joinmarket-clientserver), widen it once with
``jm-wallet rescan --scan-depth N``. See
[Wallet Scanning](technical/wallet-scanning.md) for the full model.

**What is the node itself doing?** Useful when ``scanning`` returns
``false`` but balances still look wrong:

```bash
bitcoin-cli getblockchaininfo \
  | jq '{blocks, headers, verificationprogress, initialblockdownload, pruned}'
bitcoin-cli getindexinfo            # txindex / coinstatsindex / blockfilterindex
bitcoin-cli getmempoolinfo
```

**Force a one-shot rescan** when the descriptors look healthy but the
node missed transactions (e.g. after a long downtime or a manual
`importdescriptors` outside JoinMarket NG):

```bash
$RPCWALLET rescanblockchain $START_HEIGHT
```

Use the wallet creation height as ``$START_HEIGHT``. ``getwalletinfo``
shows ``birthtime``; for an imported BIP39 wallet, set this to the
earliest possible block height that could contain your funds.

Prefer the JoinMarket NG wrappers when possible (they print a
before/after coverage snapshot and respect the wallet's recorded
creation height as a floor):

```bash
jm-wallet info --scan-status      # show current Core scan coverage
jm-wallet rescan                  # kick off rescan and poll until complete
                                  # (Ctrl-C is safe: the scan keeps running
                                  # server-side; re-check with --scan-status)
jm-wallet rescan --scan-depth N   # widen the address range to N, then rescan
```

**Cross-check balances and UTXOs** without involving JoinMarket NG:

```bash
$RPCWALLET getbalances
$RPCWALLET listunspent 0 9999999 '[]' true
```

If these report the expected funds but `jm-wallet info` does not, the
issue is on the JoinMarket NG side -- file a bug with the output of
``jm-wallet debug-info`` (which redacts sensitive data).

For Neutrino backends the equivalent diagnostics live on the
neutrino-api server itself rather than via `bitcoin-cli`. See
[Neutrino TLS](technical/neutrino-tls.md) for credentials and the
neutrino-api project's own `/status` endpoint for sync state.

## Uninstall

There is no automatic uninstaller. Uninstalling has three parts: the
Python packages, the JoinMarket NG data directory, and the descriptor
wallet inside Bitcoin Core. Pick the steps that apply to your install.

> WARNING: the data directory holds your encrypted wallet seeds, the
> fidelity-bond registry, and other state. Back it up before deleting
> anything. You will be able to recover the funds with the mnemonic alone.

### 1) Python packages and virtualenv

If you used `install.sh` (recommended path), removing the venv removes
all installed JoinMarket NG packages and shell entry points at once:

```bash
rm -rf "${JMNG_VENV_DIR:-$HOME/.joinmarket-ng/venv}"
```

If you installed into a system Python or another venv, uninstall the
packages explicitly:

```bash
pip uninstall jmcore jmwallet jmwalletd jm-taker jm-maker jm-tumbler \
              jm-directory-server joinmarket-orderbook-watcher
```

Static shell completions installed by `install.sh` live alongside the
venv and are removed with it. If you also added a manual `source ...`
line to `~/.bashrc` / `~/.zshrc` (for example for `activate.sh`),
remove that line too.

### 2) Data directory

The data directory defaults to `~/.joinmarket-ng` and is configurable
via `$JOINMARKET_DATA_DIR`. It contains:

- `config.toml`
- `wallets/` (encrypted seeds and the fidelity-bond registry)
- Tor state, log files, and per-wallet caches

```bash
rm -rf "${JOINMARKET_DATA_DIR:-$HOME/.joinmarket-ng}"
```

### 3) Descriptor wallet inside Bitcoin Core

When using the `descriptor_wallet` backend, JoinMarket NG creates a
watch-only descriptor wallet inside Bitcoin Core named
`jm_<fingerprint>_<network>` (for example `jm_abc12345_mainnet`). It is
not removed by uninstalling the Python packages.

List JoinMarket NG wallets currently loaded:

```bash
bitcoin-cli listwallets | jq '.[] | select(startswith("jm_"))'
```

Unload then delete each one. Bitcoin Core has no RPC to remove wallet
files, so the directory must be removed manually after unloading:

```bash
WALLET="jm_<fingerprint>_<network>"
bitcoin-cli unloadwallet "$WALLET"
# Remove the on-disk wallet directory. Adjust the path if your
# Bitcoin Core datadir is not the default ~/.bitcoin (use
# `bitcoin-cli getwalletinfo` while loaded to confirm the location).
rm -rf "$HOME/.bitcoin/wallets/$WALLET"
```

On Neutrino-only installs there is no Bitcoin Core wallet to clean up.

## Windows (Manual Install)

`install.sh` is bash-only and targets `apt`/`brew`, so Windows users cannot
use it directly. The supported path is a manual pip install of the
JoinMarket NG Python components plus a Tor daemon. CI exercises this exact
path on `windows-latest` against the public signet directory nodes; if you
follow these steps and the smoke at the end fails, please file a bug.

Prerequisites:

- Windows 10 or later (or Windows Server 2022+).
- Python 3.11+ from python.org (tick "Add Python to PATH" during install).
- PowerShell 7+ (or built-in Windows PowerShell 5).
- Tor (we use the Tor Project Expert Bundle; Tor Browser is not required).

### 1. Install joinmarket-ng

Clone the repository and install the Python components into a venv:

```powershell
git clone https://github.com/joinmarket-ng/joinmarket-ng.git
cd joinmarket-ng
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install .\jmcore .\taker .\jmwallet
```

Add `--maker` (`pip install .\maker`) if you want to run as a maker.

### 2. Install and start Tor

Download the Tor Expert Bundle from
<https://www.torproject.org/download/tor/>, extract it, and start `tor.exe`
with the default SOCKS port (9050):

```powershell
# Example: with the bundle extracted to C:\tor
Start-Process -FilePath C:\tor\tor\tor.exe -ArgumentList "--SocksPort","9050"
```

Wait until the log reports "Bootstrapped 100%". Keep the window open while
you use JoinMarket NG, or install Tor as a service if you prefer.

### 3. Verify connectivity

Fetch the public signet orderbook over Tor (this proves that Tor is reachable
and the installed software can speak the JoinMarket directory protocol):

```powershell
python scripts\check_signet_orderbook.py --min-offers 1
```

A successful run prints `OK: signet orderbook reachable, N offers`. Failures
print the underlying error and exit non-zero.

## Next Docs

- [Wallet](README-jmwallet.md)
- [Taker](README-taker.md)
- [Maker](README-maker.md)
- [Technical Documentation](technical/index.md)
