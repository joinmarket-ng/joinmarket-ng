#!/usr/bin/env bash
#
# JoinMarket-NG Installation Script
#
# When piped from curl, auto-confirms Tor setup and other prompts.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash
#   curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --maker
#   curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --update
#
# Or run locally:
#   ./install.sh
#   ./install.sh --update
#   ./install.sh --maker --taker
#

set -e  # Exit on error

# Configuration
VENV_DIR="${JMNG_VENV_DIR:-$HOME/.joinmarket-ng/venv}"
DATA_DIR="${JOINMARKET_DATA_DIR:-$HOME/.joinmarket-ng}"
PYTHON_MIN_VERSION="3.11"
GITHUB_REPO="joinmarket-ng/joinmarket-ng"
DEFAULT_VERSION="0.33.0"  # Updated on each release

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo ""
    echo -e "${BLUE}=== $1 ===${NC}"
    echo ""
}

# Unset TLS environment variables when they incorrectly point to the
# Neutrino peer certificate. That certificate is only for the Neutrino
# backend connection and must not be used as a global HTTPS trust store.
sanitize_tls_environment() {
    local tls_vars=(
        "SSL_CERT_FILE"
        "REQUESTS_CA_BUNDLE"
        "CURL_CA_BUNDLE"
        "PIP_CERT"
        "GIT_SSL_CAINFO"
        "CMAKE_TLS_CAINFO"
    )

    local var_name=""
    local raw_value=""
    local normalized_value=""

    for var_name in "${tls_vars[@]}"; do
        raw_value="${!var_name:-}"
        if [[ -z "$raw_value" ]]; then
            continue
        fi

        normalized_value="$raw_value"
        if [[ "$normalized_value" == "~/"* ]]; then
            normalized_value="${HOME}/${normalized_value#"~/"}"
        fi

        if [[ "$normalized_value" == */neutrino/tls.cert ]]; then
            print_warning "$var_name points to Neutrino TLS cert; unsetting for installer"
            print_warning "Fix your shell config to avoid exporting $var_name=$raw_value"
            unset "$var_name"
        fi
    done
}

# Detect OS
detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        OS_TYPE="linux"
        if command -v apt &> /dev/null; then
            PKG_MANAGER="apt"
        elif command -v dnf &> /dev/null; then
            PKG_MANAGER="dnf"
        elif command -v pacman &> /dev/null; then
            PKG_MANAGER="pacman"
        else
            PKG_MANAGER="unknown"
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        OS_TYPE="macos"
        PKG_MANAGER="brew"
    else
        OS_TYPE="unknown"
        PKG_MANAGER="unknown"
    fi
}

# Check system dependencies
check_system_dependencies() {
    print_header "Checking System Dependencies"

    local missing_deps=()

    detect_os

    if [[ "$OS_TYPE" == "linux" ]] && [[ "$PKG_MANAGER" == "apt" ]]; then
        # Debian/Ubuntu/Raspberry Pi OS
        if ! dpkg -s build-essential &> /dev/null 2>&1; then
            missing_deps+=("build-essential")
        fi
        if ! dpkg -s cmake &> /dev/null 2>&1; then
            missing_deps+=("cmake")
        fi
        if ! dpkg -s ca-certificates &> /dev/null 2>&1; then
            missing_deps+=("ca-certificates")
        fi
        if ! dpkg -s libffi-dev &> /dev/null 2>&1; then
            missing_deps+=("libffi-dev")
        fi
        if ! dpkg -s libsodium-dev &> /dev/null 2>&1; then
            missing_deps+=("libsodium-dev")
        fi
        if ! dpkg -s pkg-config &> /dev/null 2>&1; then
            missing_deps+=("pkg-config")
        fi
        if ! dpkg -s python3-dev &> /dev/null 2>&1; then
            missing_deps+=("python3-dev")
        fi
        if ! dpkg -s python3-venv &> /dev/null 2>&1; then
            missing_deps+=("python3-venv")
        fi
        if ! dpkg -s git &> /dev/null 2>&1; then
            missing_deps+=("git")
        fi
        # gnupg + curl are required for release-signature verification
        # (see verify_release()). They are typically preinstalled but
        # missing on minimal Debian/Ubuntu images and the resulting
        # gpg/curl-not-found errors during --update were confusing.
        if ! command -v gpg &> /dev/null; then
            missing_deps+=("gnupg")
        fi
        if ! command -v curl &> /dev/null; then
            missing_deps+=("curl")
        fi
    elif [[ "$OS_TYPE" == "macos" ]]; then
        if ! command -v brew &> /dev/null; then
            print_error "Homebrew not found. Install from https://brew.sh"
            exit 1
        fi
        if ! brew list cmake &> /dev/null 2>&1; then
            missing_deps+=("cmake")
        fi
        if ! brew list libsodium &> /dev/null 2>&1; then
            missing_deps+=("libsodium")
        fi
        if ! brew list pkg-config &> /dev/null 2>&1; then
            missing_deps+=("pkg-config")
        fi
        # gnupg ships with macOS as ``gpg2`` only in some installs;
        # require Homebrew gnupg so verify_release() finds ``gpg``.
        if ! command -v gpg &> /dev/null; then
            missing_deps+=("gnupg")
        fi
        if ! command -v curl &> /dev/null; then
            missing_deps+=("curl")
        fi
    fi

    if [ ${#missing_deps[@]} -gt 0 ]; then
        print_warning "Missing system dependencies: ${missing_deps[*]}"
        echo ""
        echo "Please install the required dependencies first:"
        echo ""
        if [[ "$PKG_MANAGER" == "apt" ]]; then
            echo "  sudo apt update && sudo apt install -y ${missing_deps[*]}"
        elif [[ "$PKG_MANAGER" == "brew" ]]; then
            echo "  brew install ${missing_deps[*]}"
        fi
        echo ""

        # Resolve a privilege-escalation helper for apt. Running as root
        # needs no helper. Otherwise we need ``sudo`` AND the invoking
        # user must be allowed to run it. We do not silently fail later
        # when ``sudo`` is missing or denied - users hit this on minimal
        # Debian images that ship without ``sudo`` and on accounts that
        # were never added to the sudo group.
        local sudo_cmd=""
        if [[ "$PKG_MANAGER" == "apt" ]]; then
            if [[ "$EUID" -eq 0 ]]; then
                sudo_cmd=""
            elif command -v sudo &> /dev/null; then
                sudo_cmd="sudo"
            else
                print_error "Cannot install missing dependencies: ``sudo`` is not installed"
                print_error "and this script is not running as root."
                print_error "Either install ``sudo`` (as root: 'apt install sudo' then add"
                print_error "your user to the sudo group), or run this installer as root."
                exit 1
            fi
        fi

        if [[ "$AUTO_YES" == "true" ]]; then
            print_info "Attempting to install dependencies automatically..."
            if [[ "$PKG_MANAGER" == "apt" ]]; then
                $sudo_cmd apt update && $sudo_cmd apt install -y "${missing_deps[@]}" || {
                    print_error "Failed to install system dependencies. Please install them manually and re-run."
                    exit 1
                }
            elif [[ "$PKG_MANAGER" == "brew" ]]; then
                brew install "${missing_deps[@]}" || {
                    print_error "Failed to install system dependencies. Please install them manually and re-run."
                    exit 1
                }
            fi
        else
            read -p "Do you want to install them now? [Y/n] " -n 1 -r </dev/tty
            echo
            if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                if [[ "$PKG_MANAGER" == "apt" ]]; then
                    $sudo_cmd apt update && $sudo_cmd apt install -y "${missing_deps[@]}" || {
                        print_error "Failed to install system dependencies. Please install them manually and re-run."
                        exit 1
                    }
                elif [[ "$PKG_MANAGER" == "brew" ]]; then
                    brew install "${missing_deps[@]}" || {
                        print_error "Failed to install system dependencies. Please install them manually and re-run."
                        exit 1
                    }
                fi
            else
                print_error "Cannot continue without required dependencies."
                exit 1
            fi
        fi
    fi

    print_success "All system dependencies are installed"
}

# Check Python version
check_python_version() {
    print_info "Checking Python version..."

    if ! command -v python3 &> /dev/null; then
        print_error "Python 3 is not installed. Please install Python 3.11 or higher."
        echo "  For Debian/Ubuntu: sudo apt install python3 python3-dev python3-venv python3-pip"
        echo "  For macOS: brew install python3"
        exit 1
    fi

    PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')

    if python3 -c "import sys; exit(0 if sys.version_info >= (3, 11) else 1)"; then
        print_success "Python $PYTHON_VERSION detected (minimum: $PYTHON_MIN_VERSION)"
    else
        print_error "Python $PYTHON_VERSION is too old. Minimum required: $PYTHON_MIN_VERSION"
        exit 1
    fi
}

# Setup Tor
setup_tor() {
    print_header "Setting Up Tor"

    detect_os

    # Check if Tor is installed
    if command -v tor &> /dev/null; then
        print_success "Tor is already installed"
    else
        print_warning "Tor is not installed"
        echo ""
        echo "JoinMarket-NG requires Tor for privacy."
        echo ""

        if [[ "$AUTO_YES" == "true" ]]; then
            REPLY="y"
        else
            read -p "Do you want to install Tor now? [Y/n] " -n 1 -r </dev/tty
            echo
        fi

        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            print_info "Installing Tor..."
            if [[ "$PKG_MANAGER" == "apt" ]]; then
                sudo apt update && sudo apt install -y tor || {
                    print_error "Failed to install Tor. Please install it manually and re-run."
                    exit 1
                }
            elif [[ "$PKG_MANAGER" == "brew" ]]; then
                brew install tor || {
                    print_error "Failed to install Tor. Please install it manually and re-run."
                    exit 1
                }
            else
                print_warning "Please install Tor manually for your system"
                return 0
            fi
        else
            print_warning "Skipping Tor installation"
            return 0
        fi
    fi

    # Configure Tor for JoinMarket
    local torrc_path=""
    if [[ "$OS_TYPE" == "linux" ]]; then
        torrc_path="/etc/tor/torrc"
    elif [[ "$OS_TYPE" == "macos" ]]; then
        torrc_path="$(brew --prefix 2>/dev/null)/etc/tor/torrc"
    fi

    if [ -n "$torrc_path" ] && [ -f "$torrc_path" ]; then
        # Check for both ControlPort and CookieAuthFile - need both for proper setup
        if ! grep -q "^ControlPort 127.0.0.1:9051" "$torrc_path" 2>/dev/null || \
           ! grep -q "^CookieAuthFile /run/tor/control.authcookie" "$torrc_path" 2>/dev/null; then
            echo ""
            echo "Tor needs control port configuration for maker bots."
            echo ""

            if [[ "$AUTO_YES" == "true" ]]; then
                REPLY="y"
            else
                read -p "Configure Tor control port now? [Y/n] " -n 1 -r </dev/tty
                echo
            fi

            if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                sudo cp "$torrc_path" "${torrc_path}.backup.$(date +%Y%m%d_%H%M%S)"
                # Remove old JoinMarket-NG section if it exists (to replace with correct one)
                if grep -q "## JoinMarket-NG Configuration" "$torrc_path" 2>/dev/null; then
                    sudo sed -i '/^## JoinMarket-NG Configuration/,/^$/d' "$torrc_path"
                fi
                sudo bash -c "cat >> $torrc_path" << 'EOF'

## JoinMarket-NG Configuration
SocksPort 127.0.0.1:9050
ControlPort 127.0.0.1:9051
CookieAuthentication 1
CookieAuthFile /run/tor/control.authcookie
EOF
                print_success "Tor configured"

                # Restart Tor
                if [[ "$OS_TYPE" == "linux" ]] && command -v systemctl &> /dev/null; then
                    sudo systemctl restart tor
                    sudo systemctl enable tor
                elif [[ "$OS_TYPE" == "macos" ]]; then
                    brew services restart tor 2>/dev/null || brew services start tor
                fi
            fi
        else
            print_success "Tor is already configured for JoinMarket"
        fi
    fi
}

# Get latest release version from GitHub
get_latest_version() {
    local version=""
    if command -v curl &> /dev/null; then
        # Note: the GitHub API is rate-limited per source IP, so on shared CI
        # runners curl can succeed but return a rate-limit JSON without a
        # tag_name field. Fall back to DEFAULT_VERSION whenever the pipeline
        # produces an empty string instead of relying on `|| echo`, which
        # only fires when the final sed exits non-zero.
        version=$(curl -sL "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" 2>/dev/null | \
            grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/')
    fi
    if [ -z "$version" ]; then
        version="$DEFAULT_VERSION"
    fi
    echo "$version"
}

# Resolve a version/tag/branch to a commit hash
resolve_to_commit_hash() {
    local ref="$1"

    if command -v curl &> /dev/null; then
        # Try to get commit hash from GitHub API
        # First try as a branch
        local commit_hash=$(curl -sL "https://api.github.com/repos/${GITHUB_REPO}/commits/${ref}" 2>/dev/null | \
            grep '"sha":' | head -1 | sed -E 's/.*"([^"]+)".*/\1/')

        if [ -n "$commit_hash" ] && [ "$commit_hash" != "sha" ]; then
            echo "$commit_hash"
            return 0
        fi
    fi

    # Fallback: return original ref (could be a tag or commit hash already)
    echo "$ref"
}

# Verify that the resolved commit hash for $version is attested by at least
# one trusted GPG signature stored under signatures/<version>/ in the repo.
#
# Returns 0 on success, 1 on failure. Honours $SKIP_VERIFY: when true, prints
# a warning and returns 0 without verifying. Intended to be called after
# resolve_to_commit_hash so we have the commit hash the install will pull.
#
# We trust the repository's own signatures/trusted-keys.txt and pubkeys
# directory as ground truth, because the install.sh the user is running was
# itself fetched from main of the same repository, so an attacker who can
# rewrite trusted-keys.txt can also rewrite install.sh. The substantive
# protection here is that an attacker who only controls a release tag (or
# a CDN edge serving the tarball) cannot bypass GPG verification: they
# would also have to fake a signature in signatures/<version>/ that
# verifies against one of the keys committed to main of the source repo.
verify_release_signature() {
    local version="$1"
    local commit_hash="$2"

    if [[ "$SKIP_VERIFY" == "true" ]]; then
        print_warning "Skipping GPG signature verification (--skip-verify or --dev)"
        return 0
    fi

    print_header "Verifying release signatures"
    print_info "Version: $version"
    print_info "Commit:  $commit_hash"

    if ! command -v gpg &> /dev/null; then
        print_error "gpg is required for release verification but was not found."
        print_error "Install GnuPG (e.g. 'apt install gnupg' / 'brew install gnupg'),"
        print_error "or rerun with --skip-verify to bypass (NOT recommended)."
        return 1
    fi

    if ! command -v curl &> /dev/null; then
        print_error "curl is required to fetch signatures but was not found."
        return 1
    fi

    local raw_base="https://raw.githubusercontent.com/${GITHUB_REPO}/main"
    local api_base="https://api.github.com/repos/${GITHUB_REPO}/contents/signatures/${version}?ref=main"

    # Ephemeral working dir and GPG home so we don't touch the user's keyring.
    local work_dir
    work_dir=$(mktemp -d -t jmng-verify.XXXXXX)
    if [[ -z "$work_dir" || ! -d "$work_dir" ]]; then
        print_error "Failed to create temporary directory for verification."
        return 1
    fi
    local gnupg_home="$work_dir/gnupg"
    mkdir -p "$gnupg_home"
    chmod 700 "$gnupg_home"

    # Cleanup on every exit path of this function.
    local rc=1
    _verify_cleanup() {
        rm -rf "$work_dir" 2>/dev/null || true
    }

    # Fetch the trusted-keys list and import the corresponding pubkeys from
    # the repo's signatures/pubkeys/ directory. We deliberately do NOT use a
    # keyserver here: the source of truth is the repo itself, which is what
    # the install.sh the user is running also came from.
    local trusted_keys_file="$work_dir/trusted-keys.txt"
    if ! curl -fsSL "$raw_base/signatures/trusted-keys.txt" -o "$trusted_keys_file"; then
        print_error "Failed to fetch trusted-keys.txt from $raw_base"
        _verify_cleanup
        return 1
    fi

    local imported_fps=()
    while IFS=' ' read -r fingerprint name || [[ -n "$fingerprint" ]]; do
        # Skip comments and empty lines
        [[ "$fingerprint" =~ ^#.*$ || -z "$fingerprint" ]] && continue
        local pubkey_url="$raw_base/signatures/pubkeys/${fingerprint}.asc"
        local pubkey_file="$work_dir/${fingerprint}.asc"
        if curl -fsSL "$pubkey_url" -o "$pubkey_file" 2>/dev/null; then
            # Capture gpg stderr so we can surface the real error on
            # failure. The previous ``2>/dev/null`` made debugging
            # impossible (users only saw ``Failed to import key`` with
            # no context). The captured log is printed below when the
            # import fails so users can paste it in a bug report.
            local gpg_log="$work_dir/${fingerprint}.gpg.log"
            if GNUPGHOME="$gnupg_home" gpg --quiet --batch --import "$pubkey_file" > "$gpg_log" 2>&1; then
                imported_fps+=("$fingerprint")
                print_info "Imported trusted key $fingerprint ($name)"
            else
                print_warning "Failed to import key $fingerprint ($name)"
                if [[ -s "$gpg_log" ]]; then
                    print_warning "gpg said:"
                    sed -E 's/^/    /' "$gpg_log" >&2
                fi
                # Also report the first bytes of the downloaded pubkey
                # so we can tell a transport error (HTML proxy page,
                # truncated file) apart from a genuine GnuPG failure.
                local pubkey_head
                pubkey_head=$(head -c 80 "$pubkey_file" 2>/dev/null | tr -d '\r' | head -1)
                print_warning "Downloaded pubkey starts with: ${pubkey_head:-<empty>}"
            fi
        else
            print_warning "Pubkey not found in repo for $fingerprint ($name)"
        fi
    done < "$trusted_keys_file"

    if [[ ${#imported_fps[@]} -eq 0 ]]; then
        print_error "No trusted GPG keys could be imported. Aborting."
        _verify_cleanup
        return 1
    fi

    # List signatures for this version directory via the GitHub contents API.
    # We can't 'ls' a remote dir, so the API enumerates the .sig files we
    # then fetch individually. If the directory is missing, the release was
    # not signed and we refuse to install (use --skip-verify to bypass).
    local sigs_listing="$work_dir/sigs-listing.json"
    if ! curl -fsSL "$api_base" -o "$sigs_listing"; then
        print_error "No signatures directory for $version on main."
        print_error "Either the release is unsigned or the version tag is wrong."
        print_error "Rerun with --skip-verify to bypass (NOT recommended)."
        _verify_cleanup
        return 1
    fi

    # Extract <fp>.sig filenames. Use grep/sed instead of jq to avoid an
    # extra runtime dependency; the GitHub API JSON keys are stable.
    local sig_names
    sig_names=$(grep -oE '"name":[[:space:]]*"[A-F0-9]{40}\.sig"' "$sigs_listing" | sed -E 's/.*"([A-F0-9]{40}\.sig)".*/\1/')
    if [[ -z "$sig_names" ]]; then
        print_error "No signature files found under signatures/$version/."
        print_error "Rerun with --skip-verify to bypass (NOT recommended)."
        _verify_cleanup
        return 1
    fi

    # CI-first signers sign the release asset shared by all signers. Local-first
    # signers commit an individual manifest next to their signature instead.
    # Fetch the release asset once, then fall back per signer when needed.
    local shared_manifest="$work_dir/release-manifest-${version}.txt"
    local shared_manifest_url="https://github.com/${GITHUB_REPO}/releases/download/${version}/release-manifest-${version}.txt"
    local shared_manifest_available=0
    if curl -fsSL "$shared_manifest_url" -o "$shared_manifest" 2>/dev/null; then
        shared_manifest_available=1
    else
        print_warning "Shared release manifest unavailable; trying local signer manifests."
    fi

    # Verify a signature, ensure GnuPG identifies the expected trusted signer,
    # and bind the selected manifest to the exact commit being installed.
    local verified_signer=""
    verify_signed_manifest() {
        local expected_fingerprint="$1"
        local signature_file="$2"
        local manifest_file="$3"
        local status_file="$work_dir/${expected_fingerprint}.status"

        if ! GNUPGHOME="$gnupg_home" gpg --quiet --batch --status-fd 1 --verify \
            "$signature_file" "$manifest_file" > "$status_file" 2>/dev/null; then
            return 1
        fi

        verified_signer=$(awk '$1 == "[GNUPG:]" && $2 == "VALIDSIG" { print $3; exit }' \
            "$status_file")
        if [[ "$verified_signer" != "$expected_fingerprint" ]]; then
            print_warning "Signature for $expected_fingerprint was made by $verified_signer; ignoring"
            return 1
        fi

        local manifest_commit
        manifest_commit=$(awk -F': ' '$1 == "commit" { print $2; exit }' "$manifest_file" | tr -d '[:space:]')
        if [[ -z "$manifest_commit" ]]; then
            print_warning "Signed manifest from $expected_fingerprint has no 'commit' line; ignoring"
            return 1
        fi
        if [[ "$manifest_commit" != "$commit_hash" ]]; then
            print_warning "Signed manifest commit ($manifest_commit) != install commit ($commit_hash) for $expected_fingerprint"
            return 1
        fi
        return 0
    }

    local valid_sigs=0
    local signers=()
    local sig_name
    while IFS= read -r sig_name; do
        [[ -z "$sig_name" ]] && continue
        local fingerprint="${sig_name%.sig}"

        # Only trust signatures whose fingerprint is in trusted-keys.txt.
        local trusted=0
        local imp_fp
        for imp_fp in "${imported_fps[@]}"; do
            if [[ "$imp_fp" == "$fingerprint" ]]; then
                trusted=1
                break
            fi
        done
        if [[ $trusted -eq 0 ]]; then
            print_warning "Ignoring signature from untrusted key $fingerprint"
            continue
        fi

        local sig_url="$raw_base/signatures/${version}/${fingerprint}.sig"
        local sig_file="$work_dir/${fingerprint}.sig"

        if ! curl -fsSL "$sig_url" -o "$sig_file"; then
            print_warning "Failed to fetch signature $sig_name"
            continue
        fi

        local verified=0
        if [[ $shared_manifest_available -eq 1 ]] && \
            verify_signed_manifest "$fingerprint" "$sig_file" "$shared_manifest"; then
            verified=1
        else
            local local_manifest_url="$raw_base/signatures/${version}/${fingerprint}-manifest.txt"
            local local_manifest="$work_dir/${fingerprint}-manifest.txt"
            if curl -fsSL "$local_manifest_url" -o "$local_manifest" 2>/dev/null && \
                verify_signed_manifest "$fingerprint" "$sig_file" "$local_manifest"; then
                verified=1
            elif [[ $shared_manifest_available -eq 1 ]]; then
                print_warning "No valid shared or local manifest for $fingerprint"
            else
                print_warning "No valid local manifest for $fingerprint"
            fi
        fi

        if [[ $verified -eq 0 ]]; then
            continue
        fi

        valid_sigs=$((valid_sigs + 1))
        signers+=("$fingerprint")
        print_success "Valid signature from $fingerprint"
    done <<< "$sig_names"

    if [[ $valid_sigs -ge 1 ]]; then
        print_success "Release $version verified ($valid_sigs trusted signature(s))"
        rc=0
    else
        print_error "Release $version could not be verified."
        print_error "No trusted signature attested the install commit $commit_hash."
        print_error "Rerun with --skip-verify to bypass (NOT recommended)."
        rc=1
    fi

    _verify_cleanup
    return $rc
}

# Create or update virtual environment
setup_virtualenv() {
    print_header "Setting Up Virtual Environment"

    if [ -d "$VENV_DIR" ]; then
        if [[ "$MODE" == "update" ]]; then
            print_info "Using existing virtual environment at $VENV_DIR"
        else
            print_warning "Virtual environment already exists at $VENV_DIR"
            if [[ "$AUTO_YES" != "true" ]]; then
                read -p "Recreate it? (This removes existing packages) [y/N] " -n 1 -r </dev/tty
                echo
                if [[ $REPLY =~ ^[Yy]$ ]]; then
                    print_info "Removing existing virtual environment..."
                    rm -rf "$VENV_DIR"
                fi
            fi
        fi
    fi

    if [ ! -d "$VENV_DIR" ]; then
        print_info "Creating virtual environment at $VENV_DIR..."
        mkdir -p "$(dirname "$VENV_DIR")"
        python3 -m venv "$VENV_DIR"
        print_success "Virtual environment created"
    fi

    # Activate
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"
    print_success "Virtual environment activated"

    # Upgrade pip
    print_info "Upgrading pip..."
    pip install --upgrade pip --quiet
}

# Fetch the dependency lock files for the JoinMarket-NG packages from the
# given (already GPG-verified) commit. requirements.txt is the single
# source of truth (generated by scripts/update-deps.sh with
# --generate-hashes). Because pip-compile records hashes for *every*
# distribution file of each pinned version (all wheel tags + sdist), the
# locks are portable across Python versions and platforms: pip selects a
# compatible artifact and its hash is already present.
#
# This sets up two views of the same locked versions:
#   DEP_HASHED_FILE     merged hashed requirements.txt (for --require-hashes)
#   DEP_CONSTRAINTS_FILE  hash-free name==version constraints (for pip -c)
# pip enables all-or-nothing --require-hashes mode as soon as any constraint
# carries a hash, which would reject the un-hashable git checkout; the
# hash-free constraints view is what lets the default version-pin path
# coexist with the git+https package installs.
#
# Honours $SKIP_VERIFY: when verification is skipped (dev/main) there is no
# pinned commit to anchor the locks to, so pinning is disabled.
DEP_PIN_ARGS=()
DEP_HASHED_FILE=""
DEP_CONSTRAINTS_FILE=""
prepare_dep_pinning() {
    local commit="$1"

    DEP_PIN_ARGS=()
    DEP_HASHED_FILE=""
    DEP_CONSTRAINTS_FILE=""

    # Without a verified commit (dev/main install or offline) we cannot
    # anchor the locks to trusted content, so fall back to unpinned.
    if [[ "$SKIP_VERIFY" == "true" || -z "$commit" || "$commit" == "$VERSION" ]]; then
        print_warning "Dependency pinning disabled (no verified commit to anchor lock files)."
        return 0
    fi

    if ! command -v curl &> /dev/null; then
        print_warning "SECURITY: curl not found; installing dependencies WITHOUT version"
        print_warning "pinning or hash verification. Dependency integrity is NOT enforced."
        return 0
    fi

    local raw_base="https://raw.githubusercontent.com/${GITHUB_REPO}/${commit}"

    local work_file
    work_file=$(mktemp -t jmng-deps.XXXXXX) || {
        print_warning "Could not create temp file for dependency locks; skipping pinning."
        return 0
    }

    # Only the components being installed contribute their locks. jmcore and
    # jmwallet are always installed; maker/taker/tumbler are conditional.
    local pkgs=("jmcore" "jmwallet")
    [[ "${INSTALL_MAKER:-true}" == "true" ]] && pkgs+=("maker")
    [[ "${INSTALL_TAKER:-true}" == "true" ]] && pkgs+=("taker")
    [[ "${INSTALL_TUMBLER:-false}" == "true" ]] && pkgs+=("tumbler")

    local fetched=0
    local pkg
    for pkg in "${pkgs[@]}"; do
        local url="${raw_base}/${pkg}/requirements.txt"
        if curl -fsSL "$url" >> "$work_file" 2>/dev/null; then
            printf '\n' >> "$work_file"
            fetched=$((fetched + 1))
        else
            print_warning "Could not fetch ${pkg}/requirements.txt from the release commit."
        fi
    done

    if [[ $fetched -eq 0 ]]; then
        print_warning "SECURITY: no dependency lock files found for this release; installing"
        print_warning "WITHOUT version pinning or hash verification."
        rm -f "$work_file"
        return 0
    fi

    DEP_HASHED_FILE="$work_file"

    # Pre-compute the hash-free constraints view so we can fall back to
    # version-pinning if hash-checked installation is not possible.
    local constraints_file
    constraints_file=$(mktemp -t jmng-constraints.XXXXXX) || {
        constraints_file=""
    }
    if [[ -n "$constraints_file" ]]; then
        # Keep only "name==version" tokens: drop --hash lines, comments,
        # environment markers, inline comments and line-continuation slashes.
        sed -n 's/^\([A-Za-z0-9._-]*==[^ ;#\\]*\).*/\1/p' "$work_file" > "$constraints_file"
        DEP_CONSTRAINTS_FILE="$constraints_file"
    fi
    return 0
}

# Remove any temporary lock/constraints files created by prepare_dep_pinning.
cleanup_dep_pinning() {
    [[ -n "$DEP_HASHED_FILE" ]] && rm -f "$DEP_HASHED_FILE"
    [[ -n "$DEP_CONSTRAINTS_FILE" ]] && rm -f "$DEP_CONSTRAINTS_FILE"
    DEP_HASHED_FILE=""
    DEP_CONSTRAINTS_FILE=""
}

# Decide how the JoinMarket-NG package installs should pin dependencies and
# (when hash-checking) install the hash-verified dependencies up front.
#
# By default we hash-check dependencies (maximum supply-chain integrity).
# Hash checking is a hard requirement: if it fails for any reason we abort
# rather than silently weakening integrity, pointing the user at
# --no-hash-deps so opting out is an explicit, informed choice.
#
# Sets DEP_PIN_ARGS to the pip args the package installs should append:
#   * (--no-deps)           when hashed deps were installed up front
#   * (-c <constraints>)    when version-pinning (--no-hash-deps)
#   * ()                    when no locks are available at all
# Returns non-zero only on a hard, non-recoverable failure.
apply_dep_pinning() {
    DEP_PIN_ARGS=()

    # No locks available (dev install / fetch failure): nothing to pin.
    if [[ -z "$DEP_HASHED_FILE" ]]; then
        return 0
    fi

    if [[ "$PINNED_DEPS" != "true" ]]; then
        # User opted out of hash checking with --no-hash-deps. Still pin
        # exact versions to block silent upstream upgrades.
        if [[ -n "$DEP_CONSTRAINTS_FILE" ]]; then
            print_warning "SECURITY: --no-hash-deps set; dependencies are version-pinned but"
            print_warning "their hashes are NOT verified."
            DEP_PIN_ARGS=(-c "$DEP_CONSTRAINTS_FILE")
        fi
        return 0
    fi

    # Default: hash-checked install. This is a hard requirement; on any
    # failure we abort instead of weakening supply-chain integrity.
    print_info "Installing hash-verified dependencies..."
    if ! pip install --require-hashes -r "$DEP_HASHED_FILE" --quiet; then
        print_error "Hash-verified dependency installation failed."
        print_error "Dependency integrity could not be guaranteed, so the install was"
        print_error "aborted. If your platform/Python lacks a pre-built wheel for a"
        print_error "pinned version (forcing an un-hashable source build), rerun with"
        print_error "--no-hash-deps to install version-pinned dependencies WITHOUT hash"
        print_error "verification."
        return 1
    fi
    print_success "Hash-verified dependencies installed"
    DEP_PIN_ARGS=(--no-deps)
    return 0
}

# Install JoinMarket-NG packages from GitHub
install_packages() {
    print_header "Installing JoinMarket-NG"

    # Determine version to install
    if [[ -n "$INSTALL_VERSION" ]]; then
        VERSION="$INSTALL_VERSION"
    else
        VERSION=$(get_latest_version)
    fi

    print_info "Installing version $VERSION..."

    local git_base="git+https://github.com/${GITHUB_REPO}.git@${VERSION}"

    # Stamp commit/ref into the built wheels so the TUI can display them
    # post-install (issue #451). Resolve VERSION (may be a tag, branch, or
    # commit) to a short hash; on failure we leave the env unset and the
    # build hook will fall back to live git.
    local install_commit
    install_commit=$(resolve_to_commit_hash "$VERSION" 2>/dev/null || echo "")
    if [ -n "$install_commit" ] && [ "$install_commit" != "$VERSION" ]; then
        export JOINMARKET_BUILD_COMMIT="${install_commit:0:7}"
    fi
    export JOINMARKET_BUILD_REF="$VERSION"

    # Verify the resolved commit against GPG signatures stored in the repo.
    # Skipped automatically for branch installs (--dev / main) and when the
    # user passes --skip-verify. On success we pin the install to the
    # verified commit hash so a tag that gets repointed after verification
    # cannot smuggle in a different commit (TOCTOU).
    if [[ -n "$install_commit" && "$install_commit" != "$VERSION" ]]; then
        if ! verify_release_signature "$VERSION" "$install_commit"; then
            exit 1
        fi
        if [[ "$SKIP_VERIFY" != "true" ]]; then
            git_base="git+https://github.com/${GITHUB_REPO}.git@${install_commit}"
            print_info "Pinned install to verified commit ${install_commit:0:12}"
        fi
    else
        # Could not resolve to a commit (offline / API failure / dev mode).
        # verify_release_signature requires a commit to compare against, so
        # we only call it when we have one. In --dev mode SKIP_VERIFY is
        # already true; otherwise this is best-effort and we fall through.
        if [[ "$SKIP_VERIFY" != "true" ]]; then
            print_warning "Could not resolve $VERSION to a commit hash; skipping signature verification."
            print_warning "Rerun with a tagged --version to enable verification."
        fi
    fi
    # Prepare dependency pinning anchored to the verified commit, then
    # decide how to pin: hash-checked by default (installing the verified
    # deps up front and resolving packages with --no-deps), with automatic
    # fallback to version-pinning if hashes cannot be satisfied here.
    prepare_dep_pinning "$install_commit"
    apply_dep_pinning || { cleanup_dep_pinning; exit 1; }
    local pkg_extra=("${DEP_PIN_ARGS[@]}")

    print_info "Installing jmcore..."
    pip install "${git_base}#subdirectory=jmcore" "${pkg_extra[@]}" --quiet
    print_success "jmcore installed"

    print_info "Installing jmwallet..."
    pip install "${git_base}#subdirectory=jmwallet" "${pkg_extra[@]}" --quiet
    print_success "jmwallet installed"

    # Install selected components
    if [[ "$INSTALL_MAKER" == "true" ]]; then
        print_info "Installing maker..."
        pip install "${git_base}#subdirectory=maker" "${pkg_extra[@]}" --quiet
        print_success "Maker installed"
    fi

    if [[ "$INSTALL_TAKER" == "true" ]]; then
        print_info "Installing taker..."
        pip install "${git_base}#subdirectory=taker" "${pkg_extra[@]}" --quiet
        print_success "Taker installed"
    fi

    if [[ "${INSTALL_TUMBLER:-false}" == "true" ]]; then
        print_info "Installing tumbler..."
        pip install "${git_base}#subdirectory=tumbler" \
            "${git_base}#subdirectory=maker" "${git_base}#subdirectory=taker" \
            "${pkg_extra[@]}" --quiet
        print_success "Tumbler installed"
    fi

    cleanup_dep_pinning

    # Verify installation (shared with the update path so a missing
    # runtime module like ``nacl`` yields the same actionable guidance).
    verify_update_imports || exit 1
}

# Update packages
update_packages() {
    print_header "Updating JoinMarket-NG"

    # Get version
    if [[ -n "$INSTALL_VERSION" ]]; then
        VERSION="$INSTALL_VERSION"
    else
        VERSION=$(get_latest_version)
    fi

    print_info "Updating to version $VERSION..."

    # Resolve to commit hash to ensure pip detects changes
    local commit_hash=$(resolve_to_commit_hash "$VERSION")
    if [ "$commit_hash" != "$VERSION" ]; then
        print_info "Resolved to commit: ${commit_hash:0:8}..."
    fi

    # Verify the resolved commit against GPG signatures stored in the repo.
    # See the matching block in install_packages() for design notes.
    if [[ "$commit_hash" != "$VERSION" ]]; then
        if ! verify_release_signature "$VERSION" "$commit_hash"; then
            exit 1
        fi
    else
        if [[ "$SKIP_VERIFY" != "true" ]]; then
            print_warning "Could not resolve $VERSION to a commit hash; skipping signature verification."
            print_warning "Rerun with a tagged --version to enable verification."
        fi
    fi

    local git_base="git+https://github.com/${GITHUB_REPO}.git@${commit_hash}"

    # Stamp the commit/ref into the wheels so the running TUI can
    # display them later. Each package's setup.py picks these up
    # (issue #451).
    export JOINMARKET_BUILD_COMMIT="${commit_hash:0:7}"
    export JOINMARKET_BUILD_REF="$VERSION"

    # The local jmcore/jmwallet URLs. maker/taker declare ``jmcore`` and
    # ``jmwallet`` as bare dependencies; those names do not exist on
    # PyPI, so we must hand pip the git URLs explicitly. When a
    # requirement is given as a direct URL, pip uses it to satisfy the
    # matching name instead of querying PyPI. This lets us resolve and
    # install *all other* dependencies (pynacl, mnemonic, etc.) normally
    # while keeping the JoinMarket-NG packages pinned to git.
    local core_url="${git_base}#subdirectory=jmcore"
    local wallet_url="${git_base}#subdirectory=jmwallet"
    local maker_url="${git_base}#subdirectory=maker"
    local taker_url="${git_base}#subdirectory=taker"
    local tumbler_url="${git_base}#subdirectory=tumbler"

    # Prepare dependency pinning anchored to the verified commit, then
    # decide how to pin: hash-checked by default (installing verified deps
    # up front so the resolving installs below become --no-deps), with
    # automatic fallback to version-pin constraints if hashes cannot be
    # satisfied here.
    prepare_dep_pinning "$commit_hash"
    apply_dep_pinning || { cleanup_dep_pinning; exit 1; }
    local dep_extra=("${DEP_PIN_ARGS[@]}")

    # Update core libraries. We force-reinstall the JoinMarket-NG
    # packages (so a same-version-different-commit update is picked up)
    # but DO let pip resolve dependencies, so a changed dependency set
    # (e.g. the libnacl -> PyNaCl swap) is installed instead of leaving
    # the venv missing a module like ``nacl`` (issue: ModuleNotFoundError
    # 'nacl' after update). ``--force-reinstall`` is scoped to only the
    # explicitly named URLs by pip, so third-party deps that are already
    # satisfied are not needlessly rebuilt.
    print_info "Updating jmcore..."
    pip install --upgrade --force-reinstall --no-deps "$core_url" --quiet
    print_info "Updating jmwallet..."
    pip install --upgrade --force-reinstall --no-deps "$wallet_url" --quiet

    # Resolve and install any new/changed dependencies for the core
    # libraries from the git source (not PyPI, which has no jmcore /
    # jmwallet). This is what pulls in dependencies added or swapped
    # since the installed version.
    print_info "Updating jmcore/jmwallet dependencies..."
    pip install --upgrade "$core_url" "$wallet_url" "${dep_extra[@]}" --quiet
    print_success "jmcore and jmwallet updated"

    # Update/install maker (default: install if not present)
    local should_install_maker="${INSTALL_MAKER:-true}"
    if pip show jm-maker &> /dev/null; then
        print_info "Updating maker..."
        pip install --upgrade --force-reinstall --no-deps "$maker_url" --quiet
        # Resolve maker deps from git so jmcore/jmwallet are not sought
        # on PyPI and new third-party deps (e.g. pynacl) are installed.
        pip install --upgrade "$maker_url" "$core_url" "$wallet_url" "${dep_extra[@]}" --quiet
        print_success "Maker updated"
    elif [[ "$should_install_maker" == "true" ]]; then
        print_info "Installing maker..."
        pip install "$maker_url" "$core_url" "$wallet_url" "${dep_extra[@]}" --quiet
        print_success "Maker installed"
    fi

    # Update/install taker (default: install if not present)
    local should_install_taker="${INSTALL_TAKER:-true}"
    if pip show jm-taker &> /dev/null; then
        print_info "Updating taker..."
        pip install --upgrade --force-reinstall --no-deps "$taker_url" --quiet
        pip install --upgrade "$taker_url" "$core_url" "$wallet_url" "${dep_extra[@]}" --quiet
        print_success "Taker updated"
    elif [[ "$should_install_taker" == "true" ]]; then
        print_info "Installing taker..."
        pip install "$taker_url" "$core_url" "$wallet_url" "${dep_extra[@]}" --quiet
        print_success "Taker installed"
    fi

    # Preserve an existing tumbler during a minimal-profile update, while a
    # maker+taker profile installs it when it was not present before.
    local should_install_tumbler="${INSTALL_TUMBLER:-false}"
    if pip show jm-tumbler &> /dev/null; then
        print_info "Updating tumbler..."
        pip install --upgrade --force-reinstall --no-deps "$tumbler_url" --quiet
        pip install --upgrade "$tumbler_url" "$core_url" "$wallet_url" "$maker_url" "$taker_url" \
            "${dep_extra[@]}" --quiet
        print_success "Tumbler updated"
    elif [[ "$should_install_tumbler" == "true" ]]; then
        print_info "Installing tumbler..."
        pip install "$tumbler_url" "$core_url" "$wallet_url" "$maker_url" "$taker_url" \
            "${dep_extra[@]}" --quiet
        print_success "Tumbler installed"
    fi

    cleanup_dep_pinning

    # Verify the update actually produced an importable install. This
    # catches the case where a dependency swap left the venv missing a
    # runtime module (e.g. ``nacl`` after the libnacl -> PyNaCl change)
    # and gives the user an actionable remediation instead of a cryptic
    # ModuleNotFoundError the next time they launch the bot.
    verify_update_imports || exit 1

    print_success "Update complete!"
}

# Verify the venv can import the core modules and their key runtime
# dependencies. Shared by the fresh-install and update paths. Prints
# actionable remediation (and returns non-zero) when a module is missing
# so a stale/incomplete dependency set does not surface later as a
# cryptic ModuleNotFoundError.
verify_update_imports() {
    print_info "Verifying installation..."

    # Capture the import error so we can show the user exactly what is
    # missing rather than a bare boolean.
    #
    # We only import the JoinMarket-NG packages themselves. If they import
    # cleanly, every runtime dependency they actually need for this version
    # is present by definition. We must NOT hard-require version-specific
    # transitive modules like ``nacl`` here: older releases do not use
    # PyNaCl, so importing ``nacl`` directly would fail and trigger a
    # misleading "repair" that installs a dependency the release does not
    # need.
    local err
    if err=$(python3 -c "import jmcore, jmwallet" 2>&1); then
        print_success "Core libraries verified"
        return 0
    fi

    print_error "Installation verification failed: $err"

    # A known failure mode after the libnacl -> PyNaCl swap is jmcore/
    # jmwallet failing to import because ``nacl`` is missing. Only repair
    # when the failure is actually caused by a missing ``nacl`` module
    # (i.e. this version needs PyNaCl), then re-verify the packages import.
    # Keep each step on its own line (not nested ``if``s) so a failing
    # repair does not interact badly with ``set -e``.
    if echo "$err" | grep -qi "No module named 'nacl'"; then
        print_warning "Missing the 'nacl' module (PyNaCl). Attempting to install it..."
        pip install --upgrade "pynacl>=1.5.0" --quiet || true
        if python3 -c "import jmcore, jmwallet" 2>/dev/null; then
            print_success "Installed PyNaCl; core libraries verified"
            return 0
        fi
        print_error "Automatic repair failed."
    fi

    echo ""
    print_warning "To fix this manually, activate the virtual environment and install the"
    print_warning "missing dependency, then re-run the installer:"
    echo "  source \"$VENV_DIR/bin/activate\""
    echo "  pip install 'pynacl>=1.5.0'"
    return 1
}

# Migrate config file: add new sections and keys from the bundled template
migrate_config() {
    print_header "Configuration Check"

    local config_file="$DATA_DIR/config.toml"

    if [ ! -f "$config_file" ]; then
        print_info "No config file found; creating from template..."
        local stderr_file
        stderr_file=$(mktemp)
        python3 -c "
from pathlib import Path
from jmcore.settings import migrate_config
migrate_config(Path('$config_file'))
" 2>"$stderr_file" || {
            print_warning "Config creation failed"
            if [ -s "$stderr_file" ]; then
                tail -5 "$stderr_file" >&2
            fi
            rm -f "$stderr_file"
            return 0
        }
        rm -f "$stderr_file"
        if [ -f "$config_file" ]; then
            print_success "Config file created at $config_file"
        fi
        return 0
    fi

    # Config exists -- check for new settings in the template.
    print_info "Checking for new settings in the template..."
    local stderr_file
    stderr_file=$(mktemp)
    local result
    result=$(python3 -c "
from pathlib import Path
from jmcore.settings import config_diff
diffs = config_diff(Path('$config_file'))
for d in diffs:
    print(d)
" 2>"$stderr_file") || {
        print_warning "Config diff check failed (your config is unchanged)"
        rm -f "$stderr_file"
        return 0
    }
    rm -f "$stderr_file"

    if [ -z "$result" ]; then
        print_info "Config is up to date"
    else
        local section_count=0
        local key_count=0
        while IFS= read -r diff; do
            if [[ "$diff" == section:* ]]; then
                print_info "  New section available: [${diff#section:}]"
                section_count=$((section_count + 1))
            elif [[ "$diff" == key:* ]]; then
                print_info "  New setting available: ${diff#key:}"
                key_count=$((key_count + 1))
            fi
        done <<< "$result"
        local total=$((section_count + key_count))
        print_info "$total new setting(s) available in the template"
        print_info "Compare your config with config.toml.template to see details"
    fi
}

# Setup data directory and config
setup_data_directory() {
    print_header "Setting Up Configuration"

    mkdir -p "$DATA_DIR/wallets"
    chmod 700 "$DATA_DIR"
    chmod 700 "$DATA_DIR/wallets"

    # Initialize config file if it doesn't exist
    local config_file="$DATA_DIR/config.toml"
    if [ ! -f "$config_file" ]; then
        print_info "Creating config file at $config_file..."

        # Download config template from repository
        # Use VERSION if available, otherwise use main branch
        local version_tag="${VERSION:-main}"
        local config_template_url="https://raw.githubusercontent.com/$GITHUB_REPO/${version_tag}/jmcore/src/jmcore/data/config.toml.template"
        if ! curl -fsSL "$config_template_url" -o "$config_file"; then
            print_warning "Failed to download config template, using fallback..."
            # Fallback: create minimal config if download fails
            cat > "$config_file" << 'EOF'
# JoinMarket-NG Configuration
# See: https://joinmarket-ng.github.io/joinmarket-ng/
# For full template: https://github.com/joinmarket-ng/joinmarket-ng/blob/main/jmcore/src/jmcore/data/config.toml.template

# [bitcoin]
# rpc_url = "http://127.0.0.1:8332"
# rpc_user = ""
# rpc_password = ""
EOF
        fi
        print_success "Config file created"

        echo ""
        print_info "Edit $config_file to customize your settings."
        echo "  Required: Configure the [bitcoin] section (RPC credentials)"
        echo "  Optional: Review [maker] and [taker] fee/privacy settings"
        echo "  All options documented with defaults in the config file"
    else
        print_info "Config file already exists at $config_file"
    fi
}

# Install pre-generated static shell completion scripts.
# These are produced by scripts/generate_completions.py and shipped in
# the completions/ directory of the repository, so no Python subprocess
# is needed at install time or at tab-press time.
setup_cli_completion() {
    local completions_dir="$DATA_DIR/completions"
    mkdir -p "$completions_dir"
    chmod 700 "$completions_dir"

    # Determine which commands are being installed
    local commands=("jm-wallet" "jmwalletd")
    if [[ "$INSTALL_MAKER" == "true" ]]; then
        commands+=("jm-maker")
    fi
    if [[ "$INSTALL_TAKER" == "true" ]]; then
        commands+=("jm-taker")
    fi
    if [[ "${INSTALL_TUMBLER:-false}" == "true" ]]; then
        commands+=("jm-tumbler")
    fi

    local installed_count=0
    local raw_base="https://raw.githubusercontent.com/${GITHUB_REPO}/${VERSION:-main}/completions"

    for cmd in "${commands[@]}"; do
        for ext in bash zsh; do
            local dst="$completions_dir/${cmd}.${ext}"
            local url="$raw_base/${cmd}.${ext}"
            if curl -fsSL "$url" -o "$dst" 2>/dev/null; then
                chmod 644 "$dst"
                installed_count=$((installed_count + 1))
            else
                rm -f "$dst"
            fi
        done
    done

    if [[ "$installed_count" -gt 0 ]]; then
        print_success "Static shell completions installed to $completions_dir"
    else
        print_warning "Could not download shell completion scripts"
        print_warning "Run 'python scripts/generate_completions.py' to generate them locally"
    fi
}

# Create shell integration script
create_shell_integration() {
    print_header "Setting Up Shell Integration"

    mkdir -p "$DATA_DIR"
    setup_cli_completion

    local shell_script="$DATA_DIR/activate.sh"

    cat > "$shell_script" << EOF
# JoinMarket-NG Shell Integration
# Source this file to activate the environment:
#   source ~/.joinmarket-ng/activate.sh

export JOINMARKET_DATA_DIR="$DATA_DIR"
export PATH="$VENV_DIR/bin:\$PATH"

# Load generated completion scripts (bash/zsh)
if [ -n "\${BASH_VERSION:-}" ]; then
    for completion_file in "$DATA_DIR"/completions/*.bash; do
        [ -f "\$completion_file" ] || continue
        . "\$completion_file"
    done
elif [ -n "\${ZSH_VERSION:-}" ]; then
    if ! type compdef >/dev/null 2>&1; then
        autoload -Uz compinit 2>/dev/null || true
        compinit -i >/dev/null 2>&1 || true
    fi
    setopt localoptions nonomatch 2>/dev/null || true
    for completion_file in "$DATA_DIR"/completions/*.zsh; do
        [ -f "\$completion_file" ] || continue
        . "\$completion_file"
    done
fi

# Optional: Alias for convenience
alias jm-activate='source "$VENV_DIR/bin/activate"'
EOF

    chmod 644 "$shell_script"

    # Add to shell rc if not already there
    local shell_rc=""
    if [ -f "$HOME/.bashrc" ]; then
        shell_rc="$HOME/.bashrc"
    elif [ -f "$HOME/.zshrc" ]; then
        shell_rc="$HOME/.zshrc"
    fi

    if [ -n "$shell_rc" ]; then
        local source_line="source \"$shell_script\""
        if ! grep -q "joinmarket-ng/activate.sh" "$shell_rc" 2>/dev/null; then
            echo ""
            if [[ "$AUTO_YES" == "true" ]]; then
                REPLY="y"
            else
                read -p "Add JoinMarket-NG to your shell config ($shell_rc)? [Y/n] " -n 1 -r </dev/tty
                echo
            fi

            if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                echo "" >> "$shell_rc"
                echo "# JoinMarket-NG" >> "$shell_rc"
                echo "$source_line" >> "$shell_rc"
                print_success "Added to $shell_rc"
            fi
        fi
    fi
}

# Tumbler combines taker CoinJoin rounds with maker sessions. It is available
# in complete maker+taker profiles, while individual roles remain minimal.
derive_install_tumbler() {
    if [[ "$INSTALL_MAKER" == "true" ]] && [[ "$INSTALL_TAKER" == "true" ]]; then
        INSTALL_TUMBLER=true
    else
        INSTALL_TUMBLER=false
    fi
}

# Ask user for component selection
ask_components() {
    if [[ "$AUTO_YES" == "true" ]]; then
        return
    fi

    if [[ "$INSTALL_MAKER" == "false" ]] && [[ "$INSTALL_TAKER" == "false" ]]; then
        print_header "Component Selection"
        echo "Which components do you want to install?"
        echo ""
        echo "  1) Maker only (earn fees by providing liquidity)"
        echo "  2) Taker only (mix your coins for privacy)"
        echo "  3) Both Maker and Taker"
        echo "  4) Core only (libraries only, no CLI tools)"
        echo ""

        read -p "Enter your choice [1-4]: " -n 1 -r </dev/tty
        echo

        case $REPLY in
            1)
                INSTALL_MAKER=true
                INSTALL_TAKER=false
                ;;
            2)
                INSTALL_MAKER=false
                INSTALL_TAKER=true
                ;;
            3)
                INSTALL_MAKER=true
                INSTALL_TAKER=true
                ;;
            *)
                INSTALL_MAKER=false
                INSTALL_TAKER=false
                ;;
        esac
    fi

    derive_install_tumbler
}

# Print completion message
print_completion() {
    print_header "Installation Complete!"

    echo "JoinMarket-NG has been installed to: $VENV_DIR"
    echo "Configuration directory: $DATA_DIR"
    echo ""

    if [[ -f "$HOME/.bashrc" ]] || [[ -f "$HOME/.zshrc" ]]; then
        echo -e "${GREEN}To get started:${NC}"
        echo ""
        echo "  1. Start a new terminal (or run: source ~/.joinmarket-ng/activate.sh)"
        echo ""
    else
        echo -e "${GREEN}To get started:${NC}"
        echo ""
        echo "  1. Activate the environment:"
        echo "     source $VENV_DIR/bin/activate"
        echo ""
    fi

    echo "  2. Edit your configuration:"
    echo "     nano $DATA_DIR/config.toml"
    echo ""

    if [[ "$INSTALL_MAKER" == "true" ]] || [[ "$INSTALL_TAKER" == "true" ]]; then
        echo "  3. Create a wallet:"
        echo "     jm-wallet generate --save --prompt-password --output $DATA_DIR/wallets/wallet.mnemonic"
        echo ""
    fi

    if [[ "$INSTALL_MAKER" == "true" ]]; then
        echo "  4. Start maker: jm-maker start -f $DATA_DIR/wallets/wallet.mnemonic"
    fi
    if [[ "$INSTALL_TAKER" == "true" ]]; then
        echo "  4. Run CoinJoin: jm-taker coinjoin -f $DATA_DIR/wallets/wallet.mnemonic --amount 1000000"
    fi
    if [[ "${INSTALL_TUMBLER:-false}" == "true" ]]; then
        echo "  4. Build a mixing plan: jm-tumbler plan -f $DATA_DIR/wallets/wallet.mnemonic"
    fi

    echo ""
    echo -e "${BLUE}To update later:${NC}"
    echo "  curl -sSL https://raw.githubusercontent.com/${GITHUB_REPO}/main/install.sh | bash -s -- --update"
    echo ""
    echo -e "${BLUE}Documentation:${NC}"
    echo "  https://github.com/${GITHUB_REPO}"
    echo ""

    # Docker hint for advanced users
    echo -e "${YELLOW}Docker users:${NC} See the docker-compose files in maker/ and taker/ directories."
    echo "  git clone https://github.com/${GITHUB_REPO}.git && cd joinmarket-ng"
    echo ""
}

# Show help
show_help() {
    cat << 'EOF'
JoinMarket-NG Installation Script

Usage:
  curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash
  curl -sSL ... | bash -s -- [OPTIONS]
  ./install.sh [OPTIONS]

Options:
  -h, --help          Show this help message
  -y, --yes           Automatic yes to prompts
  --update            Update existing installation
  --maker             Install maker component (installed by default)
  --taker             Install taker component (installed by default)
  --version VERSION   Install specific version (default: latest)
  --dev               Install from main branch (for development)
  --skip-tor          Skip Tor installation and configuration
  --skip-verify       Skip GPG signature verification of the release
                      (NOT recommended; auto-enabled with --dev or
                      --version main since main branch is not signed)
  --no-hash-deps      Do not hash-verify third-party dependencies. By
                      default the installer installs dependencies from the
                      release's hash-checked lock files (requirements.txt)
                      for maximum supply-chain integrity, and aborts if hash
                      verification cannot be satisfied (e.g. no pre-built
                      wheel for this platform/Python). This flag instead
                      version-pins WITHOUT hash verification (still
                      preventing silent upstream upgrades).
  --venv PATH         Custom virtual environment path

Note: When piped from curl, auto-confirm is enabled by default for Tor
      configuration and other prompts. Use --skip-tor to skip Tor setup.
      By default, both maker and taker are installed.

Examples:
  # Install with both maker and taker (default)
  curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash

  # Install maker only
  curl -sSL ... | bash -s -- --maker

  # Install taker only
  curl -sSL ... | bash -s -- --taker

  # Update existing installation
  curl -sSL ... | bash -s -- --update

  # Install specific version
  curl -sSL ... | bash -s -- --version 0.9.0

Environment:
  JMNG_VENV_DIR       Custom venv path (default: ~/.joinmarket-ng/venv)
  JOINMARKET_DATA_DIR Custom data directory (default: ~/.joinmarket-ng)

EOF
}

# Parse arguments
parse_args() {
    MODE="install"
    INSTALL_MAKER=""
    INSTALL_TAKER=""
    AUTO_YES=false
    SKIP_TOR=false
    INSTALL_VERSION=""
    EXPLICIT_COMPONENTS=false
    SKIP_VERIFY=false
    # Hash-check dependencies by default (strongest supply-chain integrity);
    # auto-falls back to version-pinning if hashes cannot be satisfied here.
    PINNED_DEPS=true

    while [[ $# -gt 0 ]]; do
        case $1 in
            -h|--help)
                show_help
                exit 0
                ;;
            -y|--yes)
                AUTO_YES=true
                shift
                ;;
            --update)
                MODE="update"
                shift
                ;;
            --maker)
                INSTALL_MAKER=true
                EXPLICIT_COMPONENTS=true
                shift
                ;;
            --taker)
                INSTALL_TAKER=true
                EXPLICIT_COMPONENTS=true
                shift
                ;;
            --version)
                INSTALL_VERSION="$2"
                shift 2
                ;;
            --dev)
                INSTALL_VERSION="main"
                # No signed releases are produced for the main branch HEAD,
                # so verification is meaningless here. Operators that pass
                # --dev are explicitly opting into a moving target.
                SKIP_VERIFY=true
                shift
                ;;
            --skip-verify)
                SKIP_VERIFY=true
                shift
                ;;
            --no-hash-deps)
                PINNED_DEPS=false
                shift
                ;;
            --skip-tor)
                SKIP_TOR=true
                shift
                ;;
            --venv)
                VENV_DIR="$2"
                shift 2
                ;;
            *)
                print_error "Unknown option: $1"
                echo "Use --help for usage information"
                exit 1
                ;;
        esac
    done

    # Treat an explicit --version main as a dev install for verification
    # purposes; the main branch has no release signatures by design.
    if [[ "$INSTALL_VERSION" == "main" ]]; then
        SKIP_VERIFY=true
    fi

    # Set defaults if components not explicitly specified
    if [[ "$EXPLICIT_COMPONENTS" == "false" ]]; then
        INSTALL_MAKER=${INSTALL_MAKER:-true}
        INSTALL_TAKER=${INSTALL_TAKER:-true}
    else
        INSTALL_MAKER=${INSTALL_MAKER:-false}
        INSTALL_TAKER=${INSTALL_TAKER:-false}
    fi

    derive_install_tumbler
}

# Main
main() {
    echo ""
    echo -e "${BLUE}JoinMarket-NG Installer${NC}"
    echo ""

    parse_args "$@"

    # Guard against accidental global CA overrides from Neutrino TLS setup.
    sanitize_tls_environment

    # If stdin is not a terminal (piped from curl) and no --yes flag, auto-enable yes mode
    if [[ ! -t 0 ]] && [[ "$AUTO_YES" != "true" ]]; then
        print_info "Non-interactive mode detected (piped install), enabling auto-confirm"
        AUTO_YES=true
        # Don't auto-enable maker/taker in this case - let user specify
    fi

    # Detect if this is an update
    if [ -d "$VENV_DIR" ] && [[ "$MODE" != "update" ]]; then
        print_info "Existing installation detected at $VENV_DIR"
        if [[ "$AUTO_YES" != "true" ]]; then
            echo ""
            read -p "Do you want to update? [Y/n] " -n 1 -r </dev/tty
            echo
            if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                MODE="update"
            else
                print_info "Continuing with fresh install (will not remove existing venv)"
            fi
        else
            # In auto mode, default to update if venv exists
            MODE="update"
        fi
    fi

    if [[ "$MODE" == "update" ]]; then
        # Update mode - check deps, update packages, and verify Tor config
        check_system_dependencies
        setup_virtualenv
        update_packages
        migrate_config
        create_shell_integration
        if [[ "$SKIP_TOR" == "false" ]]; then
            setup_tor
        fi
        print_success "JoinMarket-NG updated successfully!"
        echo ""
        echo "Restart any running maker/taker processes to use the new version."
        exit 0
    fi

    # Fresh install
    check_system_dependencies

    if [[ "$SKIP_TOR" == "false" ]]; then
        setup_tor
    fi

    check_python_version
    ask_components
    setup_virtualenv
    install_packages
    setup_data_directory
    create_shell_integration
    print_completion
}

# Only run main when this file is executed directly, not when sourced.
# Sourcing is used by tests to call individual helper functions.
#
# We can't compare BASH_SOURCE[0] to $0 here, because when the script is
# piped (curl ... | bash), BASH_SOURCE[0] is empty while $0 is 'bash',
# and the comparison fails, silently skipping main and exiting 0.
# Instead, attempt `return`: it only succeeds inside a sourced file. If
# the script is executed (directly or via a pipe), `return` fails and
# main runs.
(return 0 2>/dev/null) || main "$@"
