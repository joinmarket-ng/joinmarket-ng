# Advanced Installation and Removal

Use these paths only when the [recommended installation](install.md) does not
fit your environment. Configure the backend and Tor afterward in
[setup](setup.md).

## Installation Profiles

The default installer profile includes maker, taker, tumbler, and orderbook
watcher. `--maker` and `--taker` install restricted profiles without the tumbler
or watcher; `--orderbook-watcher` installs the watcher alone. For an existing
authenticated installation missing a component, use the saved installer:

```bash
bash ~/.joinmarket-ng/install.sh --orderbook-watcher
source ~/.joinmarket-ng/activate.sh
```

Use the complete profile (no profile flag) for `jm-tumbler`. See
[updating](update.md) if your older installation has no saved installer.

## Supply-chain Security

The recommended first-run bootstrap trusts GitHub and HTTPS for one download.
It downloads a versioned release installer, verifies detached signatures from
the two pinned primary fingerprints, then verifies the signed application
release and its resolved commit. Only a successfully authenticated installer is
saved for later updates.

`--skip-verify`, `--dev`, and `--version main` bypass signed release
verification. `--no-hash-deps` disables hash verification for third-party
dependencies. Do not use those options for a funded wallet. The signature
verification covers downloaded release content, not a compromised local machine
or a compromised signing quorum.

### Manual Bootstrap Verification (Optional)

For a first install without executing the HTTPS bootstrap, install GnuPG first.

```bash
# Debian/Ubuntu
(
  set -e
  sudo apt update
  sudo apt install -y gnupg
)
```

```bash
# macOS
(
  set -e
  brew install gnupg
)
```

Independently authenticate these full primary fingerprints before using this
procedure (for example, through a trusted maintainer channel or a previously
verified release):

- `1C53A412D11EF3051704419C44912E1E03005B31`
- `9253062A4F92D63459085CA62D230520212A5901`

Replace `X.Y.Z` below with a published release that has the `install.sh` asset
and both matching signatures. This uses isolated homes, exports each exact
primary key into a separate keyring, verifies both signatures, and only then
executes the downloaded local file.

```bash
(
  set -euo pipefail
  VERSION="X.Y.Z" # Replace with the published release version.
  REPO="joinmarket-ng/joinmarket-ng"
  RAW_BASE="https://raw.githubusercontent.com/$REPO/main"
  FP_ONE="1C53A412D11EF3051704419C44912E1E03005B31"
  FP_TWO="9253062A4F92D63459085CA62D230520212A5901"
  WORK_DIR=$(mktemp -d)
  trap 'rm -rf "$WORK_DIR"' EXIT

  curl -fsSL "https://github.com/$REPO/releases/download/$VERSION/install.sh" \
    -o "$WORK_DIR/install.sh"

  for FINGERPRINT in "$FP_ONE" "$FP_TWO"; do
    KEY_HOME="$WORK_DIR/key-$FINGERPRINT"
    VERIFY_HOME="$WORK_DIR/verify-$FINGERPRINT"
    mkdir -m 700 "$KEY_HOME" "$VERIFY_HOME"
    curl -fsSL "$RAW_BASE/signatures/pubkeys/$FINGERPRINT.asc" \
      -o "$WORK_DIR/$FINGERPRINT.asc"
    GNUPGHOME="$KEY_HOME" gpg --no-options --batch --import "$WORK_DIR/$FINGERPRINT.asc"
    IMPORTED_FINGERPRINT=$(GNUPGHOME="$KEY_HOME" gpg --no-options --batch --with-colons \
      --list-keys "$FINGERPRINT" | awk -F: '$1 == "fpr" { print $10; exit }')
    test "$IMPORTED_FINGERPRINT" = "$FINGERPRINT"
    GNUPGHOME="$KEY_HOME" gpg --no-options --batch --export "$FINGERPRINT" \
      > "$WORK_DIR/$FINGERPRINT.gpg"
    curl -fsSL "$RAW_BASE/signatures/$VERSION/$FINGERPRINT.install.sh.sig" \
      -o "$WORK_DIR/$FINGERPRINT.install.sh.sig"
    GNUPGHOME="$VERIFY_HOME" gpg --no-options --batch --no-default-keyring \
      --keyring "$WORK_DIR/$FINGERPRINT.gpg" --status-fd 1 --verify \
      "$WORK_DIR/$FINGERPRINT.install.sh.sig" "$WORK_DIR/install.sh" > "$WORK_DIR/status"
    if grep -Eq '^\[GNUPG:\] (REVKEYSIG|EXPKEYSIG|EXPSIG|KEYREVOKED|KEYEXPIRED|SIGEXPIRED)( |$)' "$WORK_DIR/status"; then
      exit 1
    fi
  done

  bash "$WORK_DIR/install.sh"
)
```

## Manual Install from Source

Use a source checkout for development or a custom environment. This path does
not establish the signed-release updater, so verify the revision independently
before using it with funds. Python 3.11 or later is required.

On Debian or Ubuntu:

```bash
sudo apt update
sudo apt install -y git build-essential libffi-dev libsecp256k1-dev libsodium-dev pkg-config python3 python3-venv
```

On macOS:

```bash
brew install secp256k1 libsodium pkg-config python3
```

Clone and install the components you need:

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
python -m pip install -e ./tumbler
# Optional: installs the jm-orderbook-watcher command
python -m pip install -e ./orderbook_watcher
```

For shell completions in an editable installation, source the static scripts
from the checkout:

```bash
# bash
source completions/jm-wallet.bash
source completions/jm-maker.bash
source completions/jm-taker.bash
source completions/jmwalletd.bash

# zsh (add to .zshrc)
for f in completions/*.zsh; do source "$f"; done
```

## Windows (Manual Install)

`install.sh` targets Linux and macOS. Windows requires a manual Python install,
the Tor daemon, and a source checkout. Use the signed-release path on a
supported platform for a funded wallet when possible.

Prerequisites:

- Windows 10 or later (or Windows Server 2022 or later).
- Python 3.11 or later from python.org, with "Add Python to PATH" selected.
- PowerShell 7 or later (or Windows PowerShell 5), Git, CMake, and Visual
  Studio 2022 Build Tools with the C++ build tools workload.
- Tor from the [Tor Expert Bundle](https://www.torproject.org/download/tor/).

Clone the repository and install the Python components into a virtual
environment:

```powershell
git clone https://github.com/joinmarket-ng/joinmarket-ng.git
cd joinmarket-ng
python -m venv .venv
.\.venv\Scripts\Activate.ps1
$secpSource = Join-Path $PWD "tmp\secp256k1"
$secpBuild = Join-Path $secpSource "build"
git init $secpSource
git -C $secpSource remote add origin https://github.com/bitcoin-core/secp256k1.git
git -C $secpSource fetch --depth 1 origin e3a885d42a7800c1ccebad94ad1e2b82c4df5c65
git -C $secpSource checkout --detach FETCH_HEAD
cmake -S $secpSource -B $secpBuild `
  -DBUILD_SHARED_LIBS=ON `
  -DSECP256K1_ENABLE_MODULE_RECOVERY=ON `
  -DSECP256K1_ENABLE_MODULE_ECDH=ON `
  -DSECP256K1_BUILD_TESTS=OFF `
  -DSECP256K1_BUILD_BENCHMARK=OFF `
  -DSECP256K1_BUILD_EXHAUSTIVE_TESTS=OFF
cmake --build $secpBuild --config Release
$secpDll = Get-ChildItem $secpBuild -Recurse -Filter "*secp256k1*.dll" | Select-Object -First 1
Copy-Item $secpDll.FullName .\.venv\Scripts\secp256k1.dll
python -m pip install --upgrade pip
pip install .\jmcore .\jmwallet .\taker
```

To also run a maker and tumbler:

```powershell
pip install .\maker .\tumbler
```

To install the optional orderbook watcher:

```powershell
pip install .\orderbook_watcher
jm-orderbook-watcher --help
```

Extract the Tor Expert Bundle and start `tor.exe` with its default SOCKS port:

```powershell
# Example: with the bundle extracted to C:\tor
Start-Process -FilePath C:\tor\tor.exe -ArgumentList "--SocksPort","9050"
```

Wait for Tor to report "Bootstrapped 100%". This provides the SOCKS listener;
makers also need the control-port configuration in [Tor setup](setup.md#tor).
Verify signet directory connectivity without using funds:

```powershell
python scripts\check_signet_orderbook.py --min-offers 1
```

## Removal and Wallet Safety

There is no automatic uninstaller. Stop every maker, taker, tumbler, and wallet
daemon before removing anything. Back up recovery material first:

- A wallet using a BIP39 passphrase requires both the mnemonic and the exact
  passphrase. The mnemonic alone cannot recover that wallet.
- External-key fidelity bonds require their separate external seed or key and
  derivation information; they are not recovered by the JoinMarket mnemonic.
- The data directory contains encrypted wallets and privacy-sensitive history.

Delete a wallet through the wallet command, beginning with its dry run. It
unloads the generated Bitcoin Core wallet and shows the exact local paths before
deletion:

```bash
jm-wallet delete --dry-run --core-wallet-dir "$HOME/.bitcoin/wallets"
```

Follow [Deleting a Wallet](README-jmwallet.md#deleting-a-wallet) for the
confirmed deletion, custom or remote Bitcoin Core directories, Neutrino state,
and optional history or bond-registry cleanup. Do not delete an entire data
directory with a broad recursive command. After wallet deletion, inspect and
remove only the virtual environment, configuration, or data files you intend to
discard.
