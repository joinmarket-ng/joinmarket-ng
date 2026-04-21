#!/bin/bash
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

# ---- Paths ------------------------------------------------------------------
DATA_DIR="${HOME_JM}/.joinmarket-ng"
CONFIG_FILE="${DATA_DIR}/config.toml"
LOG_DIR="${DATA_DIR}/logs"
MAKER_ENV="${DATA_DIR}/.maker.env"

# ---- Defaults for send/coinjoin parameters ----------------------------------
DEFAULT_AMOUNT="0"
DEFAULT_MIXDEPTH="0"
DEFAULT_FEE_RATE=""
DEFAULT_DESTINATION=""
# Counterparty default: read from config.toml [taker] section, fall back to 10
DEFAULT_COUNTERPARTIES=$(python3 - "$CONFIG_FILE" <<'PYEOF' 2>/dev/null
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

# ---- Activate virtual environment -------------------------------------------
# Skip if CLI tools are already available (e.g. pip-installed entry points).
if ! command -v jm-wallet &>/dev/null; then
    if [ -f "$VENV_BIN/activate" ]; then
        source "$VENV_BIN/activate"
    else
        echo "ERROR: jm-wallet not found in PATH and no venv at $VENV_BIN"
        exit 1
    fi
fi
# Ensure ~/.local/bin is in PATH (fallback for pip console scripts)
export PATH="${HOME_JM}/.local/bin:$PATH"

# =============================================================================
# Helpers
# =============================================================================

# Helper: Pause
pause() {
  echo ""
  read -p "Press [Enter] key to continue..." fakeEnterKey
}

# Helper: Get configured mnemonic file from config.toml
get_mnemonic_file() {
    local val
    val=$(grep '^mnemonic_file[[:space:]]*=' "$CONFIG_FILE" 2>/dev/null | head -1 | sed 's/^mnemonic_file[[:space:]]*=[[:space:]]*//' | tr -d '"')
    echo "$val"
}

# Helper: Comment out (clear) a value in config.toml
clear_config_value() {
    local key=$1
    if grep -q "^${key}[[:space:]]*=" "$CONFIG_FILE"; then
        sed -i "s|^${key}[[:space:]]*=.*|# ${key} =|" "$CONFIG_FILE"
    fi
}

# Helper: Set a value in config.toml (uncomment if needed)
# Values are escaped for safe use in sed replacement strings.
set_config_value() {
    local key=$1
    local value=$2
    local quote=$3 # "true" to wrap in quotes

    if [ "$quote" == "true" ]; then
        value="\"${value}\""
    fi

    # Escape sed metacharacters in the value (backslash, ampersand, pipe delimiter)
    local sed_value
    sed_value=$(printf '%s' "$value" | sed -e 's/[&\\/|]/\\&/g')

    if grep -q "^${key}[[:space:]]*=" "$CONFIG_FILE"; then
        sed -i "s|^${key}[[:space:]]*=.*|${key} = ${sed_value}|" "$CONFIG_FILE"
    elif grep -q "^#[[:space:]]*${key}[[:space:]]*=" "$CONFIG_FILE"; then
        sed -i "s|^#[[:space:]]*${key}[[:space:]]*=.*|${key} = ${sed_value}|" "$CONFIG_FILE"
    else
        echo "# Warning: Could not find key '${key}' in config"
    fi
}

# Helper: List .mnemonic files in wallets dir
list_wallets() {
    find "$DATA_DIR/wallets" -maxdepth 1 -name '*.mnemonic' -type f -printf '%f\n' 2>/dev/null
}

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

    cat <<EOF

From wallet:     $(basename "$CURRENT_WALLET")
Source Mixdepth: ${SEND_MIXDEPTH:-(default: ${DEFAULT_MIXDEPTH})}
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
        set_config_value "mnemonic_password" "$password" "true"
    fi
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

    # Security warning first (#453). Make the trade-off explicit.
    if ! whiptail --title " Security Warning " \
        --yesno "Storing the wallet password in config.toml saves it in PLAIN TEXT.\n\nAnyone with read access to:\n  $CONFIG_FILE\ncan decrypt your wallet. This effectively defeats the\nwallet encryption.\n\nOnly store the password if the maker bot needs to start\nunattended and you trust the security of this machine.\n\nContinue and store the password?" \
        18 70 --defaultno 3>&1 1>&2 2>&3; then
        return 1
    fi

    while [ $attempts -lt $max_attempts ]; do
        pwd_store=$(whiptail --title " Wallet Password " \
            --passwordbox "Enter the wallet encryption password for:\n$(basename "$wallet_path")" \
            10 60 3>&1 1>&2 2>&3)
        local rc=$?
        if [ $rc -ne 0 ]; then
            # User cancelled
            unset pwd_store
            return 1
        fi
        if [ -z "$pwd_store" ]; then
            whiptail --title " Error " --msgbox "Password cannot be empty." 8 40
            attempts=$((attempts + 1))
            continue
        fi
        if verify_wallet_password "$wallet_path" "$pwd_store"; then
            store_password "${pwd_store}"
            unset pwd_store
            whiptail --title " Password Stored " \
                --msgbox "Password verified and saved to config.toml." 8 55
            return 0
        fi
        attempts=$((attempts + 1))
        local remaining=$((max_attempts - attempts))
        if [ $remaining -gt 0 ]; then
            whiptail --title " Password Mismatch " \
                --msgbox "The password does not decrypt the wallet.\n\n${remaining} attempt(s) remaining." \
                10 60
        else
            whiptail --title " Password Mismatch " \
                --msgbox "The password does not decrypt the wallet.\n\nToo many attempts. Password was NOT saved." \
                10 60
        fi
    done
    unset pwd_store
    return 1
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
# Usage: post_wallet_create "/path/to/wallet.mnemonic"
post_wallet_create() {
    local wallet_path="$1"
    local set_active=0

    # Ask to set as active wallet (default: Yes)
    if whiptail --title " Active Wallet " \
        --yesno "Set this wallet as the active wallet in config?\n\n$(basename "$wallet_path")" \
        10 60 3>&1 1>&2 2>&3; then
        set_config_value "mnemonic_file" "$wallet_path" "true"
        # Clear any previously stored password -- it belongs to the old wallet.
        clear_config_value "mnemonic_password"
        set_active=1
        echo "Active wallet updated in config.toml"
    fi

    # Only offer to store the password when the new wallet is now the active
    # wallet in config. Storing a password for a non-active wallet would
    # guarantee a mismatch (issue #455).
    if [ $set_active -ne 1 ]; then
        return 0
    fi

    # Ask whether to store the encryption password
    if whiptail --title " Store Password " \
        --yesno "Store the wallet password in config.toml?\n\nThis lets all commands (including the maker) work without\nprompting. If you choose No, the maker will ask each time." \
        12 64 --defaultno 3>&1 1>&2 2>&3; then
        prompt_and_store_password "$wallet_path" || \
            echo "Password not stored."
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

# Helper: Interactive wallet picker + (optional) password handling for the
# maker START flow. When multiple wallets exist the user is asked to choose
# one and the selection is written to config.toml, replacing any stale
# password entry. This prevents "Decryption failed" errors where the user
# had no way to tell which wallet the password prompt referred to
# (issue #454).
#
# Usage: maker_prepare_wallet
# Returns 0 if the caller should proceed with maker start, non-zero to abort.
maker_prepare_wallet() {
    local wallets
    wallets=$(list_wallets)

    if [ -z "$wallets" ]; then
        whiptail --title " Error " \
            --msgbox "No wallet files found in $DATA_DIR/wallets/\nCreate or import a wallet first (W -> NEW or IMP)." \
            9 60
        return 1
    fi

    # Count wallets without relying on subshells preserving state.
    local wallet_count
    wallet_count=$(printf '%s\n' "$wallets" | sed '/^$/d' | wc -l)

    # Single wallet: just make sure it's the active one. No prompting needed
    # beyond what jm-maker itself already does.
    if [ "$wallet_count" -eq 1 ]; then
        local only_wallet
        only_wallet=$(printf '%s\n' "$wallets" | sed -n '1p')
        local only_path="$DATA_DIR/wallets/$only_wallet"
        if [ "$CURRENT_WALLET" != "$only_path" ]; then
            set_config_value "mnemonic_file" "$only_path" "true"
            clear_config_value "mnemonic_password"
            CURRENT_WALLET="$only_path"
        fi
        return 0
    fi

    # Multiple wallets: ask the user to explicitly pick one.
    local menu_items=()
    while IFS= read -r wf; do
        [ -z "$wf" ] && continue
        menu_items+=("$wf" "$wf")
    done <<< "$wallets"

    local current_display
    current_display=$(basename "${CURRENT_WALLET:-}" 2>/dev/null)
    [ -z "$current_display" ] && current_display="(none)"

    local selected
    selected=$(whiptail --title " Start Maker -- Select Wallet " --notags \
        --menu "Current active: ${current_display}\n\nChoose the wallet to use for the maker bot:" \
        18 66 6 \
        "${menu_items[@]}" 3>&1 1>&2 2>&3) || return 1

    local selected_path="$DATA_DIR/wallets/$selected"
    if [ ! -f "$selected_path" ]; then
        whiptail --title " Error " --msgbox "File not found: $selected_path" 8 55
        return 1
    fi

    # If the user picked a different wallet than the one in config, update
    # config and drop the stale password -- this is the root cause of the
    # #455 mismatch scenarios.
    if [ "$CURRENT_WALLET" != "$selected_path" ]; then
        set_config_value "mnemonic_file" "$selected_path" "true"
        clear_config_value "mnemonic_password"
        CURRENT_WALLET="$selected_path"
    fi
    return 0
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

# =============================================================================
# Main Loop
# =============================================================================

while true; do

  # Get Maker Service Status
  if pgrep -f "jm-maker" > /dev/null 2>&1; then
    MAKER_STATUS="RUNNING"
  else
    MAKER_STATUS="STOPPED"
  fi

  # Check if a wallet is configured
  CURRENT_WALLET=$(get_mnemonic_file)
  if [ -n "$CURRENT_WALLET" ]; then
    WALLET_INFO="Active Wallet: $(basename "$CURRENT_WALLET")"
  else
    WALLET_INFO="Active Wallet: (none configured)"
  fi

CHOICE=$(whiptail --title " JoinMarket-NG Menu " \
    --menu "
$WALLET_INFO | Maker Bot: $MAKER_STATUS

" \
    18 64 10 \
    "S" "Send Bitcoin" \
    "W" "Wallet Management" \
    "M" "Maker Bot Control" \
    "C" "Edit Configuration" \
    "U" "Update JoinMarket-NG" \
    "I" "Info / Documentation" \
    "X" "Exit" 3>&1 1>&2 2>&3)

  exitstatus=$?
  if [ $exitstatus != 0 ]; then
    clear
    exit 0
  fi

  case $CHOICE in
    # ------------------------------------------------------------------
    # SEND BITCOIN (unified: normal tx when counterparties=0, coinjoin otherwise)
    # ------------------------------------------------------------------
    S)
      if [ -z "$CURRENT_WALLET" ]; then
          whiptail --title " Error " --msgbox "No wallet configured.\nSet up a wallet first (W -> NEW or SEL)." 9 50
          continue
      fi

      # Reset all send parameters at the start
      SEND_MIXDEPTH=""
      SEND_AMOUNT=""
      SEND_CP=""
      SEND_FEE=""
      SEND_FEE_ENTERED=""
      SEND_DEST=""

      # 1. Source mixdepth
      SEND_MIXDEPTH=$(prompt_param "Choose a mixdepth to send from" \
        "$(display_send_status "Source mixdepth (account) to send from.")" \
        "") || continue
      SEND_MIXDEPTH=$(to_int "${SEND_MIXDEPTH:-$DEFAULT_MIXDEPTH}" "$DEFAULT_MIXDEPTH")

      # 2. Amount in satoshis
      SEND_AMOUNT=$(prompt_param "Send Amount" \
        "$(display_send_status "Amount in satoshis to send.\n0 = sweep entire mixdepth (best privacy for coinjoin).")" \
        "") || continue
      SEND_AMOUNT=$(to_int "${SEND_AMOUNT:-$DEFAULT_AMOUNT}" "$DEFAULT_AMOUNT")

      # 3. Counterparties (0 = normal transaction, >0 = coinjoin)
      SEND_CP=$(prompt_param "Counterparties" \
        "$(display_send_status "Number of counterparties (makers) for CoinJoin.\n0 = normal transaction (no CoinJoin).\nRecommended for CoinJoin: 4-10.")" \
        "") || continue
      SEND_CP=$(to_int "${SEND_CP:-$DEFAULT_COUNTERPARTIES}" "$DEFAULT_COUNTERPARTIES")

      # 4. Fee rate
      SEND_FEE=$(prompt_param "Fee Rate" \
        "$(display_send_status "Fee rate in sat/vB.\nLeave blank for auto (block target in config).")" \
        "") || continue
      # Validate fee rate is numeric if provided
      if [ -n "$SEND_FEE" ] && ! [[ "$SEND_FEE" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
          whiptail --title " Error " --msgbox "Fee rate must be a numeric value in sat/vB." 8 50
          continue
      fi

      # Set flag to show "auto" instead of "(default: auto)" in next prompts
      SEND_FEE_ENTERED="1"

      # 5. Destination address (if empty: INTERNAL coinjoin to next mixdepth)
      SEND_DEST=$(prompt_param "Destination Address" \
        "$(display_send_status "Enter destination bitcoin address.\nLeave empty for INTERNAL (next mixdepth, coinjoin only).")" \
        "") || continue

      # Apply INTERNAL default for coinjoin when destination is empty
      if [ -z "$SEND_DEST" ] && [ "$SEND_CP" -gt 0 ] 2>/dev/null; then
          SEND_DEST="INTERNAL"
      elif [ -z "$SEND_DEST" ]; then
          whiptail --title " Error " --msgbox "Destination address is required for normal transactions." 8 50
          continue
      fi

      # Validate destination looks like a bitcoin address (unless INTERNAL)
      if [ "$SEND_DEST" != "INTERNAL" ]; then
          # Accept mainnet (1/3/bc1), testnet (m/n/2/tb1), signet (tb1), regtest (bcrt1)
          if ! [[ "$SEND_DEST" =~ ^[13mn2][a-km-zA-HJ-NP-Z1-9]{25,40}$ || \
                  "$SEND_DEST" =~ ^(bc|tb|bcrt)1[0-9ac-hj-np-z]{11,71}$ ]]; then
              whiptail --title " Error " --msgbox "Destination does not look like a valid Bitcoin address." 8 55
              continue
          fi
      fi

      # Determine transaction type label for summary
      if [ "$SEND_CP" -gt 0 ] 2>/dev/null; then
          TX_TYPE="CoinJoin ($SEND_CP counterparties)"
      else
          TX_TYPE="Normal transaction"
      fi

      # Fee display
      if [ -n "$SEND_FEE" ]; then
          FEE_DISPLAY="${SEND_FEE} sat/vB"
      else
          FEE_DISPLAY="auto (3-block estimate)"
      fi

      # Amount display
      if [ "$SEND_AMOUNT" = "0" ]; then
          AMOUNT_DISPLAY="0 (sweep)"
      else
          AMOUNT_DISPLAY="${SEND_AMOUNT} sats"
      fi

      # Show confirmation summary
      DEFAULT_TX_TYPE="CoinJoin ($DEFAULT_COUNTERPARTIES counterparties)"
      show_summary "Confirm Send -- $(basename "$CURRENT_WALLET")" \
        "Type|${DEFAULT_TX_TYPE}|${TX_TYPE}" \
        "Destination||${SEND_DEST}" \
        "Amount|$DEFAULT_AMOUNT (sweep)|${AMOUNT_DISPLAY}" \
        "Source mixdepth|$DEFAULT_MIXDEPTH|${SEND_MIXDEPTH}" \
        "Fee rate|auto (3-block estimate)|${FEE_DISPLAY}" || continue

      # Execute the appropriate command
      clear
      if [ "$SEND_CP" -gt 0 ] 2>/dev/null; then
          # CoinJoin via jm-taker
          echo "=== CoinJoin Send ==="
          echo ""
          echo "Wallet: $(basename "$CURRENT_WALLET")"
          echo "Counterparties: $SEND_CP"
          echo "Press Ctrl+C to abort."
          echo ""

          TAKER_ARGS=(coinjoin -a "$SEND_AMOUNT" -m "$SEND_MIXDEPTH" -d "$SEND_DEST")
          TAKER_ARGS+=(-n "$SEND_CP")
          [ -n "$SEND_FEE" ] && TAKER_ARGS+=(--fee-rate "$SEND_FEE")

          jm-taker "${TAKER_ARGS[@]}"
      else
          # Normal transaction via jm-wallet send
          echo "=== Send Bitcoin ==="
          echo ""
          echo "Wallet: $(basename "$CURRENT_WALLET")"
          echo ""

          SEND_ARGS=(send -a "$SEND_AMOUNT" -m "$SEND_MIXDEPTH")
          [ -n "$SEND_FEE" ] && SEND_ARGS+=(--fee-rate "$SEND_FEE")
          SEND_ARGS+=("$SEND_DEST")

          jm-wallet "${SEND_ARGS[@]}"
      fi
      pause
      ;;

    # ------------------------------------------------------------------
    # WALLET MANAGEMENT
    # ------------------------------------------------------------------
    W)
      # Wallet Management Submenu - loops until BACK is selected
      while true; do
        # Refresh wallet info at the START of each W submenu iteration
        CURRENT_WALLET=$(get_mnemonic_file)
        if [ -n "$CURRENT_WALLET" ]; then
          WALLET_INFO="Wallet: $(basename "$CURRENT_WALLET")"
        else
          WALLET_INFO="Wallet: (none configured)"
        fi

        WCHOICE=$(whiptail --title " Wallet Management " \
         --menu "
 $WALLET_INFO

         " 20 64 11 \
          "NEW"      "Create New Wallet (24-word seed)" \
          "IMP"      "Import Existing Wallet (from seed)" \
          "VAL"      "Validate a Seed Phrase" \
          "BAL"      "View Wallet Info / Balance" \
          "HIST"     "CoinJoin History" \
          "FREEZE"   "Freeze / Unfreeze UTXOs" \
          "SEL"      "Select Active Wallet" \
          "BACK"     "Back to Main Menu" 3>&1 1>&2 2>&3)

        # Handle ESC/Cancel - exit W submenu
        [ $? -ne 0 ] && break

        case $WCHOICE in
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

              # Ask for seed word count (default 24; 12 is also widely supported)
              WORDS_CHOICE=$(whiptail --title " Create New Wallet " --notags \
                  --menu "How many seed words should the new wallet have?" 12 55 2 \
                  "24" "24 words (recommended, 256-bit entropy)" \
                  "12" "12 words (128-bit entropy)" \
                  3>&1 1>&2 2>&3) || continue
              WORDS="${WORDS_CHOICE:-24}"

              WALLET_PATH="$DATA_DIR/wallets/${WNAME}.mnemonic"
              mkdir -p "$DATA_DIR/wallets"

              clear
              echo "=== Create New Wallet ==="
              echo ""
              echo "This will generate a new ${WORDS}-word BIP39 mnemonic."
              echo "IMPORTANT: Write down the seed words! They are your backup."
              echo ""
              echo "Generating wallet..."
              jm-wallet generate --words "$WORDS" --prompt-password -o "$WALLET_PATH"
              RESULT=$?

              if [ $RESULT -eq 0 ] && [ -f "$WALLET_PATH" ]; then
                  echo ""
                  echo "Wallet saved to: $WALLET_PATH"
                  post_wallet_create "$WALLET_PATH"
              else
                  echo "Wallet creation may have failed. Check output above."
              fi
              pause
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

              # Ask for word count
              WORDS_CHOICE=$(whiptail --title " Import Wallet " --notags \
                  --menu "How many seed words does your wallet have?" 12 50 2 \
                  "24" "24 words" \
                  "12" "12 words" \
                  3>&1 1>&2 2>&3) || break
              WORDS="${WORDS_CHOICE:-24}"

              WALLET_PATH="$DATA_DIR/wallets/${WNAME}.mnemonic"
              mkdir -p "$DATA_DIR/wallets"

              clear
              echo "=== Import Wallet from Seed ==="
              echo ""
              echo "You will be prompted to enter your BIP39 seed words."
              echo ""
              jm-wallet import --words "$WORDS" --prompt-password -o "$WALLET_PATH"
              RESULT=$?

              if [ $RESULT -eq 0 ] && [ -f "$WALLET_PATH" ]; then
                  echo ""
                  echo "Wallet imported to: $WALLET_PATH"
                  post_wallet_create "$WALLET_PATH"
              else
                  echo "Import may have failed. Check output above."
              fi
              pause
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
          # BAL - Wallet Info (with submenu)
          # --------------------------------------------------------------
          BAL)
              while true; do
                  if [ -z "$CURRENT_WALLET" ]; then
                      whiptail --title " Error " --msgbox "No wallet configured.\nUse 'Select Active Wallet' or 'Create New Wallet' first." 9 50
                      break
                  fi

                  INFO_CHOICE=$(whiptail --title " Wallet Info " \
                    --menu "Choose info display level:" \
                    16 64 3 \
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
                          echo ""
                          jm-wallet info
                          pause
                          ;;
                      EXT)
                          clear
                          echo "=== Wallet Info (Extended) ==="
                          echo ""
                          echo "Active wallet: $(basename "$CURRENT_WALLET")"
                          echo ""
                          jm-wallet info --extended
                          pause
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
              if [ -z "$CURRENT_WALLET" ]; then
                  whiptail --title " Error " --msgbox "No wallet configured.\nSet up a wallet first (W -> NEW or SEL)." 9 50
              else
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

                  clear
                  echo "=== CoinJoin History ==="
                  echo ""
                  echo "Active wallet: $(basename "$CURRENT_WALLET")"
                  echo ""
                  HIST_ARGS=()
                  [ -n "$HIST_ROLE" ]  && HIST_ARGS+=(-r "$HIST_ROLE")
                  [ -n "$HIST_LIMIT" ] && HIST_ARGS+=(-n "$HIST_LIMIT")
                  [ $HIST_SHOW_STATS -eq 0 ] && HIST_ARGS+=(-s)
                  jm-wallet history "${HIST_ARGS[@]}"
                  pause
              fi
              ;;

          # --------------------------------------------------------------
          # FREEZE - Freeze/Unfreeze UTXOs
          # --------------------------------------------------------------
          FREEZE)
              clear
              echo "=== Freeze / Unfreeze UTXOs ==="
              echo ""
              if [ -z "$CURRENT_WALLET" ]; then
                  echo "No wallet configured in config.toml (mnemonic_file is empty)."
              else
                  echo "Active wallet: $(basename "$CURRENT_WALLET")"
                  echo "Opening interactive UTXO selector. Use arrow keys to navigate,"
                  echo "Space to toggle freeze state, Enter to confirm, q to quit."
                  echo ""
                  jm-wallet freeze
              fi
              pause
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
                  --menu "Current: $(basename "$(get_mnemonic_file)" 2>/dev/null || echo '(none)')\n\nChoose a wallet:" \
                  18 64 6 \
                  "${MENU_ITEMS[@]}" 3>&1 1>&2 2>&3) || continue

              if [ -f "$DATA_DIR/wallets/$WNAME" ]; then
                  set_config_value "mnemonic_file" "$DATA_DIR/wallets/$WNAME" "true"
                  # Clear stored password to prevent mismatch with the new wallet
                  clear_config_value "mnemonic_password"

                  # Offer to store the matching password for the freshly
                  # selected wallet so the maker can restart unattended
                  # (issue #455 Case 3: previously the old password was
                  # cleared with no way to record the new one, leaving a
                  # wallet_file/password mismatch until the user edited
                  # config.toml by hand).
                  if whiptail --title " Store Password " \
                      --yesno "Active wallet set to: $WNAME\n\nStore this wallet's password in config.toml?\nThis lets the maker start without prompting.\nChoose No to be asked for the password on each use." \
                      12 64 --defaultno 3>&1 1>&2 2>&3; then
                      prompt_and_store_password "$DATA_DIR/wallets/$WNAME" || \
                          echo "Password not stored."
                      whiptail --title " Wallet Selected " --msgbox "Active wallet set to: $WNAME\n\nRestart the maker service for changes to take effect." 10 60
                  else
                      whiptail --title " Wallet Selected " --msgbox "Active wallet set to: $WNAME\n\nStored password cleared; you will be prompted\nfor the password on next use.\n\nRestart the maker service for changes to take effect." 12 60
                  fi
              else
                  whiptail --title " Error " --msgbox "File not found: $DATA_DIR/wallets/$WNAME" 8 55
              fi
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
      MCHOICE=$(whiptail --title " Maker Bot (${MAKER_STATUS}) " --menu "Choose option:" 18 64 8 \
        "START"   "Start Maker Bot" \
        "STOP"    "Stop Maker Bot" \
        "RESTART" "Restart Maker Bot" \
        "BONDS"   "Fidelity Bond Management" \
        "LOG"     "Follow Maker Logs (Ctrl+C to stop)" \
        "STATUS"  "Show Service Status" \
        "BACK"    "Back to Main Menu" 3>&1 1>&2 2>&3)

      case $MCHOICE in
          START)
              clear
              if ! maker_prepare_wallet; then
                  echo "Maker start cancelled."
              else
                  maker_start
                  sleep 2
                  echo ""
                  echo "Service status:"
                  maker_status
              fi
              pause
              ;;
          STOP)
              clear
              maker_stop
              pause
              ;;
          RESTART)
              clear
              if ! maker_prepare_wallet; then
                  echo "Maker restart cancelled."
              else
                  maker_stop
                  maker_start
                  sleep 2
                  echo ""
                  echo "Service status:"
                  maker_status
              fi
              pause
              ;;
          LOG)
              clear
              echo "=== Maker Logs ==="
              echo "Press Ctrl+C to stop following."
              echo ""
              LOG_FILE="$LOG_DIR/maker.log"
              if [ -r "$LOG_FILE" ]; then
                  tail -n 50 -f "$LOG_FILE"
              else
                  echo "No log file found at $LOG_FILE (maker may not have run yet)."
                  echo "Trying journalctl..."
                  maker_status
              fi
              pause
              ;;
          STATUS)
              clear
              echo "=== Maker Service Status ==="
              echo ""
              maker_status
              pause
              ;;
          BONDS)
              # Fidelity bond submenu
              while true; do
                BCHOICE=$(whiptail --title " Fidelity Bonds " \
                  --menu "Fidelity bonds lock coins until a date to boost maker reputation.\nExpired bonds appear in wallet balance and are spendable." \
                  16 72 4 \
                  "LIST"   "List existing fidelity bonds" \
                  "CREATE" "Generate a new bond address (lock coins)" \
                  "BACK"   "Back to Maker Menu" 3>&1 1>&2 2>&3)
                [ $? -ne 0 ] && break
                case $BCHOICE in
                    LIST)
                        clear
                        echo "=== Fidelity Bonds ==="
                        echo ""
                        if [ -z "$CURRENT_WALLET" ]; then
                            echo "ERROR: No wallet configured. Set up a wallet first (W -> SEL or NEW)."
                        else
                            echo "Scanning for fidelity bonds (this may take a moment)..."
                            echo ""
                            jm-wallet list-bonds
                        fi
                        pause
                        ;;
                    CREATE)
                        if [ -z "$CURRENT_WALLET" ]; then
                            whiptail --title " Error " --msgbox "No wallet configured.\nSet up a wallet first (W -> NEW or SEL)." 9 50
                            continue
                        fi

                        # Locktime month (required)
                        LOCKDATE=$(prompt_param "Fidelity Bond Locktime" \
                          "Enter locktime as YYYY-MM (must be a future month, e.g. 2027-06).\nCoins are NOT spendable until this date." \
                          "") || continue
                        if [ -z "$LOCKDATE" ]; then
                            whiptail --title " Error " --msgbox "No locktime entered." 8 40
                            continue
                        fi

                        # Derivation index (default 0)
                        BOND_INDEX=$(prompt_param "Bond Index" \
                          "Derivation index (0 for first bond, 1 for second, etc.)." \
                          "0") || continue
                        BOND_INDEX=$(to_int "${BOND_INDEX}" "0")

                        # Confirmation summary
                        show_summary "Confirm Fidelity Bond -- $(basename "$CURRENT_WALLET")" \
                          "Locktime|<required>|${LOCKDATE}" \
                          "Derivation index|0|${BOND_INDEX}" || continue

                        clear
                        echo "=== Generating Bond Address ==="
                        echo ""
                        jm-wallet generate-bond-address \
                          --locktime-date "${LOCKDATE}" \
                          --index "${BOND_INDEX}"
                        echo ""
                        echo "Send coins to the address above to create the fidelity bond."
                        echo "Funds will be locked until the locktime expires."
                        pause
                        ;;
                    BACK|"")
                        break
                        ;;
                esac
              done
              ;;
      esac
      ;;

    U)
      # Update flow (issue #451): resolve current, latest-stable and
      # latest-main identifiers up front so the menu can display concrete
      # versions/commits instead of generic labels.
      # Gather current version/commit from the installed package. We don't
      # rely on ${VENV_BIN}/python here: on standalone (pip -e) installs the
      # user's venv may not live at the hard-coded VENV_BIN path. The
      # activation above already puts the right interpreter on PATH.
      CURRENT_VERSION=$(python3 -c "from jmcore.version import get_version; print(get_version())" 2>/dev/null || echo "unknown")
      CURRENT_COMMIT=$(python3 -c "from jmcore.version import get_commit_hash; h=get_commit_hash(); print(h or '')" 2>/dev/null || echo "")

      # Current label: "vX.Y.Z" plus short commit when we have one.
      if [ -n "$CURRENT_COMMIT" ]; then
          CURRENT_LABEL="v${CURRENT_VERSION} (${CURRENT_COMMIT})"
      else
          CURRENT_LABEL="v${CURRENT_VERSION}"
      fi

      # Best-effort network lookups. Short timeouts so a flaky connection
      # can't wedge the menu; fall back to "unknown" when anything fails.
      LATEST_STABLE=$(curl -fsSL --max-time 5 \
          "https://api.github.com/repos/joinmarket-ng/joinmarket-ng/releases/latest" 2>/dev/null | \
          grep -m1 '"tag_name"' | sed -E 's/.*"tag_name"[^"]*"([^"]+)".*/\1/' || true)
      [ -z "$LATEST_STABLE" ] && LATEST_STABLE="unknown"

      LATEST_MAIN=$(git ls-remote --quiet \
          "https://github.com/joinmarket-ng/joinmarket-ng.git" HEAD 2>/dev/null | \
          cut -c1-7 || true)
      [ -z "$LATEST_MAIN" ] && LATEST_MAIN="unknown"

      # Outer loop so "Cancel" on the confirm dialog returns to this menu
      # instead of the top-level menu (#451 point 6).
      while true; do
        UCHOICE=$(whiptail --title " Update JoinMarket-NG (current: ${CURRENT_LABEL}) " \
            --menu "Choose update channel:" 16 70 5 \
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
                --inputbox "Enter version number (e.g. 0.27.0):" 9 50 "" 3>&1 1>&2 2>&3) || continue
            [ -z "$TARGET_VERSION" ] && continue
            UPDATE_ARGS="--version $TARGET_VERSION"
            TARGET_LABEL="v${TARGET_VERSION#v}"
            ;;
          BACK)
            break
            ;;
          *)
            continue
            ;;
        esac

        # Warn if the target matches what's already installed (#451 point 5).
        # The current identifier is "vX.Y.Z" for stable and the short
        # commit for dev; compare against the matching component.
        ALREADY_CURRENT=0
        case $UCHOICE in
          STABLE|VERSION)
            if [ "$TARGET_LABEL" = "v${CURRENT_VERSION}" ]; then
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

        # Warn if maker bot is running
        if [ "$MAKER_STATUS" = "RUNNING" ]; then
            if ! whiptail --title " Warning " --yesno \
                "The Maker Bot is currently running.\n\nIt will be stopped during the update and must be restarted manually afterwards.\n\nContinue?" 12 60 3>&1 1>&2 2>&3; then
                continue
            fi
        fi

        # Confirm dialog -- show both current and target (#451 point 4).
        if ! whiptail --title " Confirm Update " --yesno \
            "Update JoinMarket-NG?\n\nCurrent:\t${CURRENT_LABEL}\nTarget:\t\t${TARGET_LABEL}\n\nThe TUI will close during the update.\nRestart it afterwards with: jm-ng" \
            14 64 3>&1 1>&2 2>&3; then
            # Cancel returns to the update menu (#451 point 6).
            continue
        fi

        clear

        if [ "$RASPIBLITZ" = "1" ]; then
            if [ "$UCHOICE" = "VERSION" ]; then
                sudo "$BONUS_SCRIPT" update "$TARGET_VERSION"
            elif [ "$UCHOICE" = "DEV" ]; then
                sudo "$BONUS_SCRIPT" update main
            else
                sudo "$BONUS_SCRIPT" update
            fi
        else
            # Standalone: download and run install.sh --update
            echo "Downloading latest installer..."
            INSTALL_SCRIPT=$(mktemp)
            curl -sSL "https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh" -o "$INSTALL_SCRIPT"
            bash "$INSTALL_SCRIPT" --update $UPDATE_ARGS -y
            rm -f "$INSTALL_SCRIPT"
        fi

        echo ""
        echo "Update complete. Please restart the TUI: jm-ng"
        exit 0
      done
      ;;

    C)
      nano "$CONFIG_FILE"
      ;;

    I)
      whiptail --title " JoinMarket-NG Info " --msgbox "\
JoinMarket-NG - Next Generation CoinJoin

Docs: https://github.com/joinmarket-ng/joinmarket-ng

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
  jm-wallet list-bonds             - List fidelity bonds
  jm-wallet generate-bond-address  - Create FB address
  jm-maker start                   - Maker bot (earn fees)
  jm-taker coinjoin                - Run a CoinJoin

Maker service (as admin):
  sudo systemctl start joinmarket-ng-maker
  sudo systemctl stop joinmarket-ng-maker
  sudo journalctl -u joinmarket-ng-maker -f" 24 66
      ;;

    X)
      clear
      exit 0
      ;;
  esac

done
