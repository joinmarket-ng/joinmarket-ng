#!/usr/bin/env bash
#
# JoinMarket-NG Installation Script
#
# When piped from curl, auto-confirms Tor setup and other prompts.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash
#   curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --maker
#   bash ~/.joinmarket-ng/install.sh --update
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
DEFAULT_VERSION="0.39.2"  # Updated on each release
REQUIRED_GPG_SIGNATURES=2
# These are trust anchors, not a list to refresh from the download server.
# Changing them requires a new installer authenticated by the previous quorum.
TRUSTED_GPG_FINGERPRINTS=(
    "1C53A412D11EF3051704419C44912E1E03005B31"
    "9253062A4F92D63459085CA62D230520212A5901"
)
INSTALLER_PROTOCOL=1
INSTALLER_SOURCE="${BASH_SOURCE[0]:-}"
VERIFIED_RELEASE_COMMIT=""
VERIFIED_RELEASE_VERSION=""
VERIFIED_SOURCE_DIR=""
CONFIG_TEMPLATE_SNAPSHOT=""
CONFIG_TEMPLATE_LABEL="previously installed"

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

debian_package_installed() {
    [[ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" == "install ok installed" ]]
}

# Check system dependencies
check_system_dependencies() {
    print_header "Checking System Dependencies"

    local missing_deps=()

    detect_os

    if [[ "$OS_TYPE" == "linux" ]] && [[ "$PKG_MANAGER" == "apt" ]]; then
        # Debian/Ubuntu/Raspberry Pi OS
        if ! debian_package_installed build-essential; then
            missing_deps+=("build-essential")
        fi
        if ! debian_package_installed ca-certificates; then
            missing_deps+=("ca-certificates")
        fi
        if ! debian_package_installed libffi-dev; then
            missing_deps+=("libffi-dev")
        fi
        if ! debian_package_installed libsodium-dev; then
            missing_deps+=("libsodium-dev")
        fi
        if ! debian_package_installed libsecp256k1-dev; then
            missing_deps+=("libsecp256k1-dev")
        fi
        if ! debian_package_installed pkg-config; then
            missing_deps+=("pkg-config")
        fi
        if ! debian_package_installed python3-dev; then
            missing_deps+=("python3-dev")
        fi
        if ! debian_package_installed python3-venv; then
            missing_deps+=("python3-venv")
        fi
        if ! debian_package_installed git; then
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
        if ! brew list libsodium &> /dev/null 2>&1; then
            missing_deps+=("libsodium")
        fi
        if ! brew list secp256k1 &> /dev/null 2>&1; then
            missing_deps+=("secp256k1")
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

    if [[ "$PKG_MANAGER" != "apt" && "$PKG_MANAGER" != "brew" ]]; then
        print_warning "Automatic dependency checks support apt and Homebrew only."
        print_warning "Install Python development tools, Git, GnuPG, curl, libsodium and libsecp256k1 using your system package manager."
    else
        print_success "All system dependencies are installed"
    fi
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

# Inspect a top-level torrc without following %include files. The globals this
# function sets are consumed immediately by the Tor setup helpers below.
analyze_torrc() {
    local torrc_path="$1"
    local line=""
    local trimmed=""
    local option=""
    local value=""

    TORRC_HAS_INCLUDE=false
    TORRC_HAS_MANAGED_BLOCK=false
    TORRC_HAS_SOCKS_PORT=false
    TORRC_HAS_CONTROL_PORT=false
    TORRC_HAS_COOKIE_AUTH=false
    TORRC_HAS_COOKIE_FILE=false
    TORRC_AUTH_CONFLICT=false

    while IFS= read -r line || [[ -n "$line" ]]; do
        trimmed="${line#"${line%%[![:space:]]*}"}"
        if [[ "$trimmed" == "## JoinMarket-NG Configuration" ]]; then
            TORRC_HAS_MANAGED_BLOCK=true
        fi
        [[ -z "$trimmed" || "${trimmed:0:1}" == "#" ]] && continue

        option="${trimmed%%[[:space:]]*}"
        [[ "$option" == "$trimmed" ]] && continue
        value="${trimmed#"$option"}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%%[[:space:]#]*}"

        case "$option" in
            [%][Ii][Nn][Cc][Ll][Uu][Dd][Ee])
                TORRC_HAS_INCLUDE=true
                ;;
            [Ss][Oo][Cc][Kk][Ss][Pp][Oo][Rr][Tt])
                if [[ "$value" == "9050" || "$value" == "127.0.0.1:9050" || \
                      "$value" == "localhost:9050" ]]; then
                    TORRC_HAS_SOCKS_PORT=true
                fi
                ;;
            [Cc][Oo][Nn][Tt][Rr][Oo][Ll][Pp][Oo][Rr][Tt])
                if [[ "$value" == "9051" || "$value" == "127.0.0.1:9051" || \
                      "$value" == "localhost:9051" ]]; then
                    TORRC_HAS_CONTROL_PORT=true
                fi
                ;;
            [Cc][Oo][Oo][Kk][Ii][Ee][Aa][Uu][Tt][Hh][Ee][Nn][Tt][Ii][Cc][Aa][Tt][Ii][Oo][Nn])
                if [[ "$value" == "1" ]]; then
                    TORRC_HAS_COOKIE_AUTH=true
                else
                    TORRC_AUTH_CONFLICT=true
                fi
                ;;
            [Cc][Oo][Oo][Kk][Ii][Ee][Aa][Uu][Tt][Hh][Ff][Ii][Ll][Ee])
                if [[ "$value" == "/run/tor/control.authcookie" ]]; then
                    TORRC_HAS_COOKIE_FILE=true
                else
                    TORRC_AUTH_CONFLICT=true
                fi
                ;;
        esac

    done < "$torrc_path"
}

torrc_needs_joinmarket_config() {
    [[ "$TORRC_HAS_SOCKS_PORT" != "true" || "$TORRC_HAS_CONTROL_PORT" != "true" || \
       "$TORRC_HAS_COOKIE_AUTH" != "true" || "$TORRC_HAS_COOKIE_FILE" != "true" ]]
}

restart_tor_service() {
    if [[ "$OS_TYPE" == "linux" ]] && command -v systemctl &> /dev/null; then
        sudo systemctl restart tor
    elif [[ "$OS_TYPE" == "macos" ]]; then
        brew services restart tor 2>/dev/null || brew services start tor
    fi
}

enable_tor_service() {
    if [[ "$OS_TYPE" == "linux" ]] && command -v systemctl &> /dev/null; then
        sudo systemctl enable tor || print_warning "Could not enable Tor to start at boot"
    fi
}

print_tor_manual_guidance() {
    local reason="$1"

    print_warning "$reason"
    print_warning "Tor configuration was not changed. Configure a SOCKS listener on 127.0.0.1:9050"
    print_warning "and cookie-authenticated control access on 127.0.0.1:9051 manually, then restart Tor."
    print_info "Use --skip-tor to leave Tor configuration entirely under manual control."
}

# Add only missing JoinMarket requirements to a candidate torrc, verify it,
# then replace the live config after preserving a backup.
configure_torrc() {
    local torrc_path="$1"
    local candidate=""
    local backup_path=""

    analyze_torrc "$torrc_path"

    if [[ "$TORRC_HAS_INCLUDE" == "true" ]]; then
        print_tor_manual_guidance "Active %include directives prevent safe duplicate detection."
        return 0
    fi

    if [[ "$TORRC_AUTH_CONFLICT" == "true" ]]; then
        print_tor_manual_guidance "Existing CookieAuthentication or CookieAuthFile settings will not be overridden."
        return 0
    fi

    if ! torrc_needs_joinmarket_config; then
        print_success "Tor is already configured for JoinMarket"
        return 0
    fi

    if [[ "$TORRC_HAS_MANAGED_BLOCK" == "true" ]]; then
        print_tor_manual_guidance "The existing JoinMarket-NG configuration block is incomplete."
        return 0
    fi

    candidate=$(mktemp "${TMPDIR:-/tmp}/joinmarket-ng-torrc.XXXXXX") || {
        print_error "Could not create a temporary Tor configuration"
        return 1
    }
    cp "$torrc_path" "$candidate" || {
        rm -f "$candidate"
        print_error "Could not read $torrc_path"
        return 1
    }

    {
        printf '\n## JoinMarket-NG Configuration\n'
        [[ "$TORRC_HAS_SOCKS_PORT" == "true" ]] || printf 'SocksPort 127.0.0.1:9050\n'
        [[ "$TORRC_HAS_CONTROL_PORT" == "true" ]] || printf 'ControlPort 127.0.0.1:9051\n'
        [[ "$TORRC_HAS_COOKIE_AUTH" == "true" ]] || printf 'CookieAuthentication 1\n'
        [[ "$TORRC_HAS_COOKIE_FILE" == "true" ]] || printf 'CookieAuthFile /run/tor/control.authcookie\n'
    } >> "$candidate"

    if ! tor --verify-config -f "$candidate" > /dev/null 2>&1; then
        rm -f "$candidate"
        print_error "Tor rejected the proposed configuration; $torrc_path was not changed."
        return 1
    fi

    backup_path="${torrc_path}.backup.$(date +%Y%m%d_%H%M%S)"
    if ! sudo cp "$torrc_path" "$backup_path"; then
        rm -f "$candidate"
        print_error "Could not back up $torrc_path; Tor configuration was not changed."
        return 1
    fi

    if ! sudo cp "$candidate" "$torrc_path"; then
        rm -f "$candidate"
        print_error "Could not replace $torrc_path; Tor configuration was not changed."
        return 1
    fi
    rm -f "$candidate"

    if ! restart_tor_service; then
        print_error "Tor restart failed; restoring the previous configuration."
        if sudo cp "$backup_path" "$torrc_path"; then
            if ! restart_tor_service; then
                print_error "Tor could not restart after restoring $backup_path."
            fi
        else
            print_error "Could not restore $torrc_path from $backup_path."
        fi
        return 1
    fi

    enable_tor_service
    print_success "Tor configured"
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
        analyze_torrc "$torrc_path"
        if [[ "$TORRC_HAS_INCLUDE" == "true" ]]; then
            print_tor_manual_guidance "Active %include directives prevent safe duplicate detection."
            return 0
        fi
        if [[ "$TORRC_AUTH_CONFLICT" == "true" ]]; then
            print_tor_manual_guidance "Existing CookieAuthentication or CookieAuthFile settings will not be overridden."
            return 0
        fi
        if ! torrc_needs_joinmarket_config; then
            print_success "Tor is already configured for JoinMarket"
            return 0
        fi

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
            configure_torrc "$torrc_path"
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

# Require an unambiguous full commit hash before release verification. The
# resolver falls back to the original ref when GitHub is unavailable, which is
# useful for explicit verification opt-outs but must not silently weaken the
# default release install path.
require_resolved_commit_hash() {
    local version="$1"
    local commit_hash="$2"

    if [[ "$commit_hash" =~ ^[0-9a-fA-F]{40}$ ]]; then
        return 0
    fi

    print_error "Could not resolve $version to a full commit hash."
    print_error "Release signature verification cannot proceed, so the install was aborted."
    print_error "Check GitHub connectivity or rerun with --skip-verify to bypass (NOT recommended)."
    return 1
}

# Public-key downloads are untrusted. Only signatures matching an embedded
# primary fingerprint count, including signatures made by its signing subkeys.
import_verification_keys() {
    local work_dir="$1"
    local fingerprint
    mkdir -p "$work_dir/gnupg" || return 1
    chmod 700 "$work_dir/gnupg" || return 1
    for fingerprint in "${TRUSTED_GPG_FINGERPRINTS[@]}"; do
        if ! curl -fsSL "https://raw.githubusercontent.com/${GITHUB_REPO}/main/signatures/pubkeys/${fingerprint}.asc" \
            -o "$work_dir/$fingerprint.asc"; then
            print_warning "Could not download public key $fingerprint"
            continue
        fi
        if ! GNUPGHOME="$work_dir/gnupg" gpg --no-options --quiet --batch --import \
            "$work_dir/$fingerprint.asc" > "$work_dir/$fingerprint.log" 2>&1; then
            print_warning "Failed to import key $fingerprint"
            sed 's/^/    /' "$work_dir/$fingerprint.log" >&2
        fi
    done
}

verify_detached_signature() {
    local work_dir="$1" fingerprint="$2" signature="$3" content="$4"
    local signer
    if ! GNUPGHOME="$work_dir/gnupg" gpg --no-options --quiet --batch --status-fd 1 \
        --verify "$signature" "$content" > "$work_dir/status" 2>/dev/null; then
        return 1
    fi
    if grep -Eq '^\[GNUPG:\] (REVKEYSIG|EXPKEYSIG|EXPSIG|KEYREVOKED|KEYEXPIRED|SIGEXPIRED)( |$)' "$work_dir/status"; then
        return 1
    fi
    signer=$(awk '$1 == "[GNUPG:]" && $2 == "VALIDSIG" {
        if (length($12) == 40) print $12; else print $3
    }' "$work_dir/status")
    if [[ "$signer" != "$fingerprint" ]]; then
        print_warning "Signature for $fingerprint was made by $signer; ignoring"
        return 1
    fi
    return 0
}

is_release_version() {
    [[ "$1" =~ ^v?(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$ ]]
}

release_is_older() {
    local candidate="${1#v}" current="${2#v}" i
    local candidate_parts current_parts
    IFS=. read -r -a candidate_parts <<< "$candidate"
    IFS=. read -r -a current_parts <<< "$current"
    for i in 0 1 2; do
        (( 10#${candidate_parts[$i]} < 10#${current_parts[$i]} )) && return 0
        (( 10#${candidate_parts[$i]} > 10#${current_parts[$i]} )) && return 1
    done
    return 1
}

# Use a subprocess so temporary downloads survive the child installer but are
# removed on failure as well as success. No downloaded shell is sourced here.
refresh_installer() (
    local version work_dir candidate fingerprint valid_sigs=0 candidate_version
    version=$(get_latest_version)
    if ! is_release_version "$version"; then
        print_error "Invalid installer release version: $version"
        exit 1
    fi
    work_dir=$(mktemp -d -t jmng-installer.XXXXXX) || exit 1
    trap 'rm -rf "$work_dir"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    candidate="$work_dir/install.sh"
    print_header "Authenticating the current installer"
    if ! curl -fsSL "https://github.com/${GITHUB_REPO}/releases/download/${version}/install.sh" -o "$candidate"; then
        print_error "Installer asset unavailable for $version. No update was performed."
        print_error "Wait for a release with signed-installer support, then retry this saved script."
        exit 1
    fi
    import_verification_keys "$work_dir" || exit 1
    for fingerprint in "${TRUSTED_GPG_FINGERPRINTS[@]}"; do
        if curl -fsSL "https://raw.githubusercontent.com/${GITHUB_REPO}/main/signatures/${version}/${fingerprint}.install.sh.sig" \
            -o "$work_dir/$fingerprint.sig" 2>/dev/null && \
            verify_detached_signature "$work_dir" "$fingerprint" "$work_dir/$fingerprint.sig" "$candidate"; then
            valid_sigs=$((valid_sigs + 1))
            print_success "Installer signature from $fingerprint"
        fi
    done
    if (( valid_sigs < REQUIRED_GPG_SIGNATURES )); then
        print_error "Installer $version needs $REQUIRED_GPG_SIGNATURES trusted signatures; found $valid_sigs."
        print_error "The release may still be awaiting signatures. Retry later; the saved installer was not changed."
        exit 1
    fi
    # Check identity only after authenticating the bytes. The handoff protocol
    # prevents running a historical script that cannot preserve trusted updates.
    candidate_version=$(sed -n 's/^DEFAULT_VERSION="\([^"]*\)".*/\1/p' "$candidate")
    if [[ "$candidate_version" != "${version#v}" ]] || \
        ! grep -qx 'INSTALLER_PROTOCOL=1' "$candidate"; then
        print_error "Signed installer does not match release $version or lacks trusted-copy support."
        exit 1
    fi
    if release_is_older "$version" "$DEFAULT_VERSION"; then
        print_error "Refusing to replace installer $DEFAULT_VERSION with older installer $version."
        exit 1
    fi
    local args=("$@")
    [[ "$MODE" == "update" ]] && args+=(--update)
    [[ "$AUTO_YES" == "true" ]] && args+=(--yes)
    # Preserve an explicit application version. Otherwise install the same
    # release selected here, not a second unauthenticated 'latest' response.
    [[ -z "$INSTALL_VERSION" ]] && args+=(--version "$version")
    JOINMARKET_DATA_DIR="$DATA_DIR" JMNG_VENV_DIR="$VENV_DIR" \
        JMNG_VERIFIED_INSTALLER="$candidate" bash "$candidate" "${args[@]}"
)

save_trusted_installer() {
    # Developer/verification opt-outs must not replace the trusted update path.
    if [[ "$SKIP_VERIFY" == "true" || "${INSTALLER_AUTHENTICATED:-false}" != "true" ]]; then
        print_warning "Trusted installer was not changed by this unverified run."
        return 0
    fi
    local candidate
    mkdir -p "$DATA_DIR" || return 1
    candidate=$(mktemp "$DATA_DIR/.install.sh.XXXXXX") || return 1
    if ! cp "$INSTALLER_SOURCE" "$candidate" || ! chmod 700 "$candidate" || \
        ! mv -f "$candidate" "$DATA_DIR/install.sh"; then
        rm -f "$candidate"
        print_error "Could not save the trusted installer."
        return 1
    fi
    print_success "Trusted installer saved. Update with: bash \"$DATA_DIR/install.sh\" --update"
}

# Verify the application commit using the same embedded primary-key allowlist
# as the installer. main stores signatures, but cannot authorize new signers.
verify_release_signature() {
    local version="$1"
    local commit_hash="$2"

    if [[ "$SKIP_VERIFY" == "true" ]]; then
        print_warning "Skipping GPG signature verification (--skip-verify or --dev)"
        return 0
    fi

    if [[ "$VERIFIED_RELEASE_COMMIT" == "$commit_hash" && "$VERIFIED_RELEASE_VERSION" == "$version" ]]; then
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

    # Ephemeral working dir and GPG home so we don't touch the user's keyring.
    local work_dir
    work_dir=$(mktemp -d -t jmng-verify.XXXXXX)
    if [[ -z "$work_dir" || ! -d "$work_dir" ]]; then
        print_error "Failed to create temporary directory for verification."
        return 1
    fi
    # Cleanup on every exit path of this function.
    local rc=1
    _verify_cleanup() {
        rm -rf "$work_dir" 2>/dev/null || true
    }

    if ! import_verification_keys "$work_dir"; then
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
    verify_signed_manifest() {
        local expected_fingerprint="$1"
        local signature_file="$2"
        local manifest_file="$3"
        if ! verify_detached_signature "$work_dir" "$expected_fingerprint" "$signature_file" "$manifest_file"; then
            return 1
        fi

        local manifest_version
        manifest_version=$(awk -F': ' '$1 == "# Version" { print $2 }' "$manifest_file")
        if [[ "${manifest_version#v}" != "${version#v}" ]]; then
            print_warning "Signed manifest version does not match $version; ignoring"
            return 1
        fi

        local manifest_commit
        manifest_commit=$(awk -F': ' '$1 == "commit" { print $2 }' "$manifest_file")
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
    local fingerprint
    for fingerprint in "${TRUSTED_GPG_FINGERPRINTS[@]}"; do
        local sig_url="$raw_base/signatures/${version}/${fingerprint}.sig"
        local sig_file="$work_dir/${fingerprint}.sig"

        if ! curl -fsSL "$sig_url" -o "$sig_file"; then
            print_warning "Failed to fetch signature $fingerprint.sig"
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
        print_success "Valid signature from $fingerprint"
    done

    if [[ $valid_sigs -ge $REQUIRED_GPG_SIGNATURES ]]; then
        print_success "Release $version verified ($valid_sigs trusted signature(s))"
        VERIFIED_RELEASE_COMMIT="$commit_hash"
        VERIFIED_RELEASE_VERSION="$version"
        rc=0
    else
        print_error "Release $version could not be verified."
        print_error "Insufficient trusted signatures. Required: $REQUIRED_GPG_SIGNATURES, Found: $valid_sigs."
        print_error "The release may still be awaiting signatures. Retry later."
        rc=1
    fi

    _verify_cleanup
    return $rc
}

# Fetch Git objects rather than trusting raw HTTP bodies merely because their
# URLs contain a commit hash. Never check out or source repository code here.
prepare_verified_source() {
    local commit="$1" fetched_commit
    [[ -n "$VERIFIED_SOURCE_DIR" ]] && return 0
    require_resolved_commit_hash "$VERSION" "$commit" || return 1
    VERIFIED_SOURCE_DIR=$(mktemp -d -t jmng-source.XXXXXX) || return 1
    if ! git -c init.templateDir= init --bare --quiet "$VERIFIED_SOURCE_DIR" || \
        ! git -C "$VERIFIED_SOURCE_DIR" -c fetch.fsckObjects=true fetch --quiet --depth 1 \
            "https://github.com/${GITHUB_REPO}.git" "$commit"; then
        print_error "Could not fetch the authenticated release source."
        return 1
    fi
    fetched_commit=$(git -C "$VERIFIED_SOURCE_DIR" rev-parse FETCH_HEAD) || return 1
    if [[ "$fetched_commit" != "$commit" ]]; then
        print_error "Fetched source does not match the authenticated commit."
        return 1
    fi
}

read_release_file() {
    local path="$1"
    if [[ "$SKIP_VERIFY" == "true" ]]; then
        curl -fsSL "https://raw.githubusercontent.com/${GITHUB_REPO}/${VERSION:-main}/$path"
    elif [[ -n "$VERIFIED_SOURCE_DIR" && -n "$VERIFIED_RELEASE_COMMIT" ]]; then
        git --no-replace-objects -C "$VERIFIED_SOURCE_DIR" show "$VERIFIED_RELEASE_COMMIT:$path"
    else
        print_error "No authenticated release source available for $path" >&2
        return 1
    fi
}

prepare_release() {
    VERSION="${INSTALL_VERSION:-$(get_latest_version)}"
    INSTALL_VERSION="$VERSION"
    if [[ "$SKIP_VERIFY" != "true" ]]; then
        local commit
        commit=$(resolve_to_commit_hash "$VERSION")
        require_resolved_commit_hash "$VERSION" "$commit" || return 1
        verify_release_signature "$VERSION" "$commit" || return 1
        prepare_verified_source "$commit" || return 1
    fi
}

cleanup_install() {
    cleanup_dep_pinning
    [[ -z "$CONFIG_TEMPLATE_SNAPSHOT" ]] || rm -f "$CONFIG_TEMPLATE_SNAPSHOT"
    [[ -z "$VERIFIED_SOURCE_DIR" ]] || rm -rf "$VERIFIED_SOURCE_DIR"
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

    prepare_verified_source "$commit" || return 1

    local work_file
    work_file=$(mktemp -t jmng-deps.XXXXXX) || {
        print_error "Could not create temporary storage for dependency locks."
        return 1
    }

    # Only the components being installed contribute their locks. jmcore and
    # jmwallet are always installed; the remaining components are conditional.
    local pkgs=("jmcore" "jmwallet")
    [[ "${INSTALL_MAKER:-true}" == "true" ]] && pkgs+=("maker")
    [[ "${INSTALL_TAKER:-true}" == "true" ]] && pkgs+=("taker")
    [[ "${INSTALL_TUMBLER:-false}" == "true" ]] && pkgs+=("tumbler")
    if [[ "${INSTALL_ORDERBOOK_WATCHER:-false}" == "true" ]] \
        || pip show joinmarket-orderbook-watcher &> /dev/null; then
        pkgs+=("orderbook_watcher")
    fi

    local pkg
    for pkg in "${pkgs[@]}"; do
        if ! read_release_file "${pkg}/requirements.txt" >> "$work_file" 2>/dev/null \
            || ! printf '\n' >> "$work_file"; then
            print_warning "Could not fetch ${pkg}/requirements.txt from the release commit."
            print_error "Cannot securely install the selected components without all dependency locks."
            rm -f "$work_file"
            return 1
        fi
    done

    DEP_HASHED_FILE="$work_file"

    # --no-hash-deps still requires an exact-version constraints view.
    if [[ "$PINNED_DEPS" != "true" ]]; then
        local constraints_file
        constraints_file=$(mktemp -t jmng-constraints.XXXXXX) || {
            print_error "Could not create temporary storage for dependency constraints."
            rm -f "$work_file"
            DEP_HASHED_FILE=""
            return 1
        }
        # Keep only "name==version" tokens: drop --hash lines, comments,
        # environment markers, inline comments and line-continuation slashes.
        if ! sed -n 's/^\([A-Za-z0-9._-]*==[^ ;#\\]*\).*/\1/p' \
            "$work_file" > "$constraints_file"; then
            print_error "Could not prepare dependency version constraints."
            rm -f "$work_file" "$constraints_file"
            DEP_HASHED_FILE=""
            return 1
        fi
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
#   * ()                    when lock pinning was intentionally disabled
# Returns non-zero only on a hard, non-recoverable failure.
apply_dep_pinning() {
    DEP_PIN_ARGS=()

    # Dev installs intentionally have no verified lock source.
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
    install_commit="${VERIFIED_RELEASE_COMMIT:-$(resolve_to_commit_hash "$VERSION" 2>/dev/null || echo "")}"
    if [ -n "$install_commit" ] && [ "$install_commit" != "$VERSION" ]; then
        export JOINMARKET_BUILD_COMMIT="${install_commit:0:7}"
    fi
    export JOINMARKET_BUILD_REF="$VERSION"

    # Verify the resolved commit against GPG signatures stored in the repo.
    # Skipped automatically for branch installs (--dev / main) and when the
    # user passes --skip-verify. On success we pin the install to the
    # verified commit hash so a tag that gets repointed after verification
    # cannot smuggle in a different commit (TOCTOU).
    if [[ "$SKIP_VERIFY" != "true" ]]; then
        require_resolved_commit_hash "$VERSION" "$install_commit" || exit 1
    fi
    if ! verify_release_signature "$VERSION" "$install_commit"; then
        exit 1
    fi
    if [[ "$SKIP_VERIFY" != "true" ]]; then
        git_base="git+https://github.com/${GITHUB_REPO}.git@${install_commit}"
        print_info "Pinned install to verified commit ${install_commit:0:12}"
    fi
    # Prepare dependency pinning anchored to the verified commit, then
    # decide how to pin: hash-checked by default (installing the verified
    # deps up front and resolving packages with --no-deps), or exact-version
    # constraints when --no-hash-deps was explicitly selected.
    prepare_dep_pinning "$install_commit" || { cleanup_dep_pinning; exit 1; }
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

    if [[ "${INSTALL_ORDERBOOK_WATCHER:-false}" == "true" ]]; then
        print_info "Installing orderbook watcher..."
        pip install "${git_base}#subdirectory=orderbook_watcher" "${pkg_extra[@]}" --quiet
        print_success "Orderbook watcher installed"
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
    local commit_hash="${VERIFIED_RELEASE_COMMIT:-$(resolve_to_commit_hash "$VERSION")}"
    if [ "$commit_hash" != "$VERSION" ]; then
        print_info "Resolved to commit: ${commit_hash:0:8}..."
    fi

    # Verify the resolved commit against GPG signatures stored in the repo.
    # See the matching block in install_packages() for design notes.
    if [[ "$SKIP_VERIFY" != "true" ]]; then
        require_resolved_commit_hash "$VERSION" "$commit_hash" || exit 1
    fi
    if ! verify_release_signature "$VERSION" "$commit_hash"; then
        exit 1
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
    local orderbook_watcher_url="${git_base}#subdirectory=orderbook_watcher"

    # Prepare dependency pinning anchored to the verified commit, then
    # decide how to pin: hash-checked by default (installing verified deps
    # up front so the resolving installs below become --no-deps), or
    # exact-version constraints when --no-hash-deps was explicitly selected.
    prepare_dep_pinning "$commit_hash" || { cleanup_dep_pinning; exit 1; }
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

    # Preserve and update an existing watcher regardless of the selected
    # role profile. Install it on demand for new watcher-only setups.
    local should_install_orderbook_watcher="${INSTALL_ORDERBOOK_WATCHER:-false}"
    if pip show joinmarket-orderbook-watcher &> /dev/null; then
        print_info "Updating orderbook watcher..."
        pip install --upgrade --force-reinstall --no-deps "$orderbook_watcher_url" --quiet
        pip install --upgrade "$orderbook_watcher_url" "$core_url" "$wallet_url" \
            "${dep_extra[@]}" --quiet
        print_success "Orderbook watcher updated"
    elif [[ "$should_install_orderbook_watcher" == "true" ]]; then
        print_info "Installing orderbook watcher..."
        pip install "$orderbook_watcher_url" "$core_url" "$wallet_url" \
            "${dep_extra[@]}" --quiet
        print_success "Orderbook watcher installed"
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

# Resolve an existing symlink so template refreshes preserve configured aliases.
config_destination() {
    local destination="$1"
    python3 -c '
from pathlib import Path
import sys

print(Path(sys.argv[1]).resolve(strict=False))
' "$destination"
}

# Create a private staged file alongside its final destination.
stage_config_destination() {
    local destination="$1"
    local resolved_destination
    resolved_destination=$(config_destination "$destination") || return 1
    mktemp "$(dirname "$resolved_destination")/.$(basename "$resolved_destination").XXXXXX"
}

# Read a release file completely before atomically replacing its destination.
install_release_config_file() {
    local release_path="$1"
    local destination="$2"
    local staged_file
    local resolved_destination
    staged_file=$(stage_config_destination "$destination") || return 1
    resolved_destination=$(config_destination "$destination") || {
        rm -f "$staged_file"
        return 1
    }

    if ! read_release_file "$release_path" > "$staged_file" 2>/dev/null || [[ ! -s "$staged_file" ]]; then
        rm -f "$staged_file"
        return 1
    fi
    chmod 600 "$staged_file" || {
        rm -f "$staged_file"
        return 1
    }
    if ! mv -f "$staged_file" "$resolved_destination"; then
        rm -f "$staged_file"
        return 1
    fi
}

install_inline_config_fallback() {
    local destination="$1"
    local staged_file
    local resolved_destination
    staged_file=$(stage_config_destination "$destination") || return 1
    resolved_destination=$(config_destination "$destination") || {
        rm -f "$staged_file"
        return 1
    }
    cat > "$staged_file" << 'EOF'
# JoinMarket-NG Configuration
# Uncomment settings to override built-in defaults.
# Full current reference: config.toml.template in this directory.
# Documentation: https://joinmarket-ng.github.io/joinmarket-ng/technical/configuration/

[bitcoin]
# rpc_url = "http://127.0.0.1:8332"
# rpc_user = ""
# rpc_password = ""
EOF
    if ! mv -f "$staged_file" "$resolved_destination"; then
        rm -f "$staged_file"
        return 1
    fi
}

release_file_exists() {
    local release_path="$1"
    [[ "$SKIP_VERIFY" != "true" && -n "$VERIFIED_SOURCE_DIR" && -n "$VERIFIED_RELEASE_COMMIT" ]] || return 1
    git --no-replace-objects -C "$VERIFIED_SOURCE_DIR" cat-file -e \
        "$VERIFIED_RELEASE_COMMIT:$release_path" 2>/dev/null
}

valid_config_template() {
    local template_file="$1"
    python3 - "$template_file" 2>/dev/null << 'PY'
from pathlib import Path
import sys
import tomllib

template = Path(sys.argv[1]).read_text(encoding="utf-8")
if not template.strip():
    raise SystemExit(1)
tomllib.loads(template)
PY
}

# Snapshot the currently installed package without importing jmcore.settings,
# which could load the user's configuration before the package is replaced.
snapshot_installed_config_template() {
    CONFIG_TEMPLATE_SNAPSHOT=""
    CONFIG_TEMPLATE_LABEL="previously installed"

    local package_template
    if ! package_template=$(python3 -c '
import importlib.util
from pathlib import Path

spec = importlib.util.find_spec("jmcore")
if spec is None or not spec.submodule_search_locations:
    raise SystemExit(1)
template = Path(next(iter(spec.submodule_search_locations))) / "data" / "config.toml.template"
if not template.is_file():
    raise SystemExit(1)
print(template)
' 2>/dev/null); then
        return 0
    fi
    if [[ ! -s "$package_template" ]] || ! valid_config_template "$package_template"; then
        return 0
    fi

    local snapshot
    snapshot=$(mktemp "${TMPDIR:-/tmp}/jmng-config-template.XXXXXX") || return 0
    if ! cp "$package_template" "$snapshot" || ! valid_config_template "$snapshot"; then
        rm -f "$snapshot"
        return 0
    fi
    CONFIG_TEMPLATE_SNAPSHOT="$snapshot"
}

print_config_comparison_unavailable() {
    print_warning "Configuration comparison unavailable; see the release notes for configuration changes."
    print_info "Your existing config.toml is unchanged."
}

report_config_template_changes() {
    if [[ -z "$CONFIG_TEMPLATE_SNAPSHOT" || ! -s "$CONFIG_TEMPLATE_SNAPSHOT" ]] || \
        ! valid_config_template "$CONFIG_TEMPLATE_SNAPSHOT"; then
        print_config_comparison_unavailable
        return 0
    fi

    local target_template
    target_template=$(mktemp "${TMPDIR:-/tmp}/jmng-target-config-template.XXXXXX") || {
        print_config_comparison_unavailable
        return 0
    }
    if ! read_release_file "jmcore/src/jmcore/data/config.toml.template" > "$target_template" 2>/dev/null || \
        ! valid_config_template "$target_template"; then
        rm -f "$target_template"
        print_config_comparison_unavailable
        return 0
    fi

    local diff_helper
    diff_helper=$(mktemp "${TMPDIR:-/tmp}/jmng-config-changelog.XXXXXX") || {
        rm -f "$target_template"
        print_config_comparison_unavailable
        return 0
    }
    if ! read_release_file "scripts/config_changelog.py" > "$diff_helper" 2>/dev/null || \
        [[ ! -s "$diff_helper" ]]; then
        rm -f "$target_template" "$diff_helper"
        print_config_comparison_unavailable
        return 0
    fi

    local diff_output
    if ! diff_output=$(python3 "$diff_helper" --from-file "$CONFIG_TEMPLATE_SNAPSHOT" \
        --to-file "$target_template" --from-label "$CONFIG_TEMPLATE_LABEL" \
        --to-label "${VERSION:-target release}" 2>/dev/null); then
        rm -f "$target_template" "$diff_helper"
        print_config_comparison_unavailable
        return 0
    fi
    rm -f "$target_template" "$diff_helper"

    [[ -n "$diff_output" ]] || return 0

    print_info "Configuration template changes are available; your config.toml is unchanged."
    if [[ "$AUTO_YES" == "true" || ! -t 0 ]]; then
        printf '%s\n' "$diff_output"
        return 0
    fi

    local response
    read -r -p "Show full configuration template changes? [Y/n] " response </dev/tty || return 0
    if [[ -z "$response" || "$response" =~ ^[Yy]$ ]]; then
        printf '%s\n' "$diff_output"
    fi
}

# Migrate config file without changing an existing user configuration.
migrate_config() {
    local config_file="$DATA_DIR/config.toml"
    local config_existed=false
    if [[ -e "$config_file" || -L "$config_file" ]]; then
        config_existed=true
    fi

    local stderr_file
    stderr_file=$(mktemp)
    CONFIG_FILE="$config_file" python3 -c '
import os
from pathlib import Path
from jmcore.settings import migrate_config

migrate_config(Path(os.environ["CONFIG_FILE"]))
' 2>"$stderr_file" || {
        print_warning "Config refresh failed (your config is unchanged)"
        rm -f "$stderr_file"
        return 0
    }
    rm -f "$stderr_file"

    if [[ "$config_existed" == "false" && ( -e "$config_file" || -L "$config_file" ) ]]; then
        print_success "Config file created at $config_file"
    fi
}

# Setup data directory and config
setup_data_directory() {
    print_header "Setting Up Configuration"

    mkdir -p "$DATA_DIR/wallets"
    chmod 700 "$DATA_DIR"
    chmod 700 "$DATA_DIR/wallets"

    # Initialize config file if it doesn't exist. Releases before the starter
    # asset intentionally retain their full-template first-run behavior.
    local config_file="$DATA_DIR/config.toml"
    if [[ ! -e "$config_file" && ! -L "$config_file" ]]; then
        print_info "Creating config file at $config_file..."

        if release_file_exists "jmcore/src/jmcore/data/config-starter.toml.template"; then
            if ! install_release_config_file \
                "jmcore/src/jmcore/data/config-starter.toml.template" "$config_file"; then
                print_warning "Failed to install the config starter, using fallback..."
                install_inline_config_fallback "$config_file" || \
                    print_warning "Could not create fallback config file"
            fi
        elif install_release_config_file \
            "jmcore/src/jmcore/data/config-starter.toml.template" "$config_file"; then
            :
        elif install_release_config_file \
            "jmcore/src/jmcore/data/config.toml.template" "$config_file"; then
            print_info "Target release has no config starter; using its full template."
        else
            print_warning "Failed to download config templates, using fallback..."
            install_inline_config_fallback "$config_file" || \
                print_warning "Could not create fallback config file"
        fi
        if [[ -e "$config_file" || -L "$config_file" ]]; then
            print_success "Config file created"
            echo ""
            print_info "Edit $config_file to customize your settings."
            echo "  Required: Configure the [bitcoin] section (RPC credentials)"
            echo "  Optional: Review [maker] and [taker] fee/privacy settings"
            echo "  Full defaults are in config.toml.template"
        else
            print_warning "Could not create config file at $config_file"
        fi
    else
        print_info "Config file already exists at $config_file"
    fi

    # Keep a reference copy of the full template alongside the config so
    # users can compare their settings after updates.
    if [[ "$(config_destination "$DATA_DIR/config.toml.template")" == "$(config_destination "$config_file")" ]]; then
        print_warning "Reference template aliases config.toml; leaving it unchanged."
    elif ! install_release_config_file \
        "jmcore/src/jmcore/data/config.toml.template" "$DATA_DIR/config.toml.template"; then
        print_warning "Could not install config.toml.template reference copy"
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
    for cmd in "${commands[@]}"; do
        for ext in bash zsh; do
            local dst="$completions_dir/${cmd}.${ext}"
            if read_release_file "completions/${cmd}.${ext}" > "$dst" 2>/dev/null; then
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
export JMNG_VENV_DIR="$VENV_DIR"
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

    if [[ "$INSTALL_MAKER" == "false" ]] \
        && [[ "$INSTALL_TAKER" == "false" ]] \
        && [[ "${INSTALL_ORDERBOOK_WATCHER:-false}" == "false" ]]; then
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
    if [[ "${INSTALL_ORDERBOOK_WATCHER:-false}" == "true" ]]; then
        echo "  4. Start the orderbook watcher: jm-orderbook-watcher"
    fi

    echo ""
    if [[ -f "$DATA_DIR/install.sh" ]]; then
        echo -e "${BLUE}To update later:${NC}"
        echo "  bash \"$DATA_DIR/install.sh\" --update"
    else
        print_warning "This unverified install did not establish a trusted update copy."
    fi
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
  bash ~/.joinmarket-ng/install.sh --update

Options:
  -h, --help          Show this help message
  -y, --yes           Automatic yes to prompts
  --update            Update existing installation
  --maker             Install maker component (installed by default)
  --taker             Install taker component (installed by default)
  --orderbook-watcher Install the orderbook watcher component
  --version VERSION   Install specific application version (default: latest)
                       The installer itself refreshes to the latest release.
  --dev               Install from main branch (for development)
  --skip-tor          Skip Tor installation and configuration
  --min-sigs N        Require at least N valid GPG signatures (default: 2)
  --skip-verify       Skip installer refresh and release signature verification
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
      By default, maker, taker, tumbler, and the orderbook watcher are installed.
      The first bootstrap trusts GitHub/HTTPS. Verified installs save a trusted
      copy at <data-dir>/install.sh for future updates. Unverified runs do not
      replace it. Older installations migrate with one final bootstrap run.

Examples:
  # Install the complete profile (default)
  curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash

  # Install maker only
  curl -sSL ... | bash -s -- --maker

  # Install taker only
  curl -sSL ... | bash -s -- --taker

  # Install the orderbook watcher
  curl -sSL ... | bash -s -- --orderbook-watcher

  # Update existing installation
  bash ~/.joinmarket-ng/install.sh --update

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
    INSTALL_ORDERBOOK_WATCHER=""
    AUTO_YES=false
    SKIP_TOR=false
    INSTALL_VERSION=""
    EXPLICIT_COMPONENTS=false
    SKIP_VERIFY=false
    # Hash-check dependencies by default; failures never weaken verification.
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
            --orderbook-watcher)
                INSTALL_ORDERBOOK_WATCHER=true
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
            --min-sigs)
                if [[ ! "${2:-}" =~ ^[1-9][0-9]*$ ]]; then
                    print_error "--min-sigs requires a positive integer"
                    exit 1
                fi
                REQUIRED_GPG_SIGNATURES="$2"
                shift 2
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
        INSTALL_ORDERBOOK_WATCHER=${INSTALL_ORDERBOOK_WATCHER:-true}
    else
        INSTALL_MAKER=${INSTALL_MAKER:-false}
        INSTALL_TAKER=${INSTALL_TAKER:-false}
        INSTALL_ORDERBOOK_WATCHER=${INSTALL_ORDERBOOK_WATCHER:-false}
    fi

    derive_install_tumbler
}

# Main
main() {
    echo ""
    echo -e "${BLUE}JoinMarket-NG Installer${NC}"
    echo ""

    parse_args "$@"

    # This marker is passed only by the locally trusted parent after signature
    # verification. Local environment control is outside the download threat
    # model, just like the explicit --skip-verify option.
    INSTALLER_AUTHENTICATED=false
    if [[ -n "$INSTALLER_SOURCE" && "${JMNG_VERIFIED_INSTALLER:-}" == "$INSTALLER_SOURCE" ]]; then
        INSTALLER_AUTHENTICATED=true
    fi
    unset JMNG_VERIFIED_INSTALLER

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

    if [[ "$SKIP_VERIFY" != "true" && "$INSTALLER_AUTHENTICATED" != "true" ]]; then
        if ! command -v gpg &> /dev/null || ! command -v curl &> /dev/null; then
            if [[ -f "$DATA_DIR/install.sh" ]]; then
                print_error "GnuPG and curl are required to authenticate an update. Install them using your system package manager."
                exit 1
            fi
            # First-use HTTPS bootstrap may provision the verifier itself.
            # Saved-copy updates never modify the system before verification.
            check_system_dependencies
        fi
        refresh_installer "$@"
        exit $?
    fi

    trap cleanup_install EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if [[ "$MODE" == "update" ]]; then
        # Update mode - check deps, update packages, and verify Tor config
        check_system_dependencies
        prepare_release
        setup_virtualenv
        snapshot_installed_config_template
        update_packages
        migrate_config
        report_config_template_changes
        create_shell_integration
        if [[ "$SKIP_TOR" == "false" ]]; then
            setup_tor
        fi
        save_trusted_installer
        print_success "JoinMarket-NG updated successfully!"
        echo ""
        echo "Restart any running maker/taker processes to use the new version."
        exit 0
    fi

    # Fresh install
    check_system_dependencies
    prepare_release

    if [[ "$SKIP_TOR" == "false" ]]; then
        setup_tor
    fi

    check_python_version
    ask_components
    setup_virtualenv
    install_packages
    setup_data_directory
    create_shell_integration
    save_trusted_installer
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
