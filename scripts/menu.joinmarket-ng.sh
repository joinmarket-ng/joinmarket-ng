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
    18 64 9 \
    "S" "Send Bitcoin" \
    "W" "Wallet Management" \
    "M" "Maker Bot Control" \
    "C" "Edit Configuration" \
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
              clear
              echo "=== Create New Wallet ==="
              echo ""
              echo "This will generate a new 24-word BIP39 mnemonic."
              echo "IMPORTANT: Write down the seed words! They are your backup."
              echo ""
              read -p "Enter wallet name (default: default): " WNAME
              WNAME=${WNAME:-default}
              # Strip extension if provided, we add .mnemonic
              WNAME="${WNAME%.mnemonic}"
              # Validate: only safe characters, no path separators
              if [[ ! "$WNAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
                  echo "Invalid wallet name. Use only letters, numbers, dot, underscore, and hyphen."
                  pause
                  continue
              fi

              WALLET_PATH="$DATA_DIR/wallets/${WNAME}.mnemonic"
              mkdir -p "$DATA_DIR/wallets"

              echo ""
              echo "Generating wallet..."
              jm-wallet generate --prompt-password -o "$WALLET_PATH"
              RESULT=$?

              if [ $RESULT -eq 0 ] && [ -f "$WALLET_PATH" ]; then
                  echo ""
                  echo "Wallet saved to: $WALLET_PATH"
                  # Ask to set as active wallet
                  read -p "Set as active wallet in config? (Y/n): " SET_ACTIVE
                  SET_ACTIVE=${SET_ACTIVE:-Y}
                  if [[ "$SET_ACTIVE" =~ ^[Yy] ]]; then
                      set_config_value "mnemonic_file" "$WALLET_PATH" "true"
                      echo "Active wallet updated in config.toml"
                  fi
                  # Ask whether to store the encryption password in config.toml
                  echo ""
                  echo "You can store the wallet password in config.toml so all"
                  echo "commands (including the maker) work without prompting."
                  echo "If you choose No, the maker will ask for the password each time."
                  read -p "Store wallet password in config.toml? (y/N): " STORE_PWD
                  if [[ "$STORE_PWD" =~ ^[Yy] ]]; then
                      read -r -s -p "Enter the wallet encryption password: " PWD_STORE
                      echo ""
                      store_password "${PWD_STORE}"
                      unset PWD_STORE
                      echo "Password stored in config.toml."
                  fi
              else
                  echo "Wallet creation may have failed. Check output above."
              fi
              pause
              ;;

          # --------------------------------------------------------------
          # IMP - Import Wallet
          # --------------------------------------------------------------
          IMP)
              clear
              echo "=== Import Wallet from Seed ==="
              echo ""
              echo "You will be prompted to enter your BIP39 seed words."
              echo ""
              read -p "Enter wallet name (default: imported): " WNAME
              WNAME=${WNAME:-imported}
              WNAME="${WNAME%.mnemonic}"
              # Validate: only safe characters, no path separators
              if [[ ! "$WNAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
                  echo "Invalid wallet name. Use only letters, numbers, dot, underscore, and hyphen."
                  pause
                  continue
              fi

              # Ask for word count
              WORDS_CHOICE=$(whiptail --title " Import Wallet " \
                  --menu "How many seed words does your wallet have?" 12 50 2 \
                  "24" "24 words" \
                  "12" "12 words" \
                  3>&1 1>&2 2>&3) || break
              WORDS="${WORDS_CHOICE:-24}"

              WALLET_PATH="$DATA_DIR/wallets/${WNAME}.mnemonic"
              mkdir -p "$DATA_DIR/wallets"

              jm-wallet import --words "$WORDS" --prompt-password -o "$WALLET_PATH"
              RESULT=$?

              if [ $RESULT -eq 0 ] && [ -f "$WALLET_PATH" ]; then
                  echo ""
                  echo "Wallet imported to: $WALLET_PATH"
                  read -p "Set as active wallet in config? (Y/n): " SET_ACTIVE
                  SET_ACTIVE=${SET_ACTIVE:-Y}
                  if [[ "$SET_ACTIVE" =~ ^[Yy] ]]; then
                      set_config_value "mnemonic_file" "$WALLET_PATH" "true"
                      echo "Active wallet updated in config.toml"
                  fi
                  # Ask whether to store the encryption password in config.toml
                  echo ""
                  echo "You can store the wallet password in config.toml so all"
                  echo "commands (including the maker) work without prompting."
                  echo "If you choose No, the maker will ask for the password each time."
                  read -p "Store wallet password in config.toml? (y/N): " STORE_PWD
                  if [[ "$STORE_PWD" =~ ^[Yy] ]]; then
                      read -r -s -p "Enter the wallet encryption password: " PWD_STORE
                      echo ""
                      store_password "${PWD_STORE}"
                      unset PWD_STORE
                      echo "Password stored in config.toml."
                  fi
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
              clear
              echo "=== Select Active Wallet ==="
              echo ""
              WALLETS=$(list_wallets)
              if [ -z "$WALLETS" ]; then
                  echo "No wallet files found in $DATA_DIR/wallets/"
                  echo "Create or import a wallet first."
              else
                  echo "Available wallets:"
                  echo "$WALLETS" | nl -ba
                  echo ""
                  echo "Current: $(get_mnemonic_file)"
                  echo ""
                  read -p "Enter wallet filename: " WNAME
                  # Validate: only safe characters, no path separators
                  if [[ ! "$WNAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
                      echo "Invalid wallet filename. Use only letters, numbers, dot, underscore, and hyphen."
                  elif [ -f "$DATA_DIR/wallets/$WNAME" ]; then
                      set_config_value "mnemonic_file" "$DATA_DIR/wallets/$WNAME" "true"
                      echo "Active wallet set to: $WNAME"
                      echo "Restart the maker service for changes to take effect."
                  else
                      echo "File not found: $DATA_DIR/wallets/$WNAME"
                  fi
              fi
              pause
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
              if [ -z "$CURRENT_WALLET" ]; then
                  echo "ERROR: No wallet configured. Set up a wallet first (W -> SEL or NEW)."
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
              if [ -z "$CURRENT_WALLET" ]; then
                  echo "ERROR: No wallet configured. Set up a wallet first (W -> SEL or NEW)."
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
