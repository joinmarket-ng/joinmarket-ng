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
DEFAULT_VERSION="0.23.0"  # Updated on each release

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
    elif [[ "$OS_TYPE" == "macos" ]]; then
        if ! command -v brew &> /dev/null; then
            print_error "Homebrew not found. Install from https://brew.sh"
            exit 1
        fi
        if ! brew list libsodium &> /dev/null 2>&1; then
            missing_deps+=("libsodium")
        fi
        if ! brew list pkg-config &> /dev/null 2>&1; then
            missing_deps+=("pkg-config")
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

        if [[ "$AUTO_YES" == "true" ]]; then
            print_info "Attempting to install dependencies automatically..."
            if [[ "$PKG_MANAGER" == "apt" ]]; then
                sudo apt update && sudo apt install -y "${missing_deps[@]}"
            elif [[ "$PKG_MANAGER" == "brew" ]]; then
                brew install "${missing_deps[@]}"
            fi
        else
            read -p "Do you want to install them now? [Y/n] " -n 1 -r </dev/tty
            echo
            if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                if [[ "$PKG_MANAGER" == "apt" ]]; then
                    sudo apt update && sudo apt install -y "${missing_deps[@]}"
                elif [[ "$PKG_MANAGER" == "brew" ]]; then
                    brew install "${missing_deps[@]}"
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
                sudo apt update && sudo apt install -y tor
            elif [[ "$PKG_MANAGER" == "brew" ]]; then
                brew install tor
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
    if command -v curl &> /dev/null; then
        curl -sL "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" 2>/dev/null | \
            grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/' || echo "$DEFAULT_VERSION"
    else
        echo "$DEFAULT_VERSION"
    fi
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

    # Always install core libraries
    print_info "Installing jmcore..."
    pip install "${git_base}#subdirectory=jmcore" --quiet
    print_success "jmcore installed"

    print_info "Installing jmwallet..."
    pip install "${git_base}#subdirectory=jmwallet" --quiet
    print_success "jmwallet installed"

    # Install selected components
    if [[ "$INSTALL_MAKER" == "true" ]]; then
        print_info "Installing maker..."
        pip install "${git_base}#subdirectory=maker" --quiet
        print_success "Maker installed"
    fi

    if [[ "$INSTALL_TAKER" == "true" ]]; then
        print_info "Installing taker..."
        pip install "${git_base}#subdirectory=taker" --quiet
        print_success "Taker installed"
    fi

    # Verify installation
    print_info "Verifying installation..."
    if python3 -c "import jmcore; import jmwallet" 2>/dev/null; then
        print_success "Core libraries verified"
    else
        print_error "Installation verification failed"
        exit 1
    fi
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

    local git_base="git+https://github.com/${GITHUB_REPO}.git@${commit_hash}"

    # Update core libraries (force reinstall to handle same version, different commit)
    print_info "Updating jmcore..."
    pip install --upgrade --force-reinstall --no-deps "${git_base}#subdirectory=jmcore" --quiet
    print_success "jmcore updated"

    print_info "Updating jmwallet..."
    pip install --upgrade --force-reinstall --no-deps "${git_base}#subdirectory=jmwallet" --quiet
    print_success "jmwallet updated"

    # Reinstall dependencies to ensure they're satisfied
    print_info "Updating dependencies..."
    pip install --upgrade jmcore jmwallet --quiet

    # Update/install maker (default: install if not present)
    local should_install_maker="${INSTALL_MAKER:-true}"
    if pip show jm-maker &> /dev/null; then
        print_info "Updating maker..."
        pip install --upgrade --force-reinstall --no-deps "${git_base}#subdirectory=maker" --quiet
        pip install --upgrade jm-maker --quiet
        print_success "Maker updated"
    elif [[ "$should_install_maker" == "true" ]]; then
        print_info "Installing maker..."
        pip install "${git_base}#subdirectory=maker" --quiet
        print_success "Maker installed"
    fi

    # Update/install taker (default: install if not present)
    local should_install_taker="${INSTALL_TAKER:-true}"
    if pip show jm-taker &> /dev/null; then
        print_info "Updating taker..."
        pip install --upgrade --force-reinstall --no-deps "${git_base}#subdirectory=taker" --quiet
        pip install --upgrade jm-taker --quiet
        print_success "Taker updated"
    elif [[ "$should_install_taker" == "true" ]]; then
        print_info "Installing taker..."
        pip install "${git_base}#subdirectory=taker" --quiet
        print_success "Taker installed"
    fi

    print_success "Update complete!"
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
        local config_template_url="https://raw.githubusercontent.com/$GITHUB_REPO/${version_tag}/config.toml.template"
        if ! curl -fsSL "$config_template_url" -o "$config_file"; then
            print_warning "Failed to download config template, using fallback..."
            # Fallback: create minimal config if download fails
            cat > "$config_file" << 'EOF'
# JoinMarket-NG Configuration
# See: https://github.com/joinmarket-ng/joinmarket-ng/blob/main/DOCS.md
# For full template: https://github.com/joinmarket-ng/joinmarket-ng/blob/main/config.toml.template

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

# Create shell integration script
create_shell_integration() {
    print_header "Setting Up Shell Integration"

    local shell_script="$DATA_DIR/activate.sh"

    cat > "$shell_script" << EOF
# JoinMarket-NG Shell Integration
# Source this file to activate the environment:
#   source ~/.joinmarket-ng/activate.sh

export JOINMARKET_DATA_DIR="$DATA_DIR"
export PATH="$VENV_DIR/bin:\$PATH"

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

    # Set defaults if components not explicitly specified
    if [[ "$EXPLICIT_COMPONENTS" == "false" ]]; then
        INSTALL_MAKER=${INSTALL_MAKER:-true}
        INSTALL_TAKER=${INSTALL_TAKER:-true}
    else
        INSTALL_MAKER=${INSTALL_MAKER:-false}
        INSTALL_TAKER=${INSTALL_TAKER:-false}
    fi
}

# Main
main() {
    echo ""
    echo -e "${BLUE}JoinMarket-NG Installer${NC}"
    echo ""

    parse_args "$@"

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
        # Update mode - update packages and verify Tor config
        setup_virtualenv
        update_packages
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

main "$@"
