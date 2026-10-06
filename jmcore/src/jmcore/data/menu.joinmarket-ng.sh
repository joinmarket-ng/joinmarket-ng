#!/bin/bash
# shellcheck disable=SC2071  # Zero-padded date strings (YYYY-MM, MM) compare correctly as strings;
                             # numeric comparison would fail on "08", "09" (octal interpretation)
################################################################################
# menu.joinmarket-ng.sh
# TUI Menu for JoinMarket-NG
#
# Works in two environments:
#   - Raspiblitz: Uses sudo bonus script for privileged maker operations
#   - Standalone: Uses direct jm-maker commands, dynamic user detection
#
# Environment is auto-detected at startup.
################################################################################

# ---- Environment detection --------------------------------------------------
# Raspiblitz ships a bonus script for privileged maker control.
# If it exists, we use sudo calls for maker-start/stop/status and password
# storage.  Otherwise we fall back to direct CLI commands.
BONUS_SCRIPT="/home/admin/config.scripts/bonus.joinmarket-ng.sh"
if [ -f "$BONUS_SCRIPT" ]; then
    RASPIBLITZ=1
    # On Raspiblitz the script runs as the joinmarketng user.
    USER_JM="joinmarketng"
    HOME_JM="/home/${USER_JM}"
    VENV_BIN="${HOME_JM}/venv/bin"
else
    RASPIBLITZ=0
    USER_JM=$(whoami)
    HOME_JM="/home/${USER_JM}"
    VENV_BIN="${HOME_JM}/.joinmarket-ng/venv/bin"
fi

# =============================================================================
# ---- Exit Handler (Environment-aware) ---------------------------------------
# =============================================================================
#
# This function handles the exit behavior of the JoinMarket-NG TUI based on
# the detected environment. The behavior differs between Raspiblitz and
# standalone installations to provide the best user experience on each
# platform.
#
# On Raspiblitz:
#   - Instead of exiting directly to the Raspiblitz menu (which would lose
#     the activated virtual environment), we drop to an interactive shell
#     with the JoinMarket venv activated.
#   - This allows users to continue using JoinMarket CLI tools directly
#     without reactivating the environment manually.
#   - From this shell, users can:
#     * Run 'jm-ng' to restart the TUI
#     * Run 'exit' to return to the Raspiblitz menu
#     * Run any JoinMarket CLI commands directly
#   - Shell nesting is prevented via JM_NG_SHELL_ACTIVE environment variable:
#     When the user exits jm-ng while already inside the JM-NG shell
#     (e.g. ESC from a restarted TUI), the variable is already set and we
#     return to the existing JM-NG shell instead of starting another one.
#     Pressing 'B' (Exit to RaspiBlitz Menu) in that situation shows an
#     info message; the user types 'exit' to reach the RaspiBlitz menu.
#
# On Standalone:
#   - We exit normally to the shell, as the user typically launches jm-ng
#     from an already configured environment.
#   - The virtual environment is usually managed by the user directly
#     in standalone setups.
#
# The RASPIBLITZ variable is set during environment detection at startup:
#   - RASPIBLITZ=1 if /home/admin/config.scripts/bonus.joinmarket-ng.sh exists
#   - RASPIBLITZ=0 otherwise
#
# Technical notes:
#   - Uses bash --init-file with a temp file instead of exec --rcfile:
#     * Temp file ensures variables are reliably set in the new shell
#     * No exec (avoids unreliable exit behavior in some environments)
#     * exit 0 after bash ensures jm-ng terminates when the shell exits
#   - JM_NG_SHELL_ACTIVE=1 is exported in the subshell to prevent nesting
#
# Usage:
#   Called from multiple places in the script:
#   - When user selects 'X' (Exit) from the main menu
#   - When user cancels (ESC) from any menu
#   - After the main loop ends (as a safety net)

exit_jm_ng() {
    local exit_mode="${1:-auto}"
    clear
    if [ "${RASPIBLITZ}" -eq 1 ]; then
        # Auto mode: determine based on where we came from
        if [ "${exit_mode}" = "auto" ]; then
            if [ "${JM_NG_SHELL_ACTIVE}" = "1" ]; then
                exit_mode="shell"
            else
                exit_mode="menu"
            fi
        fi

        if [ "${exit_mode}" = "menu" ]; then
            # Exit directly to RaspiBlitz menu
            #
            # EDGE CASE: When user presses 'B' in the TUI but the TUI was launched
            # from the JM-NG shell (not from the RaspiBlitz menu):
            #
            # Ideally we would need: exit TUI → exit shell → return to RaspiBlitz menu
            # This would require complex parent process tracking and double-exit logic.
            # Instead, we just show an info message and stay in the shell.
            # The user can then type 'exit' manually to return to the menu.
            #
            # Check if we're already in JM-NG shell - if so, just show info message
            if [ "${JM_NG_SHELL_ACTIVE}" = "1" ]; then
                echo ""
                echo "========================================"
                echo "  JoinMarket-NG CLI Shell"
                echo "========================================"
                echo "  You are already in the JM-NG shell."
                echo ""
                echo "  jm-ng       → JoinMarket-NG TUI"
                echo "  exit        → Exit to RaspiBlitz menu"
                echo "========================================"
                echo ""
            fi
            exit 0
        fi

        # Show JM shell message
        echo ""
        echo "========================================"
        echo "  JoinMarket-NG CLI Shell"
        echo "========================================"
        echo "  jm-ng       → JoinMarket-NG TUI"
        echo "  exit        → Exit to RaspiBlitz menu"
        echo "========================================"
        echo ""

        # Check if we're already in JM-NG shell (avoid nesting)
        if [ "${JM_NG_SHELL_ACTIVE}" = "1" ]; then
            exit 0
        fi

        # Start a new bash shell with venv activated and marker set.
        # This runs when user explicitly selects 'X' (Exit to Shell) from the menu.
        # ESC is handled by the auto-detection logic above (returns to wherever
        # the user came from - shell or RaspiBlitz menu).
        #
        # Create a temp init file that exports the marker variables and activates venv.
        # Using a real file instead of process substitution <(echo ...) ensures
        # the variables are reliably set in the new shell.
        INIT_FILE=$(mktemp)
        cat > "$INIT_FILE" << 'INITEOF'
export JM_NG_SHELL_ACTIVE=1
source /home/joinmarketng/venv/bin/activate
export PS1="(jmshell) \u@\h:\w\$ "
INITEOF
        bash --init-file "$INIT_FILE"
        rm -f "$INIT_FILE"
        exit 0
    else
        # Standalone: normal exit
        exit 0
    fi
}

# ---- Paths ------------------------------------------------------------------
DATA_DIR="${HOME_JM}/.joinmarket-ng"
CONFIG_FILE="${DATA_DIR}/config.toml"
LOG_DIR="${DATA_DIR}/logs"
MAKER_ENV="${DATA_DIR}/.maker.env"
FINGERPRINT_CACHE="${DATA_DIR}/.current_fingerprint"
# Clear fingerprint cache on exit (security: don't leave wallet identity on disk)
trap 'rm -f "$FINGERPRINT_CACHE"' EXIT

# ---- Activate virtual environment -------------------------------------------
# Prefer the environment paired with this TUI over global entry points or
# appliance wrappers that happen to be earlier in PATH. Activate before TOML
# reads so the config helper is always loaded from the selected installation.
if [ -f "$VENV_BIN/activate" ]; then
    source "$VENV_BIN/activate"
elif ! command -v jm-wallet &>/dev/null; then
    echo "ERROR: jm-wallet not found in PATH and no venv at $VENV_BIN"
    exit 1
fi
# Ensure ~/.local/bin is in PATH (fallback for pip console scripts)
export PATH="${HOME_JM}/.local/bin:$PATH"
TUI_PYTHON=$(command -v python3)

# ---- CLI logging inside the TUI --------------------------------------------
# jm-wallet / jm-* commands use loguru and log to stderr.
# Follow global logging unless the session or [tui] explicitly overrides it.
# Set [tui] log_level = "WARNING" for quiet menu output (issue #459).
# Priority: initial environment > [tui] log_level > [logging] level > INFO.
TUI_SESSION_LOG_LEVEL="${LOGGING__LEVEL:-}"
get_tui_log_level() {
    if [ -n "$TUI_SESSION_LOG_LEVEL" ]; then
        printf '%s' "$TUI_SESSION_LOG_LEVEL"
        return
    fi
    "$TUI_PYTHON" - "$CONFIG_FILE" <<'PYEOF' 2>/dev/null
import sys, pathlib
try:
    import tomllib
except ImportError:
    import tomli as tomllib
path = pathlib.Path(sys.argv[1])
level = "INFO"
if path.exists():
    try:
        data = tomllib.loads(path.read_text())
        level = (data.get("tui", {}).get("log_level")
                 or data.get("logging", {}).get("level") or "INFO").upper()
    except Exception:
        pass
print(level)
PYEOF
}
export LOGGING__LEVEL="$(get_tui_log_level)"

# ---- Defaults for send/coinjoin parameters ----------------------------------
DEFAULT_AMOUNT="0"
DEFAULT_MIXDEPTH="0"
DEFAULT_FEE_RATE=""
DEFAULT_DESTINATION=""
# Counterparty default: read from config.toml [taker] section, fall back to 10
DEFAULT_COUNTERPARTIES=$("$TUI_PYTHON" - "$CONFIG_FILE" <<'PYEOF' 2>/dev/null
import sys, pathlib
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]
path = pathlib.Path(sys.argv[1])
if path.exists():
    try:
        data = tomllib.loads(path.read_text())
        val = data.get("taker", {}).get("counterparty_count")
        if val is not None:
            print(int(val))
    except Exception:
        pass
PYEOF
)
DEFAULT_COUNTERPARTIES="${DEFAULT_COUNTERPARTIES:-10}"

# Ensure log directory exists
mkdir -p "$LOG_DIR"

# =============================================================================
# Notes for Contributors
# =============================================================================
# 1. Terminal Buffer: `clear` before calling whiptail dialogs that follow
#    terminal output WITHOUT a `pause` in between. When `pause` is used,
#    the buffer is cleared automatically (see pause() function).
#    Example: echo "Preparing..." followed by ensure_wallet_password()
#    needs an explicit `clear` before the whiptail call.
#
# 2. Never use whiptail --msgbox after whiptail --passwordbox.
#    Two passwordbox dialogs back-to-back without an external process
#    in between cause the terminal buffer to become stale, and the
#    msgbox will not be rendered. Instead, include the message in
#    the next dialog's text (see prompt_new_wallet_password).

# =============================================================================
# Helpers
# =============================================================================

# Helper: Pause
pause() {
  echo ""
  read -p "Press [Enter] key to continue..." fakeEnterKey
  clear
}

# Keep update output reviewable even when the parent menu clears the terminal.
review_update_output() {
    local update_log="$1"
    echo ""
    echo "Update output saved to: $update_log"
    if [ -t 0 ] && [ -t 1 ] && command -v less >/dev/null 2>&1; then
        # Ignore LESS options that could automatically close a short log.
        LESS= LESSSECURE=1 less -R +G \
            -P 'Update output (Up/Down or PgUp/PgDn to scroll, q to continue)' \
            -- "$update_log" && return
    fi
    read -r -p "Press [Enter] after reviewing the update output to continue..." fakeEnterKey
}

# Helper: Get configured mnemonic file from config.toml
get_mnemonic_file() {
    "$TUI_PYTHON" -m jmcore.config_file get \
        --config "$CONFIG_FILE" --section wallet --key mnemonic_file
}

# Helper: Get stored mnemonic_password from config.toml (empty if unset/commented).
get_stored_mnemonic_password() {
    "$TUI_PYTHON" -m jmcore.config_file get \
        --config "$CONFIG_FILE" --section wallet --key mnemonic_password
}

# Helper: Get stored bip39_passphrase from config.toml (empty if unset/commented).
get_stored_bip39_passphrase() {
    "$TUI_PYTHON" -m jmcore.config_file get \
        --config "$CONFIG_FILE" --section wallet --key bip39_passphrase
}

# Helper: Get the wallet_with_passphrase flag from config.toml ("true"/"false",
# empty if unset). Unset means "false": passphrases are strictly opt-in and no
# passphrase prompt is shown unless the user explicitly enabled the flag.
get_wallet_with_passphrase() {
    JOINMARKET_CONFIG_FILE="$CONFIG_FILE" "$TUI_PYTHON" - <<'PY'
from jmcore.settings import get_settings
print(str(get_settings().wallet.bip39_passphrase_enabled).lower())
PY
}

wallet_identity_summary() {
    "$TUI_PYTHON" - "$1" <<'PY'
import sys
from pathlib import Path
from jmcore.wallet_metadata import selected_identity
try:
    identity = selected_identity(Path(sys.argv[1]))
    print(f"{identity.fingerprint} (BIP39: {identity.bip39})" if identity else "identity unconfirmed")
except (OSError, ValueError):
    print("identity unavailable; register/select explicitly")
PY
}

wallet_requires_passphrase_prompt() {
    JOINMARKET_CONFIG_FILE="$CONFIG_FILE" "$TUI_PYTHON" - "$CURRENT_WALLET" <<'PY'
import sys
from pathlib import Path
from jmcore.cli_common import bip39_prompt_required
from jmcore.settings import get_settings
from jmcore.wallet_metadata import selected_identity
try:
    print(str(bip39_prompt_required(get_settings(), selected_identity(Path(sys.argv[1])))).lower())
except (OSError, ValueError):
    sys.exit(1)
PY
}

# Helper: Read the temporary wallet password from .maker.env (empty if absent).
#
# On Raspiblitz the wallet password (when NOT permanently stored in
# config.toml) is delivered to the maker process via the systemd
# EnvironmentFile (.maker.env -> MNEMONIC_PASSWORD). It is intentionally kept
# out of config.toml so the secret is never left in cleartext on disk after
# the maker stops. The file uses systemd's double-quoted form with C-style
# escapes. Share the non-executing parser with maker startup.
get_maker_env_password() {
    [ -f "$MAKER_ENV" ] || return 1
    "$TUI_PYTHON" -m jmcore.maker_env "$MAKER_ENV" MNEMONIC_PASSWORD
}

# Helper: Read the staged BIP39 passphrase from .maker.env (empty if absent).
get_maker_env_bip39_passphrase() {
    [ -f "$MAKER_ENV" ] || return 1
    "$TUI_PYTHON" -m jmcore.maker_env "$MAKER_ENV" BIP39_PASSPHRASE
}

# Helper: Write the wallet password, optional BIP39 passphrase and expected
# wallet fingerprint to .maker.env for the systemd EnvironmentFile, using
# systemd's double-quoted C-escape format so special characters survive.
# chmod 600. Secrets are NEVER written to config.toml (no cleartext leak);
# .maker.env is removed when the maker stops.
# EXPECTED_FINGERPRINT binds the staged passphrase to the wallet it was staged
# for: ensure_wallet_unlocked_global refuses a staged passphrase whose
# derivation does not match the active wallet (cross-wallet guard). The
# fingerprint is public, not a secret. Empty means "not determinable" and
# fails closed (the staged passphrase is then ignored).
write_maker_env() {
    local password="$1"
    local bip39_passphrase="${2:-}"
    local expected_fingerprint="${3:-}"
    local escaped
    # A non-newline sentinel protects trailing LF from command substitution.
    escaped=$(printf '%s' "$password" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' && printf '.') || return 1
    escaped=${escaped%.}
    local escaped_passphrase
    escaped_passphrase=$(printf '%s' "$bip39_passphrase" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' && printf '.') || return 1
    escaped_passphrase=${escaped_passphrase%.}
    # Preserve the private, checked replacement from the maker-env hardening.
    ( umask 077;
      local temporary
      temporary=$(mktemp "${MAKER_ENV}.XXXXXX") || exit 1
      trap 'rm -f -- "$temporary"' EXIT
      if [ -n "$password" ]; then
          printf 'MNEMONIC_PASSWORD="%s"\n' "$escaped" > "$temporary" || exit 1
      fi
      if [ $# -gt 1 ]; then
          printf 'BIP39_PASSPHRASE="%s"\n' "$escaped_passphrase" >> "$temporary" || exit 1
          printf 'EXPECTED_FINGERPRINT="%s"\n' "$expected_fingerprint" >> "$temporary" || exit 1
      fi
      chmod 600 "$temporary" || exit 1
      mv -fT -- "$temporary" "$MAKER_ENV" || exit 1
    )
}

# Helper: Read the expected wallet fingerprint from .maker.env. The line is
# written by write_maker_env and binds the staged BIP39 passphrase to the
# wallet it was staged for. Returns 1 when the file or the line is absent
# (legacy .maker.env written before the cross-wallet guard).
get_maker_env_expected_fingerprint() {
    [ -f "$MAKER_ENV" ] || return 1
    "$TUI_PYTHON" -m jmcore.maker_env "$MAKER_ENV" EXPECTED_FINGERPRINT
}

# Helper: Remove a value in config.toml.
clear_config_value() {
    local section=$1
    local key=$2
    "$TUI_PYTHON" -m jmcore.config_file remove \
        --config "$CONFIG_FILE" --section "$section" --key "$key"
}

# Helper: Set a string value in config.toml. Secrets travel over stdin so they
# are never present in a process command line or environment variable.
set_config_value() {
    local section=$1
    local key=$2
    local value=$3
    printf '%s' "$value" | "$TUI_PYTHON" -m jmcore.config_file set \
        --config "$CONFIG_FILE" --section "$section" --key "$key"
}

# Helper: Set a boolean value in config.toml (written as a TOML boolean, not a
# quoted string). Value must be exactly "true" or "false".
set_config_bool() {
    local section=$1
    local key=$2
    local value=$3
    printf '%s' "$value" | "$TUI_PYTHON" -m jmcore.config_file set --bool \
        --config "$CONFIG_FILE" --section "$section" --key "$key"
}

# Clear the saved password and BIP39 passphrase before changing the active
# wallet. If saving the new wallet fails, the old wallet remains selected
# without a mismatched secret.
set_active_wallet_config() {
    local wallet_path=$1
    rm -f "$FINGERPRINT_CACHE"  # Clear fingerprint on wallet change
    rm -f "$MAKER_ENV" || return 1
    unset MNEMONIC_PASSWORD BIP39_PASSPHRASE EXPECTED_FINGERPRINT
    clear_config_value wallet mnemonic_password || return 1
    clear_config_value wallet bip39_passphrase || return 1
    set_config_value wallet mnemonic_file "$wallet_path"
}

clear_active_wallet_config() {
    rm -f "$FINGERPRINT_CACHE" "$MAKER_ENV" || return 1
    unset MNEMONIC_PASSWORD BIP39_PASSPHRASE EXPECTED_FINGERPRINT
    clear_config_value wallet mnemonic_password || return 1
    clear_config_value wallet bip39_passphrase || return 1
    clear_config_value wallet mnemonic_file
}

show_config_save_error() {
    whiptail --title " Config Error " \
        --msgbox "Could not finish updating config.toml. The active wallet was not changed." 8 60
}

# Helper: List .mnemonic files in wallets dir
# Uses a bash glob instead of GNU 'find -printf' for portability
# (BusyBox compatibility, see issue #558).
list_wallets() (
    local _f _names=
    # Preserve find's handling of dot-prefixed wallet names without leaking
    # shell options into callers.
    shopt -s nullglob dotglob
    for _f in "$DATA_DIR"/wallets/*.mnemonic; do
        _names="${_names}$(basename "$_f")"$'\n'
    done
    printf '%s' "$_names" | sort
)

# Helper: Prompt for a parameter using whiptail inputbox.
# Usage: prompt_param "Title" "Prompt text" "default_value"
# Returns the value via stdout. Returns exit code 1 if user cancelled.
prompt_param() {
    local title="$1"
    local prompt="$2"
    local default="$3"
    local value
    value=$(whiptail --title " $title " \
      --inputbox "$prompt" \
      16 68 "$default" 3>&1 1>&2 2>&3)
    local rc=$?
    [ $rc -ne 0 ] && return 1
    echo "$value"
    return 0
}

# Helper: Sanitize a numeric string -- strip leading zeros, default to fallback.
# Usage: to_int "value" "fallback"
# Examples: to_int "02" "0" -> "2", to_int "" "0" -> "0", to_int "abc" "0" -> "0"
to_int() {
    local raw="$1"
    local fallback="${2:-0}"
    # Remove leading zeros, then validate as integer
    local stripped
    stripped=$(echo "$raw" | sed 's/^0*//' | sed 's/^$/0/')
    if [[ "$stripped" =~ ^[0-9]+$ ]]; then
        echo "$stripped"
    else
        echo "$fallback"
    fi
}

# Helper: Show a confirmation summary before executing a command.
# Compares chosen values against defaults and marks changed ones with ">>".
# Usage: show_summary "Title" "label1|default1|value1" "label2|default2|value2" ...
# Returns 0 if user confirms, 1 if cancelled.
show_summary() {
    local title="$1"
    shift
    local body=""
    local label default value marker

    for entry in "$@"; do
        IFS='|' read -r label default value <<< "$entry"
        if [ "$value" != "$default" ]; then
            marker=">>"
        else
            marker="  "
        fi
        # Show default in parentheses when there is one
        if [ -n "$default" ]; then
            body="${body}${marker} ${label}: ${value}  (default: ${default})\n"
        else
            body="${body}${marker} ${label}: ${value}\n"
        fi
    done

    body="${body}\n>> = changed from default\n\nProceed?"

    whiptail --title " $title " --yesno "$body" 20 70 3>&1 1>&2 2>&3
    return $?
}

# Helper: Display send/coinjoin status summary with defaults.
# Shows current parameter values inline while the user fills in each field.
# Usage: display_send_status "Optional explanation text"
display_send_status() {
    local explanation="${1:-}"

    # Fee display logic based on SEND_FEE_ENTERED flag
    local fee_display
    if [ -n "$SEND_FEE" ]; then
        fee_display="$SEND_FEE"
    elif [ "$SEND_FEE_ENTERED" = "1" ]; then
        fee_display="auto"
    else
        fee_display="(default: auto)"
    fi

    # With manual UTXO selection the mixdepth is derived from the selection
    local mixdepth_display
    if [ -n "$SEND_SELECT" ]; then
        mixdepth_display="manual UTXO selection"
    else
        mixdepth_display="${SEND_MIXDEPTH:-(default: ${DEFAULT_MIXDEPTH})}"
    fi

    cat <<EOF

From wallet:     $(basename "$CURRENT_WALLET")
Source Mixdepth: ${mixdepth_display}
Amount:          ${SEND_AMOUNT:-(default: ${DEFAULT_AMOUNT})} sats
Counterparties:  ${SEND_CP:-(default: ${DEFAULT_COUNTERPARTIES})} makers
Fee Rate         ${fee_display} sats/vB
Destination:     ${SEND_DEST:-not set}
----------------------------------------------------------------
${explanation}
EOF
}

# Helper: Stop Maker Bot (standalone mode)
# Cleans up process and files when not using the Raspiblitz bonus script.
stop_maker() {
    if pgrep -f "jm-maker" > /dev/null 2>&1; then
        echo "Stopping maker bot..."

        MAKER_PIDS=$(pgrep -f "jm-maker")
        echo "Found maker processes: $MAKER_PIDS"

        # Send graceful shutdown signal
        for PID in $MAKER_PIDS; do
            kill -TERM "$PID" 2>/dev/null
        done

        # Wait for graceful shutdown (up to 5 seconds)
        echo "Waiting for graceful shutdown (up to 5 seconds)..."
        sleep 5

        # Force kill if still running
        if pgrep -f "jm-maker" > /dev/null 2>&1; then
            echo "Processes still running, forcing shutdown..."
            pkill -KILL -f "jm-maker" 2>/dev/null
        fi

        echo "Maker processes stopped."
    else
        echo "No maker process running."
    fi

    # Clean up files
    rm -f "$DATA_DIR/.maker.pid"
    rm -f "$DATA_DIR/.maker.env"
    rm -f "$DATA_DIR/state/maker.nick"
    echo "Done."
}

# Helper: Store wallet password (environment-aware)
store_password() {
    local password="$1"
    if [ "$RASPIBLITZ" = "1" ]; then
        sudo "$BONUS_SCRIPT" store-password "$password"
    else
        set_config_value wallet mnemonic_password "$password"
    fi
}

# Helper: Store the BIP39 passphrase in config.toml.
#
# Unlike store_password there is no Raspiblitz bonus-script command for the
# passphrase; writing config.toml directly works in both environments (the
# TUI already does so for the log level and the delete-password flow).
store_bip39_passphrase() {
    local passphrase="$1"
    set_config_value wallet bip39_passphrase "$passphrase"
}

# Helper: Verify that a password can decrypt a wallet file.
# Returns 0 if the password matches, non-zero otherwise.
# Usage: verify_wallet_password "/path/to/wallet.mnemonic" "password"
verify_wallet_password() {
    local wallet_path="$1"
    local password="$2"
    MNEMONIC_PASSWORD="$password" jm-wallet verify-password \
        -f "$wallet_path" --no-prompt >/dev/null 2>&1
    return $?
}

# Helper: Prompt + validate the wallet password and store it on success.
# Loops up to 3 times on mismatch. User can cancel at any time.
# Usage: prompt_and_store_password "/path/to/wallet.mnemonic"
prompt_and_store_password() {
    local wallet_path="$1"
    local attempts=0
    local max_attempts=3
    local pwd_store

    # Plaintext wallets have nothing to store.
    jm-wallet verify-password -f "$wallet_path" --no-prompt --password "" \
        >/dev/null 2>&1
    if [ $? -eq 2 ]; then
        whiptail --title " Info " --msgbox "This wallet is not encrypted.\nNo password to store." 8 50
        return 1
    fi

    # Security warning first (#453). Make the trade-off explicit.
    if ! whiptail --title " Security Warning " \
        --yesno "Storing the wallet password in config.toml saves it in PLAIN TEXT.\n\nAnyone with read access to:\n  $CONFIG_FILE\ncan decrypt your wallet. This effectively defeats the\nwallet encryption.\n\nOnly store the password if the maker bot needs to start\nunattended and you trust the security of this machine.\n\nContinue and store the password?" \
        18 70 --defaultno 3>&1 1>&2 2>&3; then
        return 1
    fi

    # NOTE: Previous version used whiptail --msgbox after whiptail --passwordbox
    # for the mismatch message. This appeared to work because verify_wallet_password()
    # calls jm-wallet (an external process) which refreshes the terminal buffer
    # as a side effect. However, the msgbox-after-passwordbox pattern is unreliable
    # in general — it breaks when two passwordbox dialogs are called back-to-back
    # without an external process in between (see issue #481).
    # Using inline mismatch text in the passwordbox prompt is robust regardless
    # of external processes.
    local mismatch=""
    while [ $attempts -lt $max_attempts ]; do
        pwd_store=$(whiptail --title " Wallet Password " \
            --passwordbox "${mismatch}Enter the wallet encryption password for:\n$(basename "$wallet_path")" \
            10 60 3>&1 1>&2 2>&3 && printf '.')
        local rc=$?
        pwd_store=${pwd_store%.}
        if [ $rc -ne 0 ]; then
            # User cancelled
            unset pwd_store
            return 1
        fi
        if verify_wallet_password "$wallet_path" "$pwd_store"; then
            if ! store_password "${pwd_store}"; then
                unset pwd_store
                whiptail --title " Config Error " \
                    --msgbox "Could not save the wallet password to config.toml." 8 55
                return 1
            fi
            unset pwd_store
            whiptail --title " Password Stored " \
                --msgbox "Password verified and saved to config.toml." 8 55
            return 0
        fi
        attempts=$((attempts + 1))
        local remaining=$((max_attempts - attempts))
        if [ $remaining -gt 0 ]; then
            mismatch="Wrong password. ${remaining} attempt(s) remaining.\n\n"
        else
            whiptail --title " Password Mismatch " \
                --msgbox "The password does not decrypt the wallet.\n\nToo many attempts. Password was NOT saved." \
                10 60
        fi
    done
    unset pwd_store
    return 1
}

# Helper: Prompt for the BIP39 passphrase, confirm it via the wallet
# fingerprint, and store it in config.toml on success.
#
# A passphrase cannot be verified against the wallet file (any passphrase
# derives a valid wallet), so the wallet fingerprint IS the verification:
# it is computed from the mnemonic + entered passphrase and must be
# confirmed by the user before anything is stored. Loops up to 3 times when
# the fingerprint is rejected (likely a typo). User can cancel at any time.
#
# Usage: prompt_and_store_bip39_passphrase
# Returns 0 when the passphrase was stored, 1 otherwise.
prompt_and_store_bip39_passphrase() {
    local attempts=0
    local max_attempts=3
    local pp_entry fingerprint

    # Fingerprint derivation needs the decrypted mnemonic. Use the wallet
    # password from the environment or config.toml without exporting it
    # session-wide.
    local fp_password="${MNEMONIC_PASSWORD:-}"
    if [ -z "$fp_password" ]; then
        fp_password=$(get_stored_mnemonic_password && printf '.') || return 1
        fp_password=${fp_password%.}
    fi

    # Security warning first (#453). Make the trade-off explicit.
    if ! whiptail --title " Security Warning " \
        --yesno "Storing the BIP39 passphrase in config.toml saves it in PLAIN TEXT.\n\nAnyone with read access to:\n  $CONFIG_FILE\ncan derive your wallet (together with your seed backup).\n\nOnly store the passphrase if the maker bot needs to start\nunattended and you trust the security of this machine.\n\nContinue and store the passphrase?" \
        18 70 --defaultno 3>&1 1>&2 2>&3; then
        return 1
    fi

    local retry=""
    while [ $attempts -lt $max_attempts ]; do
        pp_entry=$(whiptail --title " BIP39 Passphrase " \
            --passwordbox "${retry}Enter the BIP39 passphrase for:\n$(basename "$CURRENT_WALLET")" \
            10 60 3>&1 1>&2 2>&3 && printf '.')
        local rc=$?
        pp_entry=${pp_entry%.}
        if [ $rc -ne 0 ]; then
            # User cancelled
            unset pp_entry
            return 1
        fi
        if [ -z "$pp_entry" ]; then
            # An empty passphrase means "no passphrase" - nothing to store.
            unset pp_entry
            whiptail --title " Info " \
                --msgbox "Empty passphrase - this wallet does not use one.\nNothing to store." \
                8 55
            return 1
        fi

        # Compute the fingerprint for user confirmation. Clear any stale
        # cache first so a failed computation can never confirm against a
        # previous wallet's fingerprint, and fail closed if none could be
        # produced.
        rm -f "$FINGERPRINT_CACHE"
        if ! MNEMONIC_PASSWORD="$fp_password" cache_wallet_fingerprint "$pp_entry" \
            || [ ! -s "$FINGERPRINT_CACHE" ]; then
            unset pp_entry
            whiptail --title " Error " \
                --msgbox "Could not compute the wallet fingerprint for confirmation.\nFor encrypted wallets the password must be stored first.\n\nPassphrase was NOT saved." \
                10 60
            return 1
        fi
        fingerprint=$(cat "$FINGERPRINT_CACHE")

        if whiptail --title " Wallet Fingerprint " \
            --yesno "Wallet fingerprint: ${fingerprint}\n\nIs this the wallet you expect?\nThe passphrase is only stored when you confirm." \
            12 60 3>&1 1>&2 2>&3; then
            if ! store_bip39_passphrase "${pp_entry}"; then
                unset pp_entry
                whiptail --title " Config Error " \
                    --msgbox "Could not save the BIP39 passphrase to config.toml." 8 55
                return 1
            fi
            unset pp_entry
            whiptail --title " Passphrase Stored " \
                --msgbox "BIP39 passphrase saved to config.toml." 8 55
            return 0
        fi
        attempts=$((attempts + 1))
        local remaining=$((max_attempts - attempts))
        if [ $remaining -gt 0 ]; then
            retry="Fingerprint rejected. ${remaining} attempt(s) remaining.\n\n"
        else
            whiptail --title " Not Confirmed " \
                --msgbox "The fingerprint was not confirmed.\n\nToo many attempts. Passphrase was NOT saved." \
                10 60
        fi
    done
    unset pp_entry
    return 1
}

# Helper: Ensure MNEMONIC_PASSWORD is available for jm-wallet calls.
#
# Without this, jm-wallet commands that need the decrypted mnemonic fall
# through to a raw terminal password prompt, breaking the whiptail-based
# TUI flow (issue: "wallet info" drops to a CLI password prompt).
#
# Behaviour:
#   - If the wallet file is plaintext (unencrypted), do nothing.
#   - If MNEMONIC_PASSWORD is already exported in the environment, do nothing.
#   - If config.toml has a non-empty mnemonic_password, export it.
#   - Otherwise prompt the user via whiptail --passwordbox, verify the
#     password against the wallet, and export MNEMONIC_PASSWORD on success.
#
# Returns 0 on success (password available or not needed), 1 if the user
# cancelled or exhausted retry attempts.
#
# Typical usage -- run the jm-wallet call inside a subshell so the exported
# password does not leak beyond the single invocation:
#   (
#       ensure_wallet_password "$CURRENT_WALLET" || exit 1
#       jm-wallet info
#   )
ensure_wallet_password() {
    local wallet_path="$1"
    export MNEMONIC_FILE="$wallet_path"
    local attempts=0
    local max_attempts=3
    local pwd_entry

    if [ -z "$wallet_path" ] || [ ! -f "$wallet_path" ]; then
        return 0
    fi

    # Plaintext wallets have nothing to unlock.
    # verify-password exits 2 when the file is not encrypted.
    jm-wallet verify-password -f "$wallet_path" --no-prompt --password "" \
        >/dev/null 2>&1
    if [ $? -eq 2 ]; then
        return 0
    fi

    # Already set in env (e.g. by a previous call in the same subshell).
    if [ -n "${MNEMONIC_PASSWORD:-}" ]; then
        return 0
    fi

    # Stored in config.toml -- jmcore picks it up automatically, but also
    # export it here so verify loops in the same shell short-circuit.
    local stored
    stored=$(get_stored_mnemonic_password && printf '.') || return 1
    stored=${stored%.}
    if [ -n "$stored" ]; then
        export MNEMONIC_PASSWORD="$stored"
        return 0
    fi

    # Temporary password from a running maker (Raspiblitz delivers it via
    # .maker.env for the systemd EnvironmentFile, never via config.toml).
    # Reuse it so wallet operations do not prompt while the maker is running.
    # Verify it first in case the file is stale or belongs to another wallet.
    local maker_env_pwd
    maker_env_pwd=$(get_maker_env_password && printf '.')
    local staging_status=$?
    maker_env_pwd=${maker_env_pwd%.}
    [ "$staging_status" -le 1 ] || return 1
    if [ -n "$maker_env_pwd" ] && verify_wallet_password "$wallet_path" "$maker_env_pwd"; then
        export MNEMONIC_PASSWORD="$maker_env_pwd"
        unset maker_env_pwd
        return 0
    fi
    unset maker_env_pwd

    # NOTE: Previous version used whiptail --msgbox after whiptail --passwordbox
    # for the mismatch message. This appeared to work because verify_wallet_password()
    # calls jm-wallet (an external process) which refreshes the terminal buffer
    # as a side effect. However, the msgbox-after-passwordbox pattern is unreliable
    # in general — it breaks when two passwordbox dialogs are called back-to-back
    # without an external process in between (see issue #481).
    # Using inline mismatch text in the passwordbox prompt is robust regardless
    # of external processes.
    local mismatch=""
    while [ $attempts -lt $max_attempts ]; do
        pwd_entry=$(whiptail --title " Wallet Password " \
            --passwordbox "${mismatch}Enter the wallet encryption password for:\n$(basename "$wallet_path")" \
            10 60 3>&1 1>&2 2>&3 && printf '.')
        local rc=$?
        pwd_entry=${pwd_entry%.}
        if [ $rc -ne 0 ]; then
            unset pwd_entry
            return 1
        fi
        if verify_wallet_password "$wallet_path" "$pwd_entry"; then
            export MNEMONIC_PASSWORD="$pwd_entry"
            unset pwd_entry
            return 0
        fi
        attempts=$((attempts + 1))
        local remaining=$((max_attempts - attempts))
        if [ $remaining -gt 0 ]; then
            mismatch="Wrong password. ${remaining} attempt(s) remaining.\n\n"
        else
            whiptail --title " Password Mismatch " \
                --msgbox "Too many attempts. Returning to menu." \
                9 50
        fi
    done
    unset pwd_entry
    return 1
}

# Helper: Compute wallet fingerprint and cache it for menu display.
# Usage: cache_wallet_fingerprint [passphrase]
# Returns 0 on success, 1 on failure. Writes to FINGERPRINT_CACHE.
cache_wallet_fingerprint() {
    local passphrase="${1:-}"
    local mnemonic_file fingerprint

    mnemonic_file="${CURRENT_WALLET:-}"
    [ -n "$mnemonic_file" ] || mnemonic_file=$(get_mnemonic_file) || return 1
    [ -f "$mnemonic_file" ] || return 1

    fingerprint=$(
        _MNEMONIC_FILE="$mnemonic_file" \
        JOINMARKET_CONFIG_FILE="$CONFIG_FILE" \
        BIP39_PASSPHRASE="$passphrase" \
        _MNEMONIC_PASSWORD="${MNEMONIC_PASSWORD:-}" \
        "$TUI_PYTHON" - <<'PY'
import os
from pathlib import Path
from jmcore.cli_common import resolve_mnemonic
from jmcore.settings import get_settings
from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

mnemonic_file = Path(os.environ["_MNEMONIC_FILE"])
passphrase = os.environ.get("BIP39_PASSPHRASE", "")
password = os.environ.get("_MNEMONIC_PASSWORD") or None

resolved = resolve_mnemonic(
    get_settings(), mnemonic_file=mnemonic_file,
    password=password, bip39_passphrase=passphrase,
)
if resolved is None:
    raise ValueError("No wallet credential source")
print(get_mnemonic_fingerprint(resolved.mnemonic, resolved.bip39_passphrase))
PY
    ) || return 1

    [ -n "$fingerprint" ] || return 1

    # Create with restrictive permissions atomically: the fingerprint is not a
    # secret but privacy-relevant, so the file must not be world-readable even
    # briefly between creation and chmod.
    ( umask 077; printf '%s\n' "$fingerprint" > "$FINGERPRINT_CACHE" )
    chmod 600 "$FINGERPRINT_CACHE"
    return 0
}

# Helper: Ensure the BIP39 passphrase for the current wallet is available in
# the current (sub)shell as BIP39_PASSPHRASE. Each menu action runs in its own
# subshell, so the cache lives for one action: all CLI invocations within that
# action reuse it without prompting again.
#
# Resolution order:
#   1. Already exported BIP39_PASSPHRASE in this shell.
#   2. Staged BIP39_PASSPHRASE from .maker.env (Raspiblitz/systemd).
#   3. wallet.bip39_passphrase configured in config.toml.
#   4. Selected identity requirement, then the onboarding preference for
#      unregistered identities. Registered requirements override the flag.
#   5. Prompt the user, then compute and display the wallet fingerprint so the
#      user can confirm the correct wallet is being derived.
#
# Side effects:
#   - Sets BIP39_PASSPHRASE environment variable
#   - Creates/updates FINGERPRINT_CACHE for menu display
#
# Returns 0 on success, 1 if the user cancels or confirms "No".
# Always run inside a subshell so the export does not leak to the parent TUI.
ensure_wallet_unlocked_global() {
    local passphrase=""
    local restage_prompt=false

    # Already cached in this subshell - ensure fingerprint exists then return.
    if [ -n "${BIP39_PASSPHRASE+set}" ]; then
        cache_wallet_fingerprint "$BIP39_PASSPHRASE"
        return $?
    fi

    local configured_passphrase
    configured_passphrase=$(get_stored_bip39_passphrase && printf '.') || return 1
    configured_passphrase=${configured_passphrase%.}

    # Older TUI versions stripped trailing LF from config credentials and bound
    # staging to that prefix wallet. Restoring LF must not silently replace it.
    if [ "${1:-}" = "restage" ] && [ -n "$configured_passphrase" ] && [ -f "$MAKER_ENV" ]; then
        local prefix="$configured_passphrase" staged_value staged_binding
        while [[ "$prefix" == *$'\n' ]]; do prefix=${prefix%$'\n'}; done
        if [ "$prefix" != "$configured_passphrase" ] && staged_value=$(get_maker_env_bip39_passphrase && printf '.'); then
            staged_value=${staged_value%.}
            if [ "$staged_value" = "$prefix" ] && staged_binding=$(get_maker_env_expected_fingerprint) &&
                cache_wallet_fingerprint "$staged_value" && [ "$(cat "$FINGERPRINT_CACHE")" = "$staged_binding" ]; then
                cache_wallet_fingerprint "$configured_passphrase" || return 1
                if ! whiptail --title " Restore Exact Passphrase " \
                    --yesno "The configured passphrase has trailing newlines omitted by older TUI versions. Restoring them selects a different wallet.\n\nStaged fingerprint: ${staged_binding}\nConfigured fingerprint: $(cat "$FINGERPRINT_CACHE")\n\nReplace staging with the configured wallet? Confirm only after checking your wallet records." \
                    16 76 --defaultno 3>&1 1>&2 2>&3; then
                    rm -f "$FINGERPRINT_CACHE"
                    return 1
                fi
            fi
        fi
    fi

    # Raspiblitz/systemd staging: passphrase is pre-configured for headless
    # operation. This staged path intentionally runs BEFORE the
    # wallet_with_passphrase flag check: explicit headless staging takes
    # precedence over the opt-in flag.
    #
    # Cross-wallet guard: a passphrase cannot be verified against the wallet
    # file (every passphrase derives a valid wallet), so the staged passphrase
    # is bound to its wallet via EXPECTED_FINGERPRINT. Without the guard a
    # passphrase staged for wallet A would silently derive the wrong wallet
    # after the user switches the active wallet to B. Reject missing/mismatched
    # binding. Restaging reuses a matching binding; otherwise it requires an
    # explicit credential, never inferring an empty passphrase from missing metadata.
    if [ -f "$MAKER_ENV" ] && { [ "${1:-}" != "restage" ] || [ -z "$configured_passphrase" ]; }; then
        if passphrase=$(get_maker_env_bip39_passphrase && printf '.'); then
            passphrase=${passphrase%.}
            local expected_fingerprint actual_fingerprint=""
            if ! expected_fingerprint=$(get_maker_env_expected_fingerprint); then
                if [ "${1:-}" != "restage" ]; then
                    whiptail --title " Restage Maker Credentials " --msgbox "Legacy BIP39 staging has no wallet binding. Restage credentials for the intended wallet before starting the maker." 10 70
                    return 1
                fi
            fi
            if [ -n "$expected_fingerprint" ] && cache_wallet_fingerprint "$passphrase"; then
                actual_fingerprint=$(cat "$FINGERPRINT_CACHE" 2>/dev/null)
            fi
            if [ -n "$expected_fingerprint" ] && [ "$actual_fingerprint" = "$expected_fingerprint" ]; then
                export BIP39_PASSPHRASE="$passphrase"
                export EXPECTED_FINGERPRINT="$expected_fingerprint"
                return 0
            fi
            # Mismatch or unverifiable: the staged passphrase belongs to a
            # different wallet (or its fingerprint was never recorded). Discard
            # it -- never derive silently with the wrong passphrase.
            rm -f "$FINGERPRINT_CACHE"
            if [ "${1:-}" != "restage" ]; then
                whiptail --title " Staged Passphrase Mismatch " \
                    --msgbox "The BIP39 passphrase staged for the maker service belongs to a different wallet (fingerprint mismatch).\n\nIt was NOT applied. Restage maker credentials for the intended wallet." \
                    14 72 3>&1 1>&2 2>&3 || true
                return 1
            fi
            restage_prompt=true
        else
            # Invalid staging is not evidence that no BIP39 credential exists.
            if [ $? -ne 1 ]; then
                [ "${1:-}" = "restage" ] || return 1
                restage_prompt=true
            fi
        fi
        # Key absent (legacy .maker.env written before passphrase support):
        # fall through to config / interactive prompt instead of pinning "".
    fi

    # Permanent config setting: passphrase stored in config.toml.
    passphrase=$configured_passphrase
    if [ -n "$passphrase" ]; then
        export BIP39_PASSPHRASE="$passphrase"
        cache_wallet_fingerprint "$passphrase"
        return $?
    fi

    # Registered identities determine the requirement. The onboarding flag
    # controls prompting only when there is no confirmed requirement.
    local requires_prompt
    requires_prompt=$(wallet_requires_passphrase_prompt) || return 1
    [ "$restage_prompt" = "true" ] && requires_prompt=true
    if [ "$requires_prompt" != "true" ]; then
        export BIP39_PASSPHRASE=""
        cache_wallet_fingerprint ""
        return $?
    fi

    # Interactive prompt: user enters passphrase, confirm via fingerprint.
    passphrase=$(whiptail --title " BIP39 Passphrase " \
        --passwordbox "Enter the optional BIP39 passphrase for this wallet.\n\nLeave empty if the wallet was created without a passphrase." \
        10 60 3>&1 1>&2 2>&3 && printf '.') || return 1
    passphrase=${passphrase%.}

    # Compute the fingerprint for user confirmation. Clear any stale cache
    # first so a failed computation can never confirm against a previous
    # wallet's fingerprint, and fail closed if none could be produced.
    local fingerprint
    rm -f "$FINGERPRINT_CACHE"
    if ! cache_wallet_fingerprint "$passphrase" || [ ! -s "$FINGERPRINT_CACHE" ]; then
        whiptail --title " Error " \
            --msgbox "Could not compute the wallet fingerprint for confirmation. Aborting." \
            8 60
        return 1
    fi
    fingerprint=$(cat "$FINGERPRINT_CACHE")

    if ! whiptail --title " Wallet Fingerprint " \
        --yesno "Wallet fingerprint: ${fingerprint}\n\nContinue with this wallet?" \
        10 60 3>&1 1>&2 2>&3; then
        rm -f "$FINGERPRINT_CACHE"  # Clean up on rejection
        return 1
    fi

    export BIP39_PASSPHRASE="$passphrase"
    return 0
}

# Helper: Prompt the user for a *new* wallet encryption password (with
# confirmation). Prints the resulting password to stdout -- empty string
# means the user explicitly opted for no encryption. Returns non-zero if
# the user cancelled the dialog (the caller should abort the flow).
#
# This keeps the wallet-create/import flow entirely inside whiptail so the
# user never drops back to the bare CLI prompt (which would break the
# menu UX) and so the captured password can be reused by downstream steps
# such as post_wallet_create (issue #462).
prompt_new_wallet_password() {
    local pwd1 pwd2
    local mismatch=""
    while true; do
        pwd1=$(whiptail --title " Wallet Password " \
            --passwordbox "Enter a password to encrypt the wallet file.\n\nLeave empty for an UNENCRYPTED wallet (not recommended)." \
            12 64 3>&1 1>&2 2>&3) || return 1
        if [ -z "$pwd1" ]; then
            if whiptail --title " No Encryption " \
                --yesno "Create the wallet WITHOUT encryption?\n\nAnyone with read access to the file can spend your coins." \
                10 64 --defaultno 3>&1 1>&2 2>&3; then
                echo ""
                return 0
            fi
            continue
        fi
        # Inner loop - only re-ask confirmation on mismatch
        mismatch=""
        while true; do
            pwd2=$(whiptail --title " Wallet Password " \
                --passwordbox "${mismatch}Re-enter the password to confirm." \
                9 64 3>&1 1>&2 2>&3) || break  # ESC goes back to pwd1
            if [ "$pwd1" = "$pwd2" ]; then
                printf '%s' "$pwd1"
                unset pwd1 pwd2
                return 0
            fi
            mismatch="Passwords did not match.\n\n"
        done
    done
}

# Helper: Post-wallet-create prompts (set active wallet + store password)
# Called after a successful wallet generate or import.
#
# Ensures mnemonic_file and mnemonic_password in config.toml stay consistent
# (issue #455):
#   - If the new wallet becomes the active one, the previously stored
#     password is cleared before optionally asking to store a new one. This
#     prevents the old password from sticking around mismatched.
#   - If the user declines to set the wallet as active, we do not offer to
#     store its password (it would mismatch the active wallet in config).
#   - The store-password prompt (issue #452) now validates the entered
#     password against the wallet file before writing it to config.toml.
#
# An optional second argument supplies the password that was just used to
# encrypt the wallet (issue #462). When provided, the "store password in
# config.toml" flow uses it directly instead of asking the user again.
#
# Usage: post_wallet_create "/path/to/wallet.mnemonic" [known_password]
post_wallet_create() {
    local wallet_path="$1"
    local known_password="${2:-}"
    local set_active=0

    # Ask to set as active wallet (default: Yes)
    if whiptail --title " Active Wallet " \
        --yesno "Set this wallet as the active wallet in config?\n\n$(basename "$wallet_path")" \
        10 60 3>&1 1>&2 2>&3; then
        if ! set_active_wallet_config "$wallet_path"; then
            show_config_save_error
            return 1
        fi
        set_active=1
        whiptail --title " Wallet Updated " --msgbox "Active wallet updated in config.toml." 8 55
    fi

    # Only offer to store the password when the new wallet is now the active
    # wallet in config. Storing a password for a non-active wallet would
    # guarantee a mismatch (issue #455).
    if [ $set_active -ne 1 ]; then
        return 0
    fi

    # Unencrypted wallets have no password to store.
    jm-wallet verify-password -f "$wallet_path" --no-prompt --password "" \
        >/dev/null 2>&1
    if [ $? -eq 2 ]; then
        return 0
    fi

    # Ask whether to store the encryption password
    if whiptail --title " Store Password " \
        --yesno "Store the wallet password in config.toml?\n\nThis lets all commands (including the maker) work without\nprompting. If you choose No, the maker will ask each time." \
        12 64 --defaultno 3>&1 1>&2 2>&3; then
        # Show the same security warning the interactive path uses so the
        # user explicitly opts into plain-text storage (#453).
        if whiptail --title " Security Warning " \
            --yesno "Storing the wallet password in config.toml saves it in PLAIN TEXT.\n\nAnyone with read access to:\n  $CONFIG_FILE\ncan decrypt your wallet. This effectively defeats the\nwallet encryption.\n\nOnly store the password if the maker bot needs to start\nunattended and you trust the security of this machine.\n\nContinue and store the password?" \
            18 70 --defaultno 3>&1 1>&2 2>&3; then
            if [ -n "$known_password" ]; then
                # Password came from the just-completed wallet creation, so
                # it trivially matches the wallet -- no need to re-prompt or
                # re-verify (issue #462).
                if store_password "$known_password"; then
                    whiptail --title " Password Stored " \
                        --msgbox "Password saved to config.toml." 8 55
                else
                    whiptail --title " Config Error " \
                        --msgbox "Could not save the wallet password to config.toml." 8 55
                fi
            else
                if ! prompt_and_store_password "$wallet_path"; then
                    whiptail --title " Password " --msgbox "Password not stored." 8 50
                fi
            fi
        else
            whiptail --title " Password " --msgbox "Password not stored." 8 50
        fi
    fi
}

# Helper: Start maker (environment-aware)
maker_start() {
    if [ "$RASPIBLITZ" = "1" ]; then
        sudo "$BONUS_SCRIPT" maker-start
    else
        jm-maker start
    fi
}

# Helper: Stop maker (environment-aware)
maker_stop() {
    if [ "$RASPIBLITZ" = "1" ]; then
        sudo "$BONUS_SCRIPT" maker-stop
    else
        stop_maker
    fi
}

# Helper: Show maker status (environment-aware)
maker_status() {
    if [ "$RASPIBLITZ" = "1" ]; then
        sudo "$BONUS_SCRIPT" maker-status
    else
        echo "Maker Bot: ($MAKER_STATUS)"
    fi
}

# Helper (Raspiblitz): make the wallet password available to the maker start
# without prompting twice and without writing the password to config.toml.
#
# The Raspiblitz maker runs as a systemd service that cannot prompt, so it
# reads the password from the .maker.env EnvironmentFile (MNEMONIC_PASSWORD).
# We prompt ONCE here via whiptail and stage the password into .maker.env so
# the bonus 'maker-start' does not prompt again (issue: maker asks 2x).
#
# Behaviour:
#   - Unencrypted wallet, or password permanently stored in config.toml: no
#     password to stage, but the BIP39 passphrase (if any) is still staged so
#     the headless maker derives the same wallet as the TUI.
#   - Otherwise: prompt once (reusing an existing .maker.env if a maker is
#     already running) and write .maker.env (chmod 600). Returns 1 if the
#     user cancels.
#
# IMPORTANT: call inside a subshell so the MNEMONIC_PASSWORD exported by
# ensure_wallet_password does not leak into the rest of the TUI session:
#   ( stage_maker_password "$CURRENT_WALLET" ) || ...
stage_maker_password() {
    local wallet_path="$1"
    export MNEMONIC_FILE="$wallet_path"
    local need_password=1

    # Unencrypted wallet -- verify-password exits 2 when not encrypted.
    jm-wallet verify-password -f "$wallet_path" --no-prompt --password "" >/dev/null 2>&1
    if [ $? -eq 2 ]; then
        need_password=0
    elif [ "$(get_stored_mnemonic_password; printf '.')" != '.' ]; then
        # Permanently stored in config.toml -- read directly downstream.
        need_password=0
    fi

    local password_to_stage=""
    if [ $need_password -eq 1 ]; then
        # Prompt once (ensure_wallet_password also reuses a running maker's
        # .maker.env if present).
        if ! ensure_wallet_password "$wallet_path"; then
            return 1
        fi
        password_to_stage="${MNEMONIC_PASSWORD}"
    fi

    # Stage the BIP39 passphrase as well, even when no password needs staging:
    # otherwise the headless maker silently derives the wrong (passphrase-less)
    # wallet while the TUI operates on the passphrase-derived one.
    if ! ensure_wallet_unlocked_global restage; then
        return 1
    fi
    # Bind the staged passphrase to THIS wallet: record the fingerprint of the
    # wallet+passphrase derivation so ensure_wallet_unlocked_global can refuse
    # the staged passphrase after the active wallet is switched (cross-wallet
    # guard). Empty when the fingerprint cannot be computed -- fails closed.
    local expected_fingerprint=""
    cache_wallet_fingerprint "${BIP39_PASSPHRASE:-}" || return 1
    expected_fingerprint=$(cat "$FINGERPRINT_CACHE") || return 1
    [[ "$expected_fingerprint" =~ ^[0-9a-f]{8}$ ]] || return 1
    MAKER_ENV="${2:-$MAKER_ENV}" write_maker_env "${password_to_stage}" "${BIP39_PASSPHRASE:-}" "$expected_fingerprint"
}

# ---------------------------------------------------------------------------
# Helper: Stale wallet check for all loops that display WALLET_INFO.
# Refreshes CURRENT_WALLET from config, checks if the file still exists,
# and updates WALLET_INFO. Called at the top of every menu loop so the
# display stays accurate even if the wallet file is deleted externally.
# ---------------------------------------------------------------------------
check_stale_wallet() {
    CURRENT_WALLET=$(get_mnemonic_file)
    if [ -n "$CURRENT_WALLET" ] && [ ! -f "$CURRENT_WALLET" ]; then
        whiptail --title " Stale Wallet Config " \
            --msgbox "The configured wallet file no longer exists:\n\n$CURRENT_WALLET\n\nThis usually means the .mnemonic file was deleted\noutside the TUI. Clearing mnemonic_file and\nmnemonic_password from config.toml.\n\nUse 'Wallet Management' to select or create a\nnew active wallet." \
            16 70 3>&1 1>&2 2>&3 || true
        if ! clear_active_wallet_config; then
            show_config_save_error
            return 1
        fi
        CURRENT_WALLET=""
        rm -f "$FINGERPRINT_CACHE"
    fi
    if [ -n "$CURRENT_WALLET" ]; then
        local wallet_name
        wallet_name=$(basename "$CURRENT_WALLET")
        WALLET_INFO="Active Wallet: ${wallet_name} | $(wallet_identity_summary "$CURRENT_WALLET")"
    else
        WALLET_INFO="Active Wallet: (none configured)"
        rm -f "$FINGERPRINT_CACHE"
    fi
}

# ---------------------------------------------------------------------------
# Helper: Ensure an active wallet is configured and available.
#
# WARNING: Do not call from subshells - modifies global CURRENT_WALLET
# and WALLET_INFO.
#
# - Refreshes CURRENT_WALLET from config
# - Silently cleans up stale config (file no longer exists)
# - If no wallet configured:
#   - No wallet files exist → unified error message, return 1
#   - 1 wallet exists → auto-set as active, offer password storage
#   - Multiple wallets → picker, offer password storage
# - Password storage is ONLY offered when wallet just changed
# - Updates WALLET_INFO whenever CURRENT_WALLET changes
# Returns 0 if a wallet is ready, 1 to abort.
# ---------------------------------------------------------------------------
ensure_active_wallet() {
    # Refresh from config
    CURRENT_WALLET=$(get_mnemonic_file)

    # Silently clean up stale config (warning shown by check_stale_wallet
    # in the loop; this handles the rare case where the file was deleted
    # between the loop's check and this call).
    if [ -n "$CURRENT_WALLET" ] && [ ! -f "$CURRENT_WALLET" ]; then
        if ! clear_active_wallet_config; then
            show_config_save_error
            return 1
        fi
        CURRENT_WALLET=""
    fi

    # Already have a valid wallet - no password offer
    if [ -n "$CURRENT_WALLET" ] && [ -f "$CURRENT_WALLET" ]; then
        local wallet_name
        wallet_name=$(basename "$CURRENT_WALLET")
        WALLET_INFO="Active Wallet: ${wallet_name} | $(wallet_identity_summary "$CURRENT_WALLET")"
        return 0
    fi

    # No wallet configured - try to find one
    local wallet_just_changed="no"
    local wallets
    wallets=$(list_wallets)

    if [ -z "$wallets" ]; then
        whiptail --title " Error " --msgbox \
            "No wallet configured.\n\nCreate or import a wallet first\n(W → NEW or IMP)." 9 50
        return 1
    fi

    local wallet_count
    wallet_count=$(printf '%s\n' "$wallets" | sed '/^$/d' | wc -l)

    if [ "$wallet_count" -eq 1 ]; then
        # Single wallet: auto-set as active
        clear
        echo "Please wait..."

        local only_wallet
        only_wallet=$(printf '%s\n' "$wallets" | sed -n '1p')
        local only_path="$DATA_DIR/wallets/$only_wallet"
        if ! set_active_wallet_config "$only_path"; then
            show_config_save_error
            return 1
        fi
        CURRENT_WALLET="$only_path"
        wallet_just_changed="yes"
    else
        # Multiple wallets: picker
        local menu_items=()
        while IFS= read -r wf; do
            [ -z "$wf" ] && continue
            menu_items+=("$wf" "$wf")
        done <<< "$wallets"

        local current_display
        current_display=$(basename "${CURRENT_WALLET:-}" 2>/dev/null)
        [ -z "$current_display" ] && current_display="(none)"

        local selected
        selected=$(whiptail --title " Select Active Wallet " --notags \
            --menu "Current active: ${current_display}\n\nChoose a wallet:" \
            18 64 6 \
            "${menu_items[@]}" 3>&1 1>&2 2>&3) || return 1

        clear
        echo "Please wait..."

        local selected_path="$DATA_DIR/wallets/$selected"
        if [ ! -f "$selected_path" ]; then
            whiptail --title " Error " --msgbox "File not found: $selected_path" 8 55
            return 1
        fi

        # Update config
        if [ "$CURRENT_WALLET" != "$selected_path" ]; then
            if ! set_active_wallet_config "$selected_path"; then
                show_config_save_error
                return 1
            fi
            CURRENT_WALLET="$selected_path"
            wallet_just_changed="yes"
        fi
    fi

    # Update WALLET_INFO
    WALLET_INFO="Active Wallet: $(basename "$CURRENT_WALLET") | $(wallet_identity_summary "$CURRENT_WALLET")"

    # Offer password storage ONLY when wallet just changed
    if [ "$wallet_just_changed" = "yes" ]; then
        jm-wallet verify-password -f "$CURRENT_WALLET" --no-prompt --password "" >/dev/null 2>&1
        if [ $? -ne 2 ]; then
            local stored_pwd
            stored_pwd=$(get_stored_mnemonic_password && printf '.')
            stored_pwd=${stored_pwd%.}
            if [ -z "$stored_pwd" ]; then
                if whiptail --title " Store Password " \
                    --yesno "Active wallet: $(basename "$CURRENT_WALLET")\n\nStore this wallet's password in config.toml?\nThis lets the maker start without prompting.\nChoose No to be asked for the password on each use." \
                    12 64 --defaultno 3>&1 1>&2 2>&3; then
                    clear
                    if ! prompt_and_store_password "$CURRENT_WALLET"; then
                        echo "Password not stored."
                    fi
                fi
            fi
        fi

        # Offer passphrase storage as well when the wallet uses a passphrase
        # (wallet_with_passphrase flag) and none is stored yet.
        if [ "$(get_wallet_with_passphrase)" = "true" ]; then
            local stored_pp
            stored_pp=$(get_stored_bip39_passphrase && printf '.')
            stored_pp=${stored_pp%.}
            if [ -z "$stored_pp" ]; then
                if whiptail --title " Store Passphrase " \
                    --yesno "Active wallet: $(basename "$CURRENT_WALLET")\n\nStore this wallet's BIP39 passphrase in config.toml?\nThis lets the maker start without prompting.\nChoose No to be asked for the passphrase on each use." \
                    12 64 --defaultno 3>&1 1>&2 2>&3; then
                    clear
                    if ! prompt_and_store_bip39_passphrase; then
                        echo "Passphrase not stored."
                    fi
                fi
            fi
        fi
    fi

    return 0
}

# ---------------------------------------------------------------------------
# Helper: Offer password storage for Maker with context-specific explanation.
#
# The Maker needs a stored password for automatic restart after crashes.
# This helper checks if the active wallet is encrypted and has no stored
# password, then offers to store it with a Maker-specific explanation.
# Called after ensure_active_wallet() in Maker START/RESTART.
# ---------------------------------------------------------------------------
offer_maker_password_storage() {
    # Check if wallet is encrypted
    jm-wallet verify-password -f "$CURRENT_WALLET" --no-prompt --password "" >/dev/null 2>&1
    if [ $? -eq 2 ]; then
        return 0  # Unencrypted wallet, no password needed
    fi

    # Check if password already stored
    local stored_pwd
    stored_pwd=$(get_stored_mnemonic_password && printf '.')
    stored_pwd=${stored_pwd%.}
    if [ -n "$stored_pwd" ]; then
        return 0  # Already stored
    fi

    # Offer with Maker-specific explanation
    if ! whiptail --title " Store Password for Maker " \
        --yesno "For automatic restart after a crash, the Maker needs\nthe wallet password stored in config.toml.\n\nWithout stored password: If the Maker crashes, you must\nmanually restart it and re-enter the password.\n\nStore password now?" \
        14 64 --defaultno 3>&1 1>&2 2>&3; then
        return 0  # User declined
    fi

    clear
    if ! prompt_and_store_password "$CURRENT_WALLET"; then
        whiptail --title " Password " --msgbox "Password not stored." 8 50
    fi

    return 0
}

# ---------------------------------------------------------------------------
# Helper: Offer BIP39 passphrase storage for Maker with context-specific
# explanation.
#
# When the wallet uses a passphrase, the Maker needs it stored for automatic
# restart after crashes: the passphrase staged in .maker.env is only written
# when the maker is started from this TUI, so a crash/restart or a Raspiblitz
# boot autostart derives the wrong (empty-passphrase) wallet unless the
# passphrase is in config.toml. Called after offer_maker_password_storage()
# in Maker START/RESTART.
# ---------------------------------------------------------------------------
offer_maker_passphrase_storage() {
    # Passphrases are strictly opt-in: without the wallet_with_passphrase flag
    # the wallet has no passphrase and there is nothing to store.
    if [ "$(get_wallet_with_passphrase)" != "true" ]; then
        return 0
    fi

    # Check if passphrase already stored
    local stored_pp
    stored_pp=$(get_stored_bip39_passphrase && printf '.')
    stored_pp=${stored_pp%.}
    if [ -n "$stored_pp" ]; then
        return 0  # Already stored
    fi

    if ! whiptail --title " Store Passphrase for Maker " \
        --yesno "This wallet uses a BIP39 passphrase.\n\nFor automatic restart after a crash, the Maker needs\nthe passphrase stored in config.toml.\n\nWithout stored passphrase: a restarted Maker derives a\nDIFFERENT wallet and will not offer your coins.\n\nStore passphrase now?" \
        15 64 --defaultno 3>&1 1>&2 2>&3; then
        return 0  # User declined
    fi

    clear
    if ! prompt_and_store_bip39_passphrase; then
        whiptail --title " Passphrase " --msgbox "Passphrase not stored." 8 50
    fi

    return 0
}

# =============================================================================
# Main Loop
# =============================================================================

clear
while true; do

  # Get Maker Service Status
  if pgrep -f "jm-maker" > /dev/null 2>&1; then
    MAKER_STATUS="RUNNING"
  else
    MAKER_STATUS="STOPPED"
  fi

  # Check if a wallet is configured
check_stale_wallet

if [ "${RASPIBLITZ}" -eq 1 ]; then
    CHOICE=$(whiptail --title " JoinMarket-NG Menu" \
        --menu "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS" \
        21 64 8 \
        "S" "Send Bitcoin" \
        "W" "Wallet Management" \
        "M" "Maker Bot Control" \
        "C" "Config Center" \
        "U" "Update JoinMarket-NG" \
        "I" "Info / Documentation" \
        "B" "Exit to RaspiBlitz Menu" \
        "X" "Exit to JoinMarket-NG CLI Shell" 3>&1 1>&2 2>&3)
  else
    CHOICE=$(whiptail --title " JoinMarket-NG Menu" \
        --menu "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS" \
        18 64 7 \
        "S" "Send Bitcoin" \
        "W" "Wallet Management" \
        "M" "Maker Bot Control" \
        "C" "Config Center" \
        "U" "Update JoinMarket-NG" \
        "I" "Info / Documentation" \
        "X" "Exit to JoinMarket-NG CLI Shell" 3>&1 1>&2 2>&3)
  fi

  exitstatus=$?
  if [ $exitstatus != 0 ]; then
    exit_jm_ng
  fi

  case $CHOICE in
    # ------------------------------------------------------------------
    # SEND BITCOIN (unified: normal tx when counterparties=0, coinjoin otherwise)
    # ------------------------------------------------------------------
    S)
      if ! ensure_active_wallet; then
          continue
      fi

      # Reset all send parameters at the start
      SEND_MIXDEPTH=""
      SEND_AMOUNT=""
      SEND_CP=""
      SEND_FEE=""
      SEND_FEE_ENTERED=""
      SEND_DEST=""
      SEND_SELECT=""

      # 1. Coin control: automatic selection from one mixdepth, or manual
      #    UTXO selection (full-wallet selector TUI; the first toggled UTXO
      #    pins the source mixdepth)
      whiptail --title " Coin Control " --defaultno --yesno \
"Select the UTXOs to spend manually?

Yes: a full-wallet UTXO selector opens after these prompts.
     The first UTXO you toggle pins the source mixdepth.
No:  automatic coin selection from one mixdepth." 12 64
      case $? in
        0) SEND_SELECT="1" ;;
        1) SEND_SELECT="" ;;
        *) continue ;;
      esac

      # 2. Source mixdepth (skipped with manual UTXO selection: the mixdepth
      #    is derived from the selected UTXOs)
      if [ -z "$SEND_SELECT" ]; then
        while true; do
          SEND_MIXDEPTH=$(prompt_param "Choose a mixdepth to send from" \
            "$(display_send_status "Source mixdepth (account) to send from.")" \
            "") || continue 2
          if [ -z "$SEND_MIXDEPTH" ]; then
            SEND_MIXDEPTH=$DEFAULT_MIXDEPTH
            break
          fi
          if ! [[ "$SEND_MIXDEPTH" =~ ^[0-9]+$ ]]; then
            whiptail --title " Error " --msgbox "Mixdepth must be a non-negative integer." 8 50
            continue
          fi
          break
        done
        SEND_MIXDEPTH=$(to_int "$SEND_MIXDEPTH" "$DEFAULT_MIXDEPTH")
      fi

      # 3. Amount in satoshis
      while true; do
        SEND_AMOUNT=$(prompt_param "Send Amount" \
          "$(display_send_status "Amount in satoshis to send.\n0 = sweep (entire mixdepth, or your picked UTXOs with manual coin control; best privacy for coinjoin).")" \
          "") || continue 2
        if [ -z "$SEND_AMOUNT" ]; then
          SEND_AMOUNT=$DEFAULT_AMOUNT
          break
        fi
        if ! [[ "$SEND_AMOUNT" =~ ^[0-9]+$ ]]; then
          whiptail --title " Error " --msgbox "Amount must be a non-negative integer." 8 50
          continue
        fi
        break
      done
      SEND_AMOUNT=$(to_int "$SEND_AMOUNT" "$DEFAULT_AMOUNT")

      # 4. Counterparties (0 = normal transaction, >0 = coinjoin)
      while true; do
        SEND_CP=$(prompt_param "Counterparties" \
          "$(display_send_status "Number of counterparties (makers) for CoinJoin.\n0 = normal transaction (no CoinJoin).\nRecommended for CoinJoin: 4-10.")" \
          "") || continue 2
        if [ -z "$SEND_CP" ]; then
          SEND_CP=$DEFAULT_COUNTERPARTIES
          break
        fi
        if ! [[ "$SEND_CP" =~ ^[0-9]+$ ]]; then
          whiptail --title " Error " --msgbox "Counterparties must be a non-negative integer." 8 50
          continue
        fi
        break
      done
      SEND_CP=$(to_int "$SEND_CP" "$DEFAULT_COUNTERPARTIES")

      # 5. Fee rate
      while true; do
        SEND_FEE=$(prompt_param "Fee Rate" \
          "$(display_send_status "Fee rate in sats/vB.\nLeave blank for auto (block target in config).")" \
          "") || continue 2
        if [ -z "$SEND_FEE" ]; then
          break
        fi
        if ! [[ "$SEND_FEE" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
          whiptail --title " Error " --msgbox "Fee rate must be a numeric value in sats/vB." 8 50
          continue
        fi
        break
      done

      # Set flag to show "auto" instead of "(default: auto)" in next prompts
      SEND_FEE_ENTERED="1"

      # 6. Destination address (if empty: INTERNAL coinjoin to next mixdepth)
      while true; do
        SEND_DEST=$(prompt_param "Destination Address" \
          "$(display_send_status "Enter destination bitcoin address.\nLeave empty for INTERNAL (next mixdepth, coinjoin only).")" \
          "") || continue 2

        # Apply INTERNAL default for coinjoin when destination is empty
        if [ -z "$SEND_DEST" ] && [ "$SEND_CP" -gt 0 ] 2>/dev/null; then
          SEND_DEST="INTERNAL"
          break
        elif [ -z "$SEND_DEST" ]; then
          whiptail --title " Error " --msgbox "Destination address is required for normal transactions." 8 50
          continue
        fi

        # Validate destination looks like a bitcoin address (unless INTERNAL)
        if [ "$SEND_DEST" != "INTERNAL" ]; then
          if ! [[ "$SEND_DEST" =~ ^[13mn2][a-km-zA-HJ-NP-Z1-9]{25,40}$ || \
                  "$SEND_DEST" =~ ^(bc|tb|bcrt)1[0-9ac-hj-np-z]{11,71}$ ]]; then
            whiptail --title " Error " --msgbox "Destination does not look like a valid Bitcoin address." 8 55
            continue
          fi
        fi
        break
      done

      # Determine transaction type label for summary
      if [ "$SEND_CP" -gt 0 ] 2>/dev/null; then
          TX_TYPE="CoinJoin ($SEND_CP counterparties)"
      else
          TX_TYPE="Normal transaction"
      fi

      # Fee display
      if [ -n "$SEND_FEE" ]; then
          FEE_DISPLAY="${SEND_FEE} sats/vB"
      else
          FEE_DISPLAY="auto (3-block estimate)"
      fi

      # Amount display
      if [ "$SEND_AMOUNT" = "0" ]; then
          AMOUNT_DISPLAY="0 (sweep)"
      else
          AMOUNT_DISPLAY="${SEND_AMOUNT} sats"
      fi

      # Mixdepth display (manual selection derives it from the chosen UTXOs)
      if [ -n "$SEND_SELECT" ]; then
          MIXDEPTH_DISPLAY="manual UTXO selection"
      else
          MIXDEPTH_DISPLAY="$SEND_MIXDEPTH"
      fi

      # Show confirmation summary
      whiptail --title " Confirm Send " --yesno \
"  From wallet:     $(basename "$CURRENT_WALLET")
  Type:            ${TX_TYPE}
  Source Mixdepth: ${MIXDEPTH_DISPLAY}
  Amount:          ${AMOUNT_DISPLAY}
  Fee Rate:        ${FEE_DISPLAY:-auto}
  Destination:     ${SEND_DEST}

  Proceed with transaction?" 14 66 || continue

      # Execute the appropriate command
      clear
      echo "Please wait..."
      (
          ensure_wallet_password "$CURRENT_WALLET" || exit 1
          ensure_wallet_unlocked_global || exit 1
          if [ "$SEND_CP" -gt 0 ] 2>/dev/null; then
              # CoinJoin via jm-taker
              echo "=== CoinJoin Send ==="
              echo ""
              echo "Wallet: $(basename "$CURRENT_WALLET")"
              echo "Counterparties: $SEND_CP"
              echo "Press Ctrl+C to abort."
              echo ""
              echo "Connecting to peers and selecting makers -- this may take"
              echo "several minutes.  All progress is shown below."
              echo ""
              echo "You will be prompted twice:"
              echo "  1. After makers are selected (fee estimate, confirm the plan)"
              echo "  2. Before broadcast (final on-chain fees, last chance to cancel)"
              echo ""
              if [ -n "$SEND_SELECT" ]; then
                  echo "The UTXO selector opens first: pick the inputs to spend"
                  echo "(the first toggled UTXO pins the source mixdepth)."
                  echo ""
              fi

              TAKER_ARGS=(coinjoin -a "$SEND_AMOUNT" -d "$SEND_DEST")
              if [ -n "$SEND_SELECT" ]; then
                  # Manual coin control: full-wallet selector TUI; the first
                  # toggled UTXO pins the source mixdepth
                  TAKER_ARGS+=(-s)
              else
                  TAKER_ARGS+=(-m "$SEND_MIXDEPTH")
              fi
              TAKER_ARGS+=(-n "$SEND_CP")
              [ -n "$SEND_FEE" ] && TAKER_ARGS+=(--fee-rate "$SEND_FEE")

              jm-taker "${TAKER_ARGS[@]}"
              TAKER_EXIT=$?
              echo ""
              if [ $TAKER_EXIT -eq 0 ]; then
                  echo "================================================"
                  echo " CoinJoin complete.  Check history for the txid."
                  echo "================================================"
              else
                  echo "================================================"
                  echo " CoinJoin failed or was cancelled."
                  echo " See the log output above for details."
                  echo "================================================"
              fi
          else
              # Normal transaction via jm-wallet send
              echo "=== Send Bitcoin ==="
              echo ""
              echo "Wallet: $(basename "$CURRENT_WALLET")"
              echo ""

              SEND_ARGS=(send -a "$SEND_AMOUNT")
              if [ -n "$SEND_SELECT" ]; then
                  # Manual coin control: full-wallet selector TUI; the first
                  # toggled UTXO pins the source mixdepth
                  SEND_ARGS+=(-s)
              else
                  SEND_ARGS+=(-m "$SEND_MIXDEPTH")
              fi
              [ -n "$SEND_FEE" ] && SEND_ARGS+=(--fee-rate "$SEND_FEE")
              SEND_ARGS+=("$SEND_DEST")

              jm-wallet "${SEND_ARGS[@]}"
              SEND_EXIT=$?

              echo ""
              if [ $SEND_EXIT -eq 0 ]; then
                  echo "================================================"
                  echo " Transaction sent successfully."
                  echo "================================================"
              else
                  echo "================================================"
                  echo " Transaction failed or was cancelled."
                  echo " See the log output above for details."
                  echo "================================================"
              fi
          fi

          # pause INSIDE subshell - only runs if password succeeded
          pause
      )
      clear
      ;;

    # ------------------------------------------------------------------
    # WALLET MANAGEMENT
    # ------------------------------------------------------------------
    W)
      # Wallet Management Submenu - loops until BACK is selected
      while true; do
        # Refresh wallet info at the START of each W submenu iteration
        check_stale_wallet

        WCHOICE=$(whiptail --title " Wallet Management " \
         --menu "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS" \
          23 78 12 \
          "BAL"      "View Wallet Info / Balance" \
          "HIST"     "CoinJoin History" \
          "REFRESH"  "Refresh / Reconstruct Wallet History" \
          "IDREG"    "Register Derived Wallet Identity" \
          "IDSEL"    "Select Registered Wallet Identity" \
          "FREEZE"   "UTXO Freeze Manager" \
          "NEW"      "Create New Wallet (12 or 24-word seed)" \
          "IMP"      "Import Existing Wallet (from seed)" \
          "SEL"      "Select Active Wallet" \
          "VAL"      "Validate a Seed Phrase" \
          "SEED"     "Show Seed Words" \
          "BACK"     "Back to Main Menu" 3>&1 1>&2 2>&3)

        # Handle ESC/Cancel - exit W submenu
        [ $? -ne 0 ] && break

        case $WCHOICE in
          # --------------------------------------------------------------
          # BAL - Wallet Info (with submenu)
          # --------------------------------------------------------------
          BAL)
              while true; do
                  check_stale_wallet
                  if ! ensure_active_wallet; then
                      break
                  fi

                  INFO_CHOICE=$(whiptail --title " Wallet Info " \
                    --menu "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS\n\nChoose info display level:" \
                    18 64 3 \
                    "BASIC" "Basic balance by mixdepth" \
                    "EXT" "Extended (detailed address list with status)" \
                    "BACK" "Back to Wallet Management" 3>&1 1>&2 2>&3)

                  # Handle ESC/Cancel
                  [ $? -ne 0 ] && break

                  case $INFO_CHOICE in
                      BASIC)
                          clear
                          echo "=== Wallet Info (Basic) ==="
                          echo ""
                          echo "Active wallet: $(basename "$CURRENT_WALLET")"
                          echo "Preparing wallet..."
                          echo ""
                          (
                              ensure_wallet_password "$CURRENT_WALLET" || exit 1
                              ensure_wallet_unlocked_global || exit 1
                              jm-wallet info
                              pause
                          )
                          clear
                          ;;
                      EXT)
                          clear
                          echo "=== Wallet Info (Extended) ==="
                          echo ""
                          echo "Active wallet: $(basename "$CURRENT_WALLET")"
                          echo "Preparing wallet..."
                          echo ""
                          (
                              ensure_wallet_password "$CURRENT_WALLET" || exit 1
                              ensure_wallet_unlocked_global || exit 1
                              jm-wallet info --extended
                              pause
                          )
                          clear
                          ;;
                      BACK|"")
                          break
                          ;;
                  esac
              done
              ;;

          # --------------------------------------------------------------
          # HIST - CoinJoin History
          # --------------------------------------------------------------
          HIST)
              if ! ensure_active_wallet; then
                  continue
              fi

              # Prompt parameters with whiptail
              HIST_ROLE=$(prompt_param "Role Filter" \
                "Filter by role: maker, taker.\nLeave blank for all." \
                "") || continue
              # Validate role
              if [ -n "$HIST_ROLE" ]; then
                  case "$HIST_ROLE" in
                      maker|taker) ;;
                      *)
                          whiptail --title " Error " --msgbox "Invalid role '${HIST_ROLE}'.\nAllowed: maker, taker, or blank for all." 9 50
                          continue
                          ;;
                  esac
              fi

              HIST_LIMIT=$(prompt_param "Max Entries" \
                "Maximum number of entries to show.\nLeave blank for all." \
                "") || continue
              # Validate limit is numeric
              if [ -n "$HIST_LIMIT" ] && ! [[ "$HIST_LIMIT" =~ ^[0-9]+$ ]]; then
                  whiptail --title " Error " --msgbox "Limit must be a positive integer." 8 40
                  continue
              fi

              whiptail --title " Statistics " \
                --yesno "Show statistics summary?" \
                8 40 --defaultno 3>&1 1>&2 2>&3
              HIST_SHOW_STATS=$?

              # Build summary entries
              ROLE_DISPLAY="${HIST_ROLE:-all}"
              LIMIT_DISPLAY="${HIST_LIMIT:-all}"
              if [ $HIST_SHOW_STATS -eq 0 ]; then
                  STATS_DISPLAY="yes"
              else
                  STATS_DISPLAY="no"
              fi

              show_summary "Confirm History -- $(basename "$CURRENT_WALLET")" \
                "Role filter|all|${ROLE_DISPLAY}" \
                "Max entries|all|${LIMIT_DISPLAY}" \
                "Show statistics|no|${STATS_DISPLAY}" || continue

              HIST_ARGS=()
              [ -n "$HIST_ROLE" ]  && HIST_ARGS+=(-r "$HIST_ROLE")
              [ -n "$HIST_LIMIT" ] && HIST_ARGS+=(-n "$HIST_LIMIT")
              [ $HIST_SHOW_STATS -eq 0 ] && HIST_ARGS+=(-s)

              clear
              echo "=== CoinJoin History ==="
              echo ""
              echo "Active wallet: $(basename "$CURRENT_WALLET")"
              echo "Preparing wallet..."
              echo ""
              (
                  jm-wallet history --mnemonic-file "$CURRENT_WALLET" "${HIST_ARGS[@]}"
                  pause
              )
              clear
              ;;

          # --------------------------------------------------------------
          # REFRESH - Explicit backend synchronization and deferred reconstruction
          # --------------------------------------------------------------
          REFRESH)
              ensure_active_wallet || continue
              (
                  ensure_wallet_password "$CURRENT_WALLET" || exit 1
                  ensure_wallet_unlocked_global || exit 1
                  jm-wallet info
                  pause
              )
              ;;
          IDREG)
              ensure_active_wallet || continue
              (
                  ensure_wallet_password "$CURRENT_WALLET" || exit 1
                  jm-wallet identity register --mnemonic-file "$CURRENT_WALLET" \
                      --prompt-bip39-passphrase --confirm-bip39-passphrase
                  pause
              )
              ;;
          IDSEL)
              ensure_active_wallet || continue
              IDENTITY_JSON=$(jm-wallet identity list --mnemonic-file "$CURRENT_WALLET" --json) || continue
              IDENTITY_OPTIONS=()
              while IFS=$'\t' read -r fingerprint status; do
                  [ -n "$fingerprint" ] && IDENTITY_OPTIONS+=("$fingerprint" "$status")
              done < <(printf '%s' "$IDENTITY_JSON" | "$TUI_PYTHON" -c '
import json, sys
for item in json.load(sys.stdin):
    print(item["fingerprint"] + "\tBIP39: " + item["bip39"] + (" (selected)" if item["selected"] else ""))
')
              if [ ${#IDENTITY_OPTIONS[@]} -eq 0 ]; then
                  whiptail --title " Identity Unconfirmed " --msgbox "Register the intended wallet identity first. Existing wallet data has not changed." 9 65
                  continue
              fi
              IDENTITY_CHOICE=$(whiptail --title " Select Wallet Identity " --menu "Choose the intended derived wallet:" 18 70 8 "${IDENTITY_OPTIONS[@]}" 3>&1 1>&2 2>&3) || continue
              clear_config_value wallet bip39_passphrase || continue
              jm-wallet identity select "$IDENTITY_CHOICE" --mnemonic-file "$CURRENT_WALLET" || continue
              rm -f "$FINGERPRINT_CACHE" "$MAKER_ENV"
              unset BIP39_PASSPHRASE EXPECTED_FINGERPRINT
              ;;

          # --------------------------------------------------------------
          # FREEZE - Freeze/Unfreeze UTXOs
          # --------------------------------------------------------------
          FREEZE)
              if ! ensure_active_wallet; then
                  continue
              fi

              clear
              echo "=== Freeze / Unfreeze UTXOs ==="
              echo ""
              echo "Active wallet: $(basename "$CURRENT_WALLET")"
              echo "Opening interactive UTXO Freeze Manager...."
              echo ""
              (
                  ensure_wallet_password "$CURRENT_WALLET" || exit 1
                  ensure_wallet_unlocked_global || exit 1
                  jm-wallet freeze
                  pause
              )
              clear
              ;;

          # --------------------------------------------------------------
          # NEW - Create New Wallet
          # --------------------------------------------------------------
          NEW)
              WNAME=$(whiptail --title " Create New Wallet " \
                  --inputbox "Enter wallet name (leave empty for 'default'):" \
                  10 55 "" 3>&1 1>&2 2>&3) || continue
              # Use 'default' if empty
              WNAME="${WNAME:-default}"
              # Strip extension if provided, we add .mnemonic
              WNAME="${WNAME%.mnemonic}"
              # Validate: only safe characters, no path separators
              if [[ ! "$WNAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
                  whiptail --title " Error " --msgbox "Invalid wallet name.\nUse only letters, numbers, dot, underscore, and hyphen." 9 55
                  continue
              fi

              # Check if wallet already exists (issue #476)
              WALLET_PATH="$DATA_DIR/wallets/${WNAME}.mnemonic"
              if [ -f "$WALLET_PATH" ]; then
                  if ! whiptail --title " Wallet Already Exists " --defaultno \
                      --yesno "A wallet named '${WNAME}' already exists.\n\nOverwrite?" 9 55; then
                      continue
                  fi
              fi

              # Ask for seed word count (default 24; 12 is also widely supported)
              WORDS_CHOICE=$(whiptail --title " Create New Wallet " --notags \
                  --menu "How many seed words should the new wallet have?" 12 55 2 \
                  "24" "24 words (recommended, 256-bit entropy)" \
                  "12" "12 words (128-bit entropy)" \
                  3>&1 1>&2 2>&3) || continue
              WORDS="${WORDS_CHOICE:-24}"

              mkdir -p "$DATA_DIR/wallets"

              # Collect the encryption password via whiptail so the user
              # does not get dropped into the CLI prompt and does not get
              # asked a second time when storing it in config.toml later
              # (issue #462). An empty password disables encryption.
              NEW_PWD=$(prompt_new_wallet_password) || continue

              clear
              echo "=== Create New Wallet ==="
              echo ""
              echo "This will generate a new ${WORDS}-word BIP39 mnemonic."
              echo "IMPORTANT: Write down the seed words! They are your backup."
              echo ""
              echo "Generating wallet..."
              MNEMONIC_PASSWORD="$NEW_PWD" jm-wallet generate \
                  --words "$WORDS" --no-prompt-password --force -o "$WALLET_PATH"
              RESULT=$?

              if [ $RESULT -eq 0 ] && [ -f "$WALLET_PATH" ]; then
                  echo ""
                  echo "Wallet saved to: $WALLET_PATH"
                  echo ""
                  echo "IMPORTANT: Write down your seed words above before continuing!"
                  pause
                  # Pass the captured password to post_wallet_create so the
                  # "store in config" branch can reuse it directly.
                  post_wallet_create "$WALLET_PATH" "$NEW_PWD"
              else
                  echo "Wallet creation may have failed. Check output above."
                  pause
              fi
              unset NEW_PWD
              clear
              ;;

          # --------------------------------------------------------------
          # IMP - Import Wallet
          # --------------------------------------------------------------
          IMP)
              WNAME=$(whiptail --title " Import Wallet " \
                  --inputbox "Enter wallet name (leave empty for 'imported'):" \
                  10 55 "" 3>&1 1>&2 2>&3) || continue
              # Use 'imported' if empty
              WNAME="${WNAME:-imported}"
              WNAME="${WNAME%.mnemonic}"
              # Validate: only safe characters, no path separators
              if [[ ! "$WNAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
                  whiptail --title " Error " --msgbox "Invalid wallet name.\nUse only letters, numbers, dot, underscore, and hyphen." 9 55
                  continue
              fi

              # Check if wallet already exists (issue #476)
              WALLET_PATH="$DATA_DIR/wallets/${WNAME}.mnemonic"
              if [ -f "$WALLET_PATH" ]; then
                  if ! whiptail --title " Wallet Already Exists " --defaultno \
                      --yesno "A wallet named '${WNAME}' already exists.\n\nOverwrite?" 9 55; then
                      continue
                  fi
              fi

              # Ask for word count
              WORDS_CHOICE=$(whiptail --title " Import Wallet " --notags \
                  --menu "How many seed words does your wallet have?" 12 50 2 \
                  "24" "24 words" \
                  "12" "12 words" \
                  3>&1 1>&2 2>&3) || continue
              WORDS="${WORDS_CHOICE:-24}"

              mkdir -p "$DATA_DIR/wallets"

              # Collect the encryption password upfront (issue #462).
              NEW_PWD=$(prompt_new_wallet_password) || continue

              clear
              echo "=== Import Wallet from Seed ==="
              echo ""
              echo "You will be prompted to enter your BIP39 seed words."
              echo ""
              MNEMONIC_PASSWORD="$NEW_PWD" jm-wallet import \
                  --words "$WORDS" --no-prompt-password --force -o "$WALLET_PATH"
              RESULT=$?

              if [ $RESULT -eq 0 ] && [ -f "$WALLET_PATH" ]; then
                  echo ""
                  echo "Wallet imported to: $WALLET_PATH"
                  whiptail --title " Wallet Recovery " --msgbox \
                      "Seed imported. Wallet recovery is not yet verified.\n\nPreviously spent addresses may still appear unused. Before receiving or spending, synchronize and compare known addresses and balances with your original records.\n\nA visible balance or an idle scan does not prove complete recovery. Historical recovery requires a backend with the necessary block and history data." 16 76
                  clear
                  post_wallet_create "$WALLET_PATH" "$NEW_PWD"
              else
                  echo "Import may have failed. Check output above."
                  pause
              fi
              unset NEW_PWD
              clear
              ;;

          # --------------------------------------------------------------
          # SEL - Select Active Wallet
          # --------------------------------------------------------------
          SEL)
              WALLETS=$(list_wallets)
              if [ -z "$WALLETS" ]; then
                  whiptail --title " Select Wallet " --msgbox "No wallet files found in $DATA_DIR/wallets/\nCreate or import a wallet first." 9 55
                  continue
              fi

              # Build whiptail menu entries from wallet files
              MENU_ITEMS=()
              while IFS= read -r wf; do
                  MENU_ITEMS+=("$wf" "$wf")
              done <<< "$WALLETS"

              WNAME=$(whiptail --title " Select Active Wallet " --notags \
                  --menu "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS\n\nChoose a wallet:" \
                  18 64 6 \
                  "${MENU_ITEMS[@]}" 3>&1 1>&2 2>&3) || continue

              clear  # prevent stale terminal buffer from flashing after wallet selection
              echo "Please wait..."

              if [ -f "$DATA_DIR/wallets/$WNAME" ]; then
                  if ! set_active_wallet_config "$DATA_DIR/wallets/$WNAME"; then
                      show_config_save_error
                      clear
                      continue
                  fi

                  # Check if wallet is encrypted before offering to store password
                  # (issue #455 Case 3: previously the old password was cleared with
                  # no way to record the new one, leaving a wallet_file/password
                  # mismatch until the user edited config.toml by hand).
                  jm-wallet verify-password -f "$DATA_DIR/wallets/$WNAME" --no-prompt --password "" \
                      >/dev/null 2>&1
                  if [ $? -eq 2 ]; then
                      # Unencrypted wallet - no password to store
                      whiptail --title " Wallet Selected " --msgbox "Active wallet set to: $WNAME\n\nThis wallet is not encrypted.\nRestart the maker service for changes to take effect." 10 60
                  elif whiptail --title " Store Password " \
                      --yesno "Active wallet set to: $WNAME\n\nStore this wallet's password in config.toml?\nThis lets the maker start without prompting.\nChoose No to be asked for the password on each use." \
                      12 64 --defaultno 3>&1 1>&2 2>&3; then

                      prompt_and_store_password "$DATA_DIR/wallets/$WNAME" || \
                          whiptail --title " Password " --msgbox "Password not stored." 8 50
                      whiptail --title " Wallet Selected " --msgbox "Active wallet set to: $WNAME\n\nRestart the maker service for changes to take effect." 10 60
                  else
                      whiptail --title " Wallet Selected " --msgbox "Active wallet set to: $WNAME\n\nStored password cleared; you will be prompted\nfor the password on next use.\n\nRestart the maker service for changes to take effect." 12 60
                  fi

                  # Offer passphrase storage as well when the wallet uses a
                  # passphrase (wallet_with_passphrase flag, mirrors the
                  # password storage offer above) and none is stored yet.
                  if [ "$(get_wallet_with_passphrase)" = "true" ]; then
                      STORED_PP=$(get_stored_bip39_passphrase && printf '.')
                      STORED_PP=${STORED_PP%.}
                      if [ -z "$STORED_PP" ]; then
                          if whiptail --title " Store Passphrase " \
                              --yesno "Store this wallet's BIP39 passphrase in config.toml?\nThis lets the maker start without prompting.\nChoose No to be asked for the passphrase on each use." \
                              11 64 --defaultno 3>&1 1>&2 2>&3; then
                              prompt_and_store_bip39_passphrase || \
                                  whiptail --title " Passphrase " --msgbox "Passphrase not stored." 8 50
                          fi
                      fi
                  fi
              else
                  whiptail --title " Error " --msgbox "File not found: $DATA_DIR/wallets/$WNAME" 8 55
              fi
              clear
              ;;

          # --------------------------------------------------------------
          # VAL - Validate Seed
          # --------------------------------------------------------------
          VAL)
              clear
              echo "=== Validate Seed Phrase ==="
              echo ""
              echo "Check that a BIP39 mnemonic is valid before importing."
              echo ""
              jm-wallet validate
              pause
              ;;

          # --------------------------------------------------------------
          # SEED - Show Seed Words
          # --------------------------------------------------------------
          SEED)
              if ! ensure_active_wallet; then
                  continue
              fi

              if ! whiptail --title " Security Warning " \
                  --yesno "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS\n\nYou are about to display your BIP39 seed words.\n\nWARNING: Anyone with these words can spend all your funds.\nDo not share them, photograph them, or paste them into any website.\n\nMake sure you are in a private setting!\n\nContinue?" \
                  17 78 --defaultno 3>&1 1>&2 2>&3; then
                  continue
              fi

              clear
              echo "Please wait..."
              (
                  ensure_wallet_password "$CURRENT_WALLET" || exit 1
                  # Full unlock, same protection as BAL/HIST/FREEZE: flag-gated
                  # BIP39 passphrase prompt with fingerprint confirmation, so
                  # the user knows exactly which wallet the seed belongs to.
                  ensure_wallet_unlocked_global || exit 1
                  # --yes skips the CLI's own confirmation prompt: it is 1:1
                  # redundant with the whiptail security warning above.
                  jm-wallet showseed -f "$CURRENT_WALLET" --yes
                  echo ""
                  echo ""
                  echo "Press [Enter] to continue."
                  read -r _
              )
              clear
              ;;

          # --------------------------------------------------------------
          # BACK - Exit Wallet Management
          # --------------------------------------------------------------
          BACK)
              break
              ;;
        esac
      done  # End W submenu loop
      ;;

    # ------------------------------------------------------------------
    # MAKER BOT CONTROL
    # ------------------------------------------------------------------
    M)
      # Maker submenu
      while true; do
      check_stale_wallet
      if pgrep -f "jm-maker" > /dev/null 2>&1; then
        MAKER_STATUS="RUNNING"
      else
        MAKER_STATUS="STOPPED"
      fi

      MCHOICE=$(whiptail --title " Maker Bot Control " \
        --menu "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS" \
        18 64 6 \
        "START"   "Start Maker Bot" \
        "STOP"    "Stop Maker Bot" \
        "RESTART" "Restart Maker Bot" \
        "BONDS"   "Fidelity Bond Management" \
        "LOG"     "Follow Maker Logs (Ctrl+C to stop)" \
        "BACK"    "Back to Main Menu" 3>&1 1>&2 2>&3)

      [ $? -ne 0 ] && break

      case $MCHOICE in
          START)
              clear
              echo "=== Starting Maker Bot ==="
              echo ""
              echo "Active wallet: $(basename "$CURRENT_WALLET")"
              echo "Preparing wallet..."
              echo ""
              if ! ensure_active_wallet; then
                  continue
              fi
              offer_maker_password_storage
              offer_maker_passphrase_storage
              if [ "$RASPIBLITZ" = "1" ]; then
                  # Raspiblitz: the maker runs under systemd and reads the
                  # password from .maker.env. Prompt once here and stage it so
                  # the bonus 'maker-start' does not prompt again. The subshell
                  # keeps MNEMONIC_PASSWORD out of the rest of the session.
                  if ! ( stage_maker_password "$CURRENT_WALLET" ); then
                      pause
                      continue
                  fi
                  maker_start
              else
                  (
                      ensure_wallet_password "$CURRENT_WALLET" || exit 1
                      ensure_wallet_unlocked_global || exit 1
                      maker_start
                  )
              fi
              if [ $? -ne 0 ]; then
                  pause
                  continue
              fi
              sleep 2
              echo ""
              echo "Service status:"
              maker_status
              pause
              ;;
          STOP)
              clear
              if [ "$MAKER_STATUS" = "STOPPED" ]; then
                  # Show info in TUI msgbox
                  whiptail --title " Maker Bot " --msgbox "Maker Bot is not running." 8 40
              else
                  echo "=== Stopping Maker Bot ==="
                  echo ""
                  echo "Please wait..."
                  echo ""
                  STOP_OUT=$(maker_stop 2>&1)
                  if [ $? -eq 0 ]; then
                      # Show success in TUI msgbox
                      whiptail --title " Maker Bot " --msgbox "Maker Bot stopped successfully." 8 40
                  else
                      # Show simple error in msgbox
                      whiptail --title " Error " --msgbox "Failed to stop Maker Bot.\n\nCheck the terminal for details." 9 50

                      # Show error details on terminal
                      clear
                      echo "=== Maker Stop Error Details ==="
                      echo ""
                      echo "The following error details may help diagnose the problem."
                      echo ""
                      printf '%s\n' "$STOP_OUT"
                      echo ""
                      pause
                  fi
              fi
              ;;
          RESTART)
              clear
              if ! ensure_active_wallet; then
                  continue
              fi
              offer_maker_password_storage
              offer_maker_passphrase_storage
              if [ "$MAKER_STATUS" = "RUNNING" ]; then
                  echo "=== Restarting Maker Bot ==="
              else
                  echo "=== Starting Maker Bot ==="
              fi
              echo ""
              echo "Active wallet: $(basename "$CURRENT_WALLET")"
              echo "Preparing wallet..."
              echo ""
              if [ "$RASPIBLITZ" = "1" ]; then
                  (
                      # Validate/confirm before stopping the current maker.
                      # Its stop hook removes .maker.env, so keep the new
                      # credentials in a private pending file until afterward.
                      pending_env=$(mktemp "${MAKER_ENV}.restart.XXXXXX") || exit 1
                      trap 'rm -f -- "$pending_env"' EXIT
                      stage_maker_password "$CURRENT_WALLET" "$pending_env" || exit 1
                      if [ "$MAKER_STATUS" = "RUNNING" ]; then
                          maker_stop 2>&1 || exit 1
                      fi
                      mv -fT -- "$pending_env" "$MAKER_ENV" || exit 1
                      maker_start
                  )
                  RESTART_RC=$?
              else
                  (
                      ensure_wallet_password "$CURRENT_WALLET" || exit 1
                      ensure_wallet_unlocked_global || exit 1
                      if [ "$MAKER_STATUS" = "RUNNING" ]; then
                          echo "Stopping Maker Bot..."
                          echo "Please wait..."
                          maker_stop 2>&1 || exit 1
                      fi
                      maker_start
                  )
                  RESTART_RC=$?
              fi
              if [ $RESTART_RC -ne 0 ]; then
                  pause
                  clear
                  continue
              fi
              clear
              sleep 2
              echo ""
              echo "Service status:"
              maker_status
              pause
              ;;
          BONDS)
              # Fidelity bond submenu
              while true; do
                check_stale_wallet
                BCHOICE=$(whiptail --title " Fidelity Bonds " \
                  --menu "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS\n\nFidelity Bonds lock coins until a date to boost maker reputation.\nExpired bonds appear in wallet balance and are spendable." \
                  18 72 3 \
                  "LIST"   "List existing Fidelity Bonds" \
                  "CREATE" "Generate a new bond address (lock coins)" \
                  "BACK"   "Back to Maker Menu" 3>&1 1>&2 2>&3)
                [ $? -ne 0 ] && break
                case $BCHOICE in
                    LIST)
                        if ! ensure_active_wallet; then
                            continue
                        fi
                        BONDS_OUT_FILE=$(mktemp)
                        clear
                        echo "=== Fidelity Bonds ==="
                        echo ""
                        echo "Active wallet: $(basename "$CURRENT_WALLET")"
                        echo "Preparing wallet..."
                        echo ""
                        (
                            ensure_wallet_password "$CURRENT_WALLET" || exit 1
                            ensure_wallet_unlocked_global || exit 1
                            jm-wallet list-bonds > "$BONDS_OUT_FILE" 2>&1 || exit 2
                        )
                        BONDS_RC=$?
                        BONDS_OUT=$(cat "$BONDS_OUT_FILE")
                        rm -f "$BONDS_OUT_FILE"
                        # Strip ANSI escape sequences from output (issue #459)
                        BONDS_OUT=$(printf '%s' "$BONDS_OUT" | sed 's/\x1b$$[0-9;]*[mK]//g')
                        if [ "$BONDS_RC" -eq 1 ]; then
                            # Password failure - "Too many attempts" msgbox already shown
                            :
                        elif [ "$BONDS_RC" -ne 0 ]; then
                            whiptail --title " Fidelity Bonds -- Error " --msgbox \
                                "Failed to list Fidelity Bonds.\n\n${BONDS_OUT:-(no output)}" \
                                20 76
                        elif printf '%s' "$BONDS_OUT" | grep -qi "No Fidelity Bonds"; then
                            whiptail --title " Fidelity Bonds " --msgbox \
                                "No Fidelity Bonds found for this wallet.\n\nUse 'CREATE' to generate a bond address, or send\ncoins to an existing one to fund it." \
                                12 64
                        else
                            # Normal listing: keep the tabular output on the
                            # terminal because whiptail's msgbox is too narrow
                            # for the bond table.
                            clear
                            echo "=== Fidelity Bonds ==="
                            echo ""
                            printf '%s\n' "$BONDS_OUT"
                            pause
                        fi
                        clear
                        ;;
                    CREATE)
                        if ! ensure_active_wallet; then
                            continue
                        fi
                        clear
                        echo "=== Generating Bond Address ==="
                        echo ""
                        echo "Active wallet: $(basename "$CURRENT_WALLET")"
                        echo "Preparing wallet..."
                        echo ""
                        (
                            ensure_wallet_password "$CURRENT_WALLET" || exit 1
                            ensure_wallet_unlocked_global || exit 1

                            # Lockdate (required) - validate format and warn on past dates
                            CURRENT_YM=$(date +%Y-%m)
                            while true; do
                                clear
                                LOCKDATE=$(prompt_param "Fidelity Bond Lockdate" \
                                  "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS\n\nEnter lockdate as YYYY-MM.\nCoins are NOT spendable until this date!" \
                                  "") || exit 1
                                if ! [[ "$LOCKDATE" =~ ^[0-9]{4}-[0-9]{2}$ ]]; then
                                    whiptail --title " Date Error " --msgbox "Invalid date format.\nUse YYYY-MM (e.g. 2027-06)." 8 50
                                    continue
                                fi
                                # Validate month is in 01..12
                                LOCKDATE_MONTH="${LOCKDATE:5:2}"
                                if [[ "$LOCKDATE_MONTH" < "01" || "$LOCKDATE_MONTH" > "12" ]]; then
                                    whiptail --title " Date Error " --msgbox "Invalid month.\nMonth must be between 01 and 12." 8 50
                                    continue
                                fi
                                # Validate date is not before epoch (2020-01)
                                if [[ "$LOCKDATE" < "2020-01" ]]; then
                                    whiptail --title " Date Error " --msgbox "Invalid lockdate.\nMinimum lockdate is 2020-01." 8 50
                                    continue
                                fi
                                # Validate year is not after 2099 (jm-wallet maximum)
                                if [[ "$LOCKDATE" > "2099-12" ]]; then
                                    whiptail --title " Date Error " --msgbox "Invalid lockdate.\nMaximum lockdate is 2099-12." 8 50
                                    continue
                                fi

                                # Final confirmation with explicit lock warning
                                if [[ "$LOCKDATE" < "$CURRENT_YM" ]]; then
                                    # Past date - coins are already spendable
                                    CONFIRM_TEXT="\n$WALLET_INFO | Maker Bot: $MAKER_STATUS\n\nGenerate a Fidelity Bond address?\n\nLockdate: ${LOCKDATE} (Date is in the past)\n\nWARNING: This lockdate is in the past.\n         All coins on this address are already SPENDABLE.\n         This is only useful for recreating existing bond addresses.\n\nProceed?"
                                else
                                    # Future date - coins will be locked
                                    CONFIRM_TEXT="\n$WALLET_INFO | Maker Bot: $MAKER_STATUS\n\nGenerate a Fidelity Bond address?\n\nLockdate: ${LOCKDATE}\n\nWARNING: Coins sent to this address will be LOCKED\n         and NOT spendable until ${LOCKDATE}.\n\nProceed?"
                                fi

                                if ! whiptail --title " Confirm Fidelity Bond " \
                                    --yesno "$CONFIRM_TEXT" \
                                    16 64 --defaultno 3>&1 1>&2 2>&3; then
                                    continue  # Back to lockdate prompt
                                fi

                                break  # Everything confirmed
                            done

                            # Generate bond address, capture output for address extraction
                            BONDS_CREATE_OUT=$(jm-wallet generate-bond-address \
                              --locktime-date "${LOCKDATE}" 2>&1)
                            BONDS_CREATE_RC=$?

                            if [ $BONDS_CREATE_RC -eq 0 ]; then
                                # Extract the address. Match all bech32 HRPs
                                # (mainnet bc1, testnet/signet tb1, regtest bcrt1)
                                # plus legacy base58 (1.../3...). The longer
                                # bech32 prefixes must come first in the
                                # alternation, otherwise grep would greedily
                                # accept the inner '1' of "tb1"/"bcrt1" via the
                                # legacy [13] branch and drop the network HRP
                                # (e.g. "tb1qvksm..." -> "1qvksm...").
                                BOND_ADDR=$(printf '%s' "$BONDS_CREATE_OUT" | grep -oE '(bcrt1|bc1|tb1|[13])[a-zA-HJ-NP-Z0-9]{25,87}' | head -1)

                                # Show address prominently in whiptail msgbox
                                if [[ "$LOCKDATE" < "$CURRENT_YM" ]]; then
                                    # Past date - coins are already spendable
                                    BOND_MSG="Fidelity Bond address generated:\n\n${BOND_ADDR}\n\nFunds are ALREADY SPENDABLE (lockdate is in the past)."
                                else
                                    # Future date - funds will be locked
                                    BOND_MSG="Fidelity Bond address generated:\n\n${BOND_ADDR}\n\nSend coins to this address to create the Fidelity Bond.\nFunds will be LOCKED until ${LOCKDATE}."
                                fi
                                whiptail --title " Fidelity Bond Address " --msgbox \
                                    "$BOND_MSG" \
                                    14 70
                            else
                                # Show simple error in msgbox
                                whiptail --title " Error " --msgbox \
                                    "Failed to generate Fidelity Bond address.\n\nCheck the terminal output for details." \
                                    10 55

                                # Show error details on terminal
                                clear
                                echo "=== Fidelity Bond Error Details ==="
                                echo ""
                                echo "The following error details may help diagnose the problem."
                                echo ""
                                printf '%s\n' "$BONDS_CREATE_OUT"
                                echo ""
                                pause
                            fi
                        )
                        clear
                        ;;
                    BACK|"")
                        break
                        ;;
                esac
              done
              ;;
          LOG)
              clear
              LOG_FILE="$LOG_DIR/maker.log"
              if [ -r "$LOG_FILE" ]; then
                  echo "=== Maker Logs ==="
                  echo "Press Ctrl+C to stop following."
                  echo ""
                  tail -n 50 -f "$LOG_FILE"
                  pause
              elif [ "$RASPIBLITZ" = "1" ]; then
                  # Raspiblitz: try journalctl fallback (untested, needs maintainer review)
                  echo "=== Maker Logs ==="
                  echo "Press Ctrl+C to stop following."
                  echo ""
                  echo "No log file found at $LOG_FILE (maker may not have run yet)."
                  echo "Trying journalctl..."
                  maker_status
                  pause
              else
                  # Standalone: no persistent logs
                  whiptail --title " Maker Logs " --msgbox "No log file found.\n\nStart the Maker Bot first to generate logs." 9 50
              fi
              ;;
          BACK)
              break
              ;;
      esac
      done
      ;;

    C)
      # Config Center submenu
      while true; do
        check_stale_wallet
        CCHOICE=$(whiptail --title " Config Center " \
          --menu "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS" \
          22 64 10 \
          "LOG"    "Configure Log Level" \
          "PPFLAG" "Configure BIP39 Onboarding" \
          "DELPW"  "Delete Active Wallet Password" \
          "DELPP"  "Delete Active Wallet Passphrase" \
          ""   "" \
          "EDIT"   "Edit config.toml manually (nano)" \
          "BACKUP" "Backup config.toml" \
          "REST"   "Restore config.toml" \
          ""   "" \
          "BACK"   "Back to Main Menu" 3>&1 1>&2 2>&3)

        [ $? -ne 0 ] && break

        case $CCHOICE in
          LOG)
            CURRENT_LOG="${LOGGING__LEVEL:-INFO}"

            LOG_CHOICE=$(whiptail --title " Log Level " --notags \
              --menu "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS\n\nSelect log level:\n\nCurrent: ${CURRENT_LOG}" \
              20 68 6 \
              "GLOBAL"  "Follow global logging (or session environment)" \
              "TRACE"   "TRACE   - Most verbose troubleshooting output" \
              "DEBUG"   "DEBUG   - Detailed debugging information" \
              "INFO"    "INFO    - General information messages" \
              "WARNING" "WARNING - Warning messages only (quiet menu)" \
              "ERROR"   "ERROR   - Error messages only" 3>&1 1>&2 2>&3) || continue

            if [ "$LOG_CHOICE" = "GLOBAL" ]; then
              clear_config_value tui log_level || {
                whiptail --title " Config Error " --msgbox "Could not save the log level to config.toml." 8 55
                continue
              }
              LOG_CHOICE="$(get_tui_log_level)"
            elif ! set_config_value tui log_level "$LOG_CHOICE"; then
              whiptail --title " Config Error " --msgbox "Could not save the log level to config.toml." 8 55
              continue
            fi

            if [ -n "$LOG_CHOICE" ]; then
              export LOGGING__LEVEL="$LOG_CHOICE"
              whiptail --title " Log Level " --msgbox "Log level set to: $LOG_CHOICE\n\nApplies to subsequent commands launched here.\nAlready-running processes are unchanged.\nExternally managed services use their own logging configuration." 12 70
            fi
            ;;

          PPFLAG)
            PP_CURRENT=$(get_wallet_with_passphrase)
            if [ "$PP_CURRENT" = "true" ]; then
              PP_CURRENT="ON"
            else
              PP_CURRENT="OFF"
            fi

            PP_CHOICE=$(whiptail --title " BIP39 Passphrase Mode " --notags \
              --menu "\nEnable BIP39 onboarding?\n\nCurrent: ${PP_CURRENT}\n\nON: prompt for unregistered wallets and confirm new identities.\nOFF (default): ordinary legacy wallets do not prompt.\n\nRegistered wallet requirements always apply. Recorded history stays offline." \
              19 70 2 \
              "ON"  "Enable advanced onboarding" \
              "OFF" "Disable onboarding (default)" 3>&1 1>&2 2>&3) || continue

            if [ "$PP_CHOICE" = "ON" ]; then
              PP_VALUE="true"
            else
              PP_VALUE="false"
            fi
            if clear_config_value wallet wallet_with_passphrase && \
                set_config_bool wallet bip39_passphrase_enabled "$PP_VALUE"; then
              # The derived wallet may change with this flag; drop the cached
              # fingerprint so the next action recomputes it.
              rm -f "$FINGERPRINT_CACHE"
              whiptail --title " BIP39 Passphrase Mode " \
                --msgbox "BIP39 passphrase mode set to: $PP_CHOICE" 8 50
            else
              whiptail --title " Config Error " --msgbox "Could not save the setting to config.toml." 8 55
            fi
            ;;

          DELPW)
            if ! ensure_active_wallet; then
              continue
            fi

            STORED_PW=$(get_stored_mnemonic_password && printf '.')
            STORED_PW=${STORED_PW%.}
            if [ -z "$STORED_PW" ]; then
              whiptail --title " Delete Password " --msgbox "No wallet password is currently stored in config.toml." 8 50
              continue
            fi

            if whiptail --title " Delete Password " --yesno \
              "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS\n\nDelete the stored password for wallet:\n$(basename "$CURRENT_WALLET")\n\nThis will require entering the password on next use." \
              13 60 --defaultno 3>&1 1>&2 2>&3; then

              if clear_config_value wallet mnemonic_password; then
                whiptail --title " Password Deleted " --msgbox "Wallet password removed from config.toml." 8 50
              else
                whiptail --title " Config Error " --msgbox "Could not remove the wallet password from config.toml." 8 55
              fi
            fi
            ;;

          DELPP)
            if ! ensure_active_wallet; then
              continue
            fi

            STORED_PP=$(get_stored_bip39_passphrase && printf '.')
            STORED_PP=${STORED_PP%.}
            if [ -z "$STORED_PP" ]; then
              whiptail --title " Delete Passphrase " --msgbox "No BIP39 passphrase is currently stored in config.toml." 8 50
              continue
            fi

            if whiptail --title " Delete Passphrase " --yesno \
              "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS\n\nDelete the stored BIP39 passphrase for wallet:\n$(basename "$CURRENT_WALLET")\n\nThis will require entering the passphrase on next use." \
              13 60 --defaultno 3>&1 1>&2 2>&3; then

              if clear_config_value wallet bip39_passphrase && rm -f "$MAKER_ENV" "$FINGERPRINT_CACHE"; then
                unset BIP39_PASSPHRASE EXPECTED_FINGERPRINT
                whiptail --title " Passphrase Deleted " --msgbox "BIP39 passphrase removed from config.toml." 8 50
              else
                whiptail --title " Config Error " --msgbox "Could not remove the BIP39 passphrase from config.toml." 8 55
              fi
            fi
            ;;

          EDIT)
            nano "$CONFIG_FILE"
            clear
            ;;

          BACKUP)
            TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
            BACKUP_FILE="${CONFIG_FILE}.backup.${TIMESTAMP}"
            if cp "$CONFIG_FILE" "$BACKUP_FILE"; then
              whiptail --title " Config Backup " --msgbox \
                "Config backed up to:\n\n$(basename "$BACKUP_FILE")\n\n(in ~/.joinmarket-ng/)" 10 60
            else
              whiptail --title " Error " --msgbox "Failed to create backup." 8 40
            fi
            ;;

          REST)
            # Use bash glob instead of find for portability (BusyBox compatibility)
            BACKUPS=
            for _f in "${DATA_DIR}"/config.toml.backup.*; do
              [ -f "$_f" ] || continue
              bf=$(basename "$_f")
              BACKUPS="${BACKUPS}${bf}"$'\n'
            done
            BACKUPS=$(echo "$BACKUPS" | sort -r)

            if [ -z "$BACKUPS" ]; then
              whiptail --title " Restore Config " --msgbox "No backups found." 8 40
              continue
            fi

            MENU_ITEMS=()
            while IFS= read -r bf; do
              [ -z "$bf" ] && continue
              MENU_ITEMS+=("$bf" "$bf")
            done <<< "$BACKUPS"

            SELECTED=$(whiptail --title " Restore Config " --notags \
              --menu "Select backup to restore:\n\nWARNING: This will overwrite current config!" \
              20 70 8 \
              "${MENU_ITEMS[@]}" 3>&1 1>&2 2>&3) || continue

            if ! whiptail --title " Confirm Restore " --yesno \
              "Restore this backup?\n\n${SELECTED}\n\nWARNING: This will OVERWRITE your current config.toml!" \
              12 70 --defaultno 3>&1 1>&2 2>&3; then
              continue
            fi

            if cp "${DATA_DIR}/${SELECTED}" "$CONFIG_FILE"; then
              whiptail --title " Config Restored " --msgbox \
                "Config restored from:\n${SELECTED}" 9 50
            else
              whiptail --title " Error " --msgbox "Failed to restore config." 8 40
            fi
            ;;

          BACK|"")
            break
            ;;
        esac
      done
      clear
      ;;

    U)
      clear
                        echo "=== Update JoinMarket-NG ==="
                        echo ""
                        echo "Preparing update information..."
                        echo ""
      # Update flow (issue #451): resolve current, latest-stable and
      # latest-main identifiers up front so the menu can display concrete
      # versions/commits instead of generic labels.
      # Gather current version/commit from the installed package. We don't
      # rely on ${VENV_BIN}/python here: on standalone (pip -e) installs the
      # user's venv may not live at the hard-coded VENV_BIN path. The
      # activation above already puts the right interpreter on PATH.
      CURRENT_VERSION=$(python3 -c "from jmcore.version import get_version; print(get_version())" 2>/dev/null || echo "unknown")
      CURRENT_COMMIT=$(python3 -c "from jmcore.version import get_commit_hash; h=get_commit_hash(); print(h or '')" 2>/dev/null || echo "")
      CURRENT_REF=$(python3 -c "from jmcore.version import get_build_ref; r=get_build_ref(); print(r or '')" 2>/dev/null || echo "")

      # Current label distinguishes a stable release ("v0.27.0 (abc1234)")
      # from a development build ("main / abc1234"): if we have a commit
      # but the build ref is anything other than a release tag matching
      # the version, we report it as a dev build. The display matches
      # what L3ftBlank reported in #451.
      IS_DEV_BUILD=0
      if [ -n "$CURRENT_REF" ] && [ "$CURRENT_REF" != "v${CURRENT_VERSION}" ] && [ "$CURRENT_REF" != "${CURRENT_VERSION}" ]; then
          IS_DEV_BUILD=1
      fi
      if [ "$IS_DEV_BUILD" = "1" ] && [ -n "$CURRENT_COMMIT" ]; then
          CURRENT_LABEL="${CURRENT_REF} / ${CURRENT_COMMIT}"
      elif [ -n "$CURRENT_COMMIT" ]; then
          CURRENT_LABEL="v${CURRENT_VERSION} (${CURRENT_COMMIT})"
      else
          CURRENT_LABEL="v${CURRENT_VERSION}"
      fi

      # Best-effort network lookups, run in PARALLEL so a slow response on
      # one endpoint doesn't stack on top of the other. Short timeouts
      # keep the blackout between the main menu and the update menu to a
      # minimum; any failure falls back to "unknown".
      LATEST_STABLE_FILE=$(mktemp)
      LATEST_MAIN_FILE=$(mktemp)
      (
          curl -fsSL --max-time 3 \
              "https://api.github.com/repos/joinmarket-ng/joinmarket-ng/releases/latest" 2>/dev/null \
              | grep -m1 '"tag_name"' \
              | sed -E 's/.*"tag_name"[^"]*"([^"]+)".*/\1/' \
              > "$LATEST_STABLE_FILE" 2>/dev/null || true
      ) &
      STABLE_PID=$!
      (
          curl -fsSL --max-time 3 \
              "https://api.github.com/repos/joinmarket-ng/joinmarket-ng/commits/main" 2>/dev/null \
              | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
    sha = payload.get("sha")
    if isinstance(sha, str):
        print(sha[:7])
except (json.JSONDecodeError, AttributeError):
    pass
' > "$LATEST_MAIN_FILE" 2>/dev/null || true
      ) &
      MAIN_PID=$!
      wait "$STABLE_PID" "$MAIN_PID" 2>/dev/null || true
      LATEST_STABLE=$(tr -d '[:space:]' < "$LATEST_STABLE_FILE" 2>/dev/null || true)
      LATEST_MAIN=$(tr -d '[:space:]' < "$LATEST_MAIN_FILE" 2>/dev/null || true)
      rm -f "$LATEST_STABLE_FILE" "$LATEST_MAIN_FILE"
      [ -z "$LATEST_STABLE" ] && LATEST_STABLE="unknown"
      [ -z "$LATEST_MAIN" ] && LATEST_MAIN="unknown"

      # Outer loop so "Cancel" on the confirm dialog returns to this menu
      # instead of the top-level menu (#451 point 6).
      while true; do
        check_stale_wallet
        UCHOICE=$(whiptail --title " Update JoinMarket-NG (current: ${CURRENT_LABEL}) " \
            --menu "\n$WALLET_INFO | Maker Bot: $MAKER_STATUS\n\nChoose update channel:" \
            16 70 4 \
            "STABLE"  "Latest stable release (v${LATEST_STABLE})" \
            "DEV"     "Latest main commit (${LATEST_MAIN})" \
            "VERSION" "Install a specific version" \
            "BACK"    "Return to main menu" 3>&1 1>&2 2>&3) || break
        TARGET_LABEL=""
        case $UCHOICE in
          STABLE)
            UPDATE_ARGS=""
            TARGET_LABEL="v${LATEST_STABLE}"
            ;;
          DEV)
            UPDATE_ARGS="--dev"
            TARGET_LABEL="main (${LATEST_MAIN})"
            ;;
          VERSION)
            TARGET_VERSION=$(whiptail --title " Specific Version " \
                --inputbox "Enter version number (e.g. 0.27.0):" 9 50 "" 3>&1 1>&2 2>&3) || { clear; continue; }
            [ -z "$TARGET_VERSION" ] && { clear; continue; }
            UPDATE_ARGS="--version $TARGET_VERSION"
            TARGET_LABEL="v${TARGET_VERSION#v}"
            ;;
          BACK)
            clear
            break
            ;;
          *)
            clear
            continue
            ;;
        esac

        # Warn if the target matches what's already installed (#451 point 5).
        # We compare on commit when both sides know it; otherwise we fall
        # back to the version string. A user on a dev build (commit known,
        # ref != stable tag) selecting STABLE is NOT considered up-to-date
        # even if the version number matches the latest release, because
        # the stable build would replace their dev commit.
        ALREADY_CURRENT=0
        case $UCHOICE in
          STABLE|VERSION)
            if [ "$IS_DEV_BUILD" = "1" ]; then
                ALREADY_CURRENT=0
            elif [ "$TARGET_LABEL" = "v${CURRENT_VERSION}" ]; then
                ALREADY_CURRENT=1
            fi
            ;;
          DEV)
            if [ -n "$CURRENT_COMMIT" ] && [ "$LATEST_MAIN" = "$CURRENT_COMMIT" ]; then
                ALREADY_CURRENT=1
            fi
            ;;
        esac

        if [ "$ALREADY_CURRENT" = "1" ]; then
            if ! whiptail --title " Already Up to Date " --defaultno --yesno \
                "You are already running ${TARGET_LABEL}.\n\nReinstall anyway?" \
                10 60 3>&1 1>&2 2>&3; then
                continue
            fi
        fi

        if [ "$UCHOICE" = "DEV" ]; then
            if ! whiptail --title " Unsigned Development Code " --defaultno --yesno \
                "main is development code. It is not a signed release.\n\nNo release signature will be checked. main may change at any time and may be unstable.\n\nInstall this unsigned development code?" \
                14 64 3>&1 1>&2 2>&3; then
                continue
            fi
        fi

        # Warn if maker bot is running
        if [ "$MAKER_STATUS" = "RUNNING" ]; then
            if ! whiptail --title " Warning " --yesno \
                "The Maker Bot is currently running.\n\nIt will be stopped during the update and must be restarted manually afterwards.\n\nContinue?" 12 60 3>&1 1>&2 2>&3; then
                continue
            fi
        fi

        # Confirm dialog -- show both current and target (#451 point 4).
        if ! whiptail --title " Confirm Update " --yesno \
            "Update JoinMarket-NG?\n\nCurrent:  ${CURRENT_LABEL}\nTarget:   ${TARGET_LABEL}\n\nThe TUI will close during the update.\nRestart it afterwards with: jm-ng" \
            14 64 3>&1 1>&2 2>&3; then
            # Cancel returns to the update menu (#451 point 6).
            continue
        fi

        clear

        # Save both output streams in a private log for review without scrollback.
        UPDATE_LOG=$(mktemp "$LOG_DIR/update.log.XXXXXX") || {
            echo "ERROR: Cannot create an update log in $LOG_DIR."
            pause
            continue
        }
        (
            if [ "$RASPIBLITZ" = "1" ]; then
                if [ "$UCHOICE" = "VERSION" ]; then
                    sudo "$BONUS_SCRIPT" update "$TARGET_VERSION"
                elif [ "$UCHOICE" = "DEV" ]; then
                    sudo "$BONUS_SCRIPT" update main
                else
                    sudo "$BONUS_SCRIPT" update
                fi
                UPDATE_RC=$?
            else
                # Standalone: prefer the locally saved installer, which
                # authenticates its own replacement against embedded GPG
                # trust anchors before applying any update.
                TRUSTED_INSTALLER="$DATA_DIR/install.sh"
                if [ -f "$TRUSTED_INSTALLER" ]; then
                    echo "Running trusted installer copy..."
                    bash "$TRUSTED_INSTALLER" --update $UPDATE_ARGS -y
                    UPDATE_RC=$?
                else
                    # First run on an installation that predates the trusted
                    # copy: bootstrap once over HTTPS, like the initial install.
                    echo "No trusted installer copy found; downloading installer..."
                    INSTALL_SCRIPT=$(mktemp)
                    curl -sSL "https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh" -o "$INSTALL_SCRIPT"
                    bash "$INSTALL_SCRIPT" --update $UPDATE_ARGS -y
                    UPDATE_RC=$?
                    rm -f "$INSTALL_SCRIPT"
                fi
            fi
            exit "$UPDATE_RC"
        ) 2>&1 | tee "$UPDATE_LOG"
        UPDATE_RC=${PIPESTATUS[0]}

        {
            echo ""
            if [ "$UPDATE_RC" -eq 0 ]; then
                echo "Update complete. Please restart the TUI: jm-ng"
            else
                echo "ERROR: Update failed (exit code ${UPDATE_RC})."
                echo "Review the output above for details."
                echo "The previous installation is unchanged."
            fi
        } | tee -a "$UPDATE_LOG"
        review_update_output "$UPDATE_LOG"
        if [ "$UPDATE_RC" -ne 0 ]; then
            continue
        fi
        exit 0
      done
      clear
      ;;

    I)
      whiptail --title " JoinMarket-NG Info " --msgbox "\
JoinMarket-NG - Next Generation CoinJoin

GitHub: https://github.com/joinmarket-ng/joinmarket-ng
Documentation: https://joinmarket-ng.github.io/joinmarket-ng/

Config: $CONFIG_FILE
Data:   $DATA_DIR
Logs:   $LOG_DIR

CLI tools (from venv):
  jm-wallet generate               - Create new wallet
  jm-wallet import                 - Import from seed
  jm-wallet validate               - Validate a seed phrase
  jm-wallet info                   - Show balance by mixdepth
  jm-wallet history                - CoinJoin history
  jm-wallet send                   - Send bitcoin
  jm-wallet freeze                 - Freeze/unfreeze UTXOs
  jm-wallet list-bonds             - List Fidelity Bonds
  jm-wallet generate-bond-address  - Create FB address
  jm-maker start                   - Maker bot (earn fees)
  jm-taker coinjoin                - Run a CoinJoin

Maker service (as admin):
  sudo systemctl start joinmarket-ng-maker
  sudo systemctl stop joinmarket-ng-maker
  sudo journalctl -u joinmarket-ng-maker -f" 24 66
      ;;

    B)
     exit_jm_ng menu
     ;;

    X)
     exit_jm_ng shell
     ;;

  esac

done

# Cancel pressed or loop ended
exit_jm_ng
