from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "jmcore" / "src" / "jmcore" / "data" / "menu.joinmarket-ng.sh"


# ---------------------------------------------------------------------------
# Shell script tests
# ---------------------------------------------------------------------------


def test_tui_script_exists() -> None:
    assert SCRIPT_PATH.is_file()


def test_tui_script_is_valid_bash() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_tui_script_has_environment_detection() -> None:
    """The unified script must detect Raspiblitz vs standalone."""
    content = SCRIPT_PATH.read_text()
    assert "RASPIBLITZ=" in content
    assert "bonus.joinmarket-ng.sh" in content


def test_tui_script_has_stop_maker_helper() -> None:
    """The script must include the stop_maker helper for standalone mode."""
    content = SCRIPT_PATH.read_text()
    assert "stop_maker()" in content


def test_tui_script_has_display_send_status() -> None:
    """The script must include the display_send_status UX helper."""
    content = SCRIPT_PATH.read_text()
    assert "display_send_status()" in content


def test_tui_script_has_wallet_name_validation() -> None:
    """Wallet name inputs must be validated against directory traversal."""
    content = SCRIPT_PATH.read_text()
    assert "^[A-Za-z0-9._-]+$" in content


def test_tui_script_wallet_name_not_prefilled() -> None:
    """Wallet name inputs should start empty, not pre-filled with a default."""
    content = SCRIPT_PATH.read_text()
    # The inputbox should use empty string as initial value, not "default"/"imported"
    assert "leave empty for" in content


def test_tui_script_has_fee_rate_validation() -> None:
    """Fee rate must be validated as numeric when provided."""
    content = SCRIPT_PATH.read_text()
    assert "Fee rate must be a numeric value" in content


def test_tui_script_has_address_validation() -> None:
    """Destination address must be validated against basic bitcoin address format."""
    content = SCRIPT_PATH.read_text()
    assert "does not look like a valid Bitcoin address" in content


def test_tui_script_has_history_role_validation() -> None:
    """History role filter must be validated (maker/taker or empty)."""
    content = SCRIPT_PATH.read_text()
    assert "maker|taker)" in content


def test_tui_script_has_sed_escaping() -> None:
    """set_config_value must escape sed metacharacters."""
    content = SCRIPT_PATH.read_text()
    assert "sed -e 's/[&\\\\/|]/\\\\&/g'" in content or "escape" in content.lower()


def test_tui_script_has_clear_config_value() -> None:
    """clear_config_value helper must exist for clearing config keys."""
    content = SCRIPT_PATH.read_text()
    assert "clear_config_value()" in content


def test_tui_script_select_wallet_clears_password() -> None:
    """Select Active Wallet must clear stored password to prevent mismatch."""
    content = SCRIPT_PATH.read_text()
    assert 'clear_config_value "mnemonic_password"' in content


def test_tui_script_post_wallet_create_validates_password() -> None:
    """The third password prompt (post_wallet_create) must validate the
    password against the wallet file before saving it (issue #452)."""
    content = SCRIPT_PATH.read_text()
    assert "verify_wallet_password()" in content
    assert "prompt_and_store_password()" in content
    # The helper must actually invoke the verification CLI.
    assert "jm-wallet verify-password" in content


def test_tui_script_post_wallet_create_clears_password_on_activate() -> None:
    """When a newly created/imported wallet is set as active, the old
    mnemonic_password must be cleared to prevent mismatch (issue #455)."""
    content = SCRIPT_PATH.read_text()
    # The post_wallet_create function clears the password when set_active
    # is taken. Grep for the specific sequence to avoid false positives.
    post_create_block = content.split("post_wallet_create()", 1)[1].split(
        "# Helper:", 1
    )[0]
    assert 'set_config_value "mnemonic_file"' in post_create_block
    assert 'clear_config_value "mnemonic_password"' in post_create_block


def test_tui_script_fidelity_bonds_list_uses_msgbox_when_empty() -> None:
    """Fidelity Bonds LIST should surface the "no bonds" case via a TUI
    msgbox instead of leaving raw CLI output behind (issue #459)."""
    content = SCRIPT_PATH.read_text()
    list_block = content.split("LIST)", 1)[1].split("CREATE)", 1)[0]
    assert "whiptail" in list_block
    assert "No Fidelity Bonds" in list_block
    # Must capture jm-wallet output so it can be inspected before deciding
    # which TUI element to show.
    assert "jm-wallet list-bonds" in list_block
    assert "BONDS_OUT" in list_block


def test_tui_script_fidelity_bond_address_regex_matches_all_networks() -> None:
    """The CREATE flow extracts the generated bond address from
    ``jm-wallet generate-bond-address`` output via grep. The regex must
    match bech32 addresses across all networks (mainnet bc1, testnet/
    signet tb1, regtest bcrt1) and legacy base58 (1.../3...).

    Regression test for the bug where signet addresses like
    ``tb1qvksm82wsdaml0s8pvruptpj4xevtuf7rhgl2yxhtzmscpq0rldksfgluqx``
    were displayed as ``1qvksm...`` because the regex listed only
    ``bc1|[13]`` and the legacy ``1`` branch swallowed the inner ``1``
    of the ``tb1`` HRP, dropping the network prefix.
    """
    import re

    content = SCRIPT_PATH.read_text()
    # Locate the BOND_ADDR extraction line and pull the regex literal.
    # The extraction may pipe through printf/echo before grep, so accept any
    # prefix up to the first grep -oE '...'.
    match = re.search(r"BOND_ADDR=\$\([^\n]*grep -oE '([^']+)'", content)
    assert match is not None, "BOND_ADDR extraction line not found"
    bond_regex = match.group(1)

    samples = {
        # Signet (the one from the bug report).
        "tb1qvksm82wsdaml0s8pvruptpj4xevtuf7rhgl2yxhtzmscpq0rldksfgluqx": (
            "tb1qvksm82wsdaml0s8pvruptpj4xevtuf7rhgl2yxhtzmscpq0rldksfgluqx"
        ),
        # Mainnet bech32.
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4": (
            "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        ),
        # Regtest bech32.
        "bcrt1qq6hag67dl53wl99vzg42z8eyzfz2xlkvwk6f7m": (
            "bcrt1qq6hag67dl53wl99vzg42z8eyzfz2xlkvwk6f7m"
        ),
        # Legacy P2PKH.
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa": ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"),
        # Legacy P2SH.
        "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy": ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"),
    }
    for raw, expected in samples.items():
        out = subprocess.run(
            ["grep", "-oE", bond_regex],
            input=raw + "\n",
            check=False,
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, f"grep failed on {raw!r}: {out.stderr}"
        assert out.stdout.strip() == expected, (
            f"regex {bond_regex!r} produced {out.stdout.strip()!r} for "
            f"input {raw!r}, expected {expected!r}"
        )


def test_tui_script_maker_start_has_wallet_picker() -> None:
    """Maker START must use ensure_active_wallet for wallet selection
    before password prompts when multiple wallets exist (issue #454)."""
    content = SCRIPT_PATH.read_text()
    # Old maker_prepare_wallet() replaced by ensure_active_wallet()
    assert "ensure_active_wallet()" in content


def test_tui_script_post_wallet_create_warns_plaintext_storage() -> None:
    """Storing the password in config.toml must show a security warning
    (issue #453)."""
    content = SCRIPT_PATH.read_text()
    assert "Security Warning" in content
    assert "PLAIN TEXT" in content


def test_tui_script_defines_ensure_wallet_password_helper() -> None:
    """Commands that need the decrypted mnemonic must go through the
    whiptail-based `ensure_wallet_password` helper instead of letting
    jm-wallet fall through to its terminal password prompt."""
    content = SCRIPT_PATH.read_text()
    assert "ensure_wallet_password()" in content
    # The helper must export MNEMONIC_PASSWORD so jmcore picks it up.
    assert "export MNEMONIC_PASSWORD=" in content
    # And rely on whiptail for the actual prompt.
    assert 'whiptail --title " Wallet Password "' in content


def test_tui_script_wallet_info_uses_ensure_wallet_password() -> None:
    """`jm-wallet info` (both basic and extended) must be wrapped in a
    subshell that calls `ensure_wallet_password` first, so the user is
    prompted via whiptail instead of a raw CLI prompt."""
    content = SCRIPT_PATH.read_text()
    # Find the BASIC branch and check both that ensure_wallet_password is
    # invoked and that jm-wallet info is called within the same subshell
    # block (i.e. the two appear close together and in that order).
    basic_idx = content.find("BASIC)")
    assert basic_idx != -1
    # Look in the next ~500 chars for the pattern.
    window = content[basic_idx : basic_idx + 800]
    assert "ensure_wallet_password" in window
    assert "jm-wallet info" in window

    ext_idx = content.find("EXT)")
    assert ext_idx != -1
    window = content[ext_idx : ext_idx + 800]
    assert "ensure_wallet_password" in window
    assert "jm-wallet info --extended" in window


def test_tui_script_new_wallet_offers_word_count_choice() -> None:
    """Creating a new wallet must let the user pick 12 or 24 seed words
    and pass --words to jm-wallet generate (issue #457)."""
    content = SCRIPT_PATH.read_text()
    # A menu with both options must appear in the NEW branch.
    assert '"24" "24 words' in content
    assert '"12" "12 words' in content
    # generate must honour the chosen word count.
    assert "jm-wallet generate \\" in content
    assert '--words "$WORDS"' in content


def test_tui_script_wallet_menu_labels_new_wallet_word_support() -> None:
    """The Wallet Management menu should advertise 12- and 24-word
    wallet creation support so the menu matches the implemented flow."""
    content = SCRIPT_PATH.read_text()
    assert "Create New Wallet (12 or 24-word seed)" in content


def test_tui_script_maker_menu_loops_until_back() -> None:
    """The Maker submenu must have its own loop so leaving Fidelity Bonds
    returns to Maker Bot Control instead of falling back to the main menu
    (issue #460)."""
    content = SCRIPT_PATH.read_text()
    maker_block = content.split("    M)\n", 1)[1].split("\n    U)\n", 1)[0]
    assert "while true; do" in maker_block
    assert 'pgrep -f "jm-maker"' in maker_block
    assert "[ $? -ne 0 ] && break" in maker_block
    assert "BACK)\n              break" in maker_block


def test_tui_script_select_wallet_offers_password_storage() -> None:
    """Selecting an active wallet must offer to store the new wallet's
    password, otherwise the config ends up with a cleared password that
    can never be re-populated through the TUI (issue #455 Case 3)."""
    content = SCRIPT_PATH.read_text()
    # The SEL branch must invoke prompt_and_store_password to capture
    # the newly-selected wallet's password.
    assert 'prompt_and_store_password "$DATA_DIR/wallets/$WNAME"' in content
    # And still clear any pre-existing password first so a declined
    # prompt leaves the config in a clean state (no mismatch).
    assert 'clear_config_value "mnemonic_password"' in content


def test_tui_script_has_update_menu() -> None:
    """Main menu must offer an Update option."""
    content = SCRIPT_PATH.read_text()
    assert '"U" "Update JoinMarket-NG"' in content


def test_tui_script_update_has_channels() -> None:
    """Update submenu must offer STABLE, DEV, and VERSION channels."""
    content = SCRIPT_PATH.read_text()
    assert '"STABLE"' in content
    assert '"DEV"' in content
    assert '"VERSION"' in content


def test_tui_script_update_warns_running_maker() -> None:
    """Update flow must warn when the maker bot is running."""
    content = SCRIPT_PATH.read_text()
    assert "MAKER_STATUS" in content
    # Check the warning mentions maker being running
    assert "Maker Bot is currently running" in content


def test_tui_script_update_shows_current_version_with_commit() -> None:
    """The update menu title must show "vX.Y.Z (commit)" when the commit
    hash is available and we're on a stable build (issue #451 point 1)."""
    content = SCRIPT_PATH.read_text()
    assert "get_commit_hash" in content
    # Stable build label includes the short commit when present.
    assert 'CURRENT_LABEL="v${CURRENT_VERSION} (${CURRENT_COMMIT})"' in content


def test_tui_script_update_shows_dev_ref_with_commit() -> None:
    """When the build ref is set to a non-tag (e.g. ``main``), the title
    must show ``main / abc1234`` instead of falling back to the static
    version, matching the expected behaviour from issue #451's follow-up
    comment."""
    content = SCRIPT_PATH.read_text()
    assert "get_build_ref" in content
    assert "IS_DEV_BUILD=1" in content
    assert 'CURRENT_LABEL="${CURRENT_REF} / ${CURRENT_COMMIT}"' in content


def test_tui_script_update_dev_to_stable_not_already_current() -> None:
    """A user on a dev build must NOT be told they are already on
    ``vX.Y.Z`` when selecting STABLE, even when the static version
    string happens to match (issue #451 follow-up)."""
    content = SCRIPT_PATH.read_text()
    # The STABLE/VERSION branch of ALREADY_CURRENT must short-circuit to
    # 0 when IS_DEV_BUILD is set.
    assert 'if [ "$IS_DEV_BUILD" = "1" ]; then' in content


def test_tui_script_update_fetches_latest_stable_and_main() -> None:
    """The update menu must look up the latest release tag and the
    short hash of origin/main so STABLE/DEV entries show concrete
    versions (issue #451 points 2 and 3)."""
    content = SCRIPT_PATH.read_text()
    # Latest stable release tag via GitHub API
    assert "api.github.com/repos/joinmarket-ng/joinmarket-ng/releases/latest" in content
    assert '"tag_name"' in content
    # Latest main commit via git ls-remote
    assert "git ls-remote" in content
    assert "joinmarket-ng/joinmarket-ng.git" in content
    # Lookups must have a bounded timeout so network issues don't hang the TUI.
    assert "--max-time" in content


def test_tui_script_update_confirm_shows_current_and_target() -> None:
    """The confirm dialog must surface both the current and target
    identifiers (issue #451 point 4)."""
    content = SCRIPT_PATH.read_text()
    confirm_block = content.split("Confirm Update", 1)[1].split("clear\n", 1)[0]
    assert "Current:" in confirm_block
    assert "Target:" in confirm_block
    assert "${CURRENT_LABEL}" in confirm_block
    assert "${TARGET_LABEL}" in confirm_block


def test_tui_script_update_warns_when_already_current() -> None:
    """When the selected channel matches the installed version, the
    user must be warned before reinstalling (issue #451 point 5)."""
    content = SCRIPT_PATH.read_text()
    assert "Already Up to Date" in content
    # The warning must default to "No" so pressing Enter does not
    # trigger a redundant reinstall.
    assert "--defaultno" in content


def test_tui_script_update_cancel_returns_to_update_menu() -> None:
    """Cancelling the confirm dialog must return to the update submenu
    rather than the main menu (issue #451 point 6)."""
    content = SCRIPT_PATH.read_text()
    # The update case must wrap its prompts in its own loop so `continue`
    # goes back to the channel picker, not to the outer main-menu loop.
    update_block = content.split("    U)\n", 1)[1].split("\n    C)\n", 1)[0]
    assert "while true; do" in update_block


def test_tui_script_update_restart_hint_uses_jm_ng() -> None:
    """The launcher binary is `jm-ng`; the post-update restart hint
    must use the correct name (issue #451 point 7)."""
    content = SCRIPT_PATH.read_text()
    update_block = content.split("    U)\n", 1)[1].split("\n    C)\n", 1)[0]
    # Restart hint appears twice: in the confirm dialog and the post-update
    # message.
    assert update_block.count("jm-ng") >= 2


def test_tui_script_update_fails_fast_on_nonzero_exit() -> None:
    """The update flow must check the exit code of the installer/bonus
    script and NOT print \"Update complete\" when it failed. Otherwise a
    user whose update aborted (e.g. missing sudoers rule on raspiblitz)
    is told success and walks away with a broken setup."""
    content = SCRIPT_PATH.read_text()
    update_block = content.split("    U)\n", 1)[1].split("\n    C)\n", 1)[0]
    # Must capture the exit code from the update invocation.
    assert "UPDATE_RC=" in update_block
    # Must branch on success before printing the success message.
    assert 'if [ "$UPDATE_RC" -eq 0 ]' in update_block
    # Failure path must surface an error and NOT fall through to exit 0.
    assert "ERROR: Update failed" in update_block


# ---------------------------------------------------------------------------
# Python entry point tests
# ---------------------------------------------------------------------------


def test_tui_module_importable() -> None:
    from jmcore import tui  # noqa: F401


def test_tui_find_menu_script_finds_repo_script() -> None:
    from jmcore.tui import _find_menu_script

    found = _find_menu_script()
    assert found is not None
    assert found.name == "menu.joinmarket-ng.sh"


def test_tui_package_data_contains_menu_script() -> None:
    """The menu script must be discoverable via importlib.resources."""
    from importlib import resources

    ref = resources.files("jmcore").joinpath("data/menu.joinmarket-ng.sh")
    p = Path(str(ref))
    assert p.is_file(), f"Package data not found at {p}"


def test_tui_main_exits_without_whiptail() -> None:
    """When whiptail is missing, main() should exit with code 1."""
    from jmcore.tui import main

    with patch("shutil.which", return_value=None):
        with pytest.raises(SystemExit, match="1"):
            main()


# ---------------------------------------------------------------------------
# Regression tests for bug fixes (see GitHub issues #459, #461, #462)
# ---------------------------------------------------------------------------


def test_tui_exports_quiet_log_level_by_default() -> None:
    """Child jm-* commands launched from the TUI must default to WARNING so
    loguru INFO messages from jmcore.settings/jmwallet do not pollute the
    whiptail output panes (issue #459). The user can still override by
    pre-setting LOGGING__LEVEL before launching jm-ng, or by setting
    [tui] log_level in config.toml."""
    content = SCRIPT_PATH.read_text()
    assert 'if [ -z "${LOGGING__LEVEL:-}"' in content
    # WARNING must be the built-in fallback when neither the env var nor
    # [tui] log_level in config.toml is set.
    assert 'export LOGGING__LEVEL="${TUI_LOG_LEVEL:-WARNING}"' in content


def test_tui_clears_stale_mnemonic_file_entry() -> None:
    """If config.toml points at a mnemonic file that no longer exists (e.g.
    the user deleted it outside the TUI), every subsequent wallet action
    would blow up with ``Mnemonic file not found`` (issue #461). The main
    loop must detect that and clear the dangling config values."""
    content = SCRIPT_PATH.read_text()
    main_loop = content.split("while true; do", 1)[1]
    # Both detection and cleanup must happen before we compute WALLET_INFO.
    assert '[ ! -f "$CURRENT_WALLET" ]' in main_loop
    assert 'clear_config_value "mnemonic_file"' in main_loop
    assert 'clear_config_value "mnemonic_password"' in main_loop
    # And the stale-config path must surface a warning so the user knows
    # why their "active wallet" suddenly went away.
    assert "Stale Wallet Config" in main_loop


def test_tui_import_wallet_checks_duplicate_name_before_prompts() -> None:
    """Importing a wallet under an existing name must ask to overwrite
    before any seed/password prompts (issue #476). The CLI is invoked
    with --force so the early TUI confirmation is the single source of
    truth for the overwrite decision."""
    content = SCRIPT_PATH.read_text()

    imp_block = content.split("          IMP)\n", 1)[1].split("          VAL)\n", 1)[0]

    # The duplicate-name check must come BEFORE the word count menu and
    # BEFORE the password prompt to avoid wasted user input.
    overwrite_idx = imp_block.find("Wallet Already Exists")
    words_idx = imp_block.find("How many seed words does your wallet have?")
    password_idx = imp_block.find("prompt_new_wallet_password")
    assert overwrite_idx != -1, "missing duplicate-name whiptail check in IMP flow"
    assert overwrite_idx < words_idx, (
        "duplicate-name check must precede word-count prompt"
    )
    assert overwrite_idx < password_idx, (
        "duplicate-name check must precede password prompt"
    )

    # The whiptail confirmation must default to "No" so an accidental
    # Enter does not overwrite an existing wallet.
    assert "--defaultno" in imp_block

    # The CLI invocation must pass --force so it does not re-prompt.
    assert "jm-wallet import" in imp_block
    assert "--force" in imp_block


def test_tui_no_second_password_prompt_when_storing_in_config() -> None:
    """After creating/importing a wallet and choosing to store the password
    in config.toml, the TUI must reuse the password the user already
    entered instead of asking a third time (issue #462)."""
    content = SCRIPT_PATH.read_text()

    # A helper that collects the new-wallet password via whiptail must exist.
    assert "prompt_new_wallet_password()" in content

    # post_wallet_create must accept and honor the known password.
    assert 'local known_password="${2:-}"' in content
    assert 'if [ -n "$known_password" ]; then' in content
    assert 'store_password "$known_password"' in content

    # The NEW and IMP flows must capture the password and pass it to
    # post_wallet_create AND to jm-wallet via MNEMONIC_PASSWORD, with
    # --no-prompt-password so jm-wallet does not ask again.
    new_block = content.split("          NEW)\n", 1)[1].split("          IMP)\n", 1)[0]
    assert "NEW_PWD=$(prompt_new_wallet_password)" in new_block
    assert 'MNEMONIC_PASSWORD="$NEW_PWD" jm-wallet generate' in new_block
    assert "--no-prompt-password" in new_block
    assert 'post_wallet_create "$WALLET_PATH" "$NEW_PWD"' in new_block

    imp_block = content.split("          IMP)\n", 1)[1].split("          VAL)\n", 1)[0]
    assert "NEW_PWD=$(prompt_new_wallet_password)" in imp_block
    assert 'MNEMONIC_PASSWORD="$NEW_PWD" jm-wallet import' in imp_block
    assert "--no-prompt-password" in imp_block
    assert 'post_wallet_create "$WALLET_PATH" "$NEW_PWD"' in imp_block


# ---------------------------------------------------------------------------
# PR: Harmonize Wallet Selection - Comprehensive Test Suite
#
# These tests verify the refactoring that replaced 9 scattered "No wallet
# configured" checks with centralized helpers:
#   - check_stale_wallet(): Detects and cleans up stale config entries
#   - ensure_active_wallet(): Unified wallet selection with auto-select
#   - offer_maker_password_storage(): Maker-specific password storage prompt
#
# See: GitHub PR (Harmonize Wallet Selection)
# ---------------------------------------------------------------------------


def test_tui_script_has_check_stale_wallet_helper() -> None:
    """The script must include check_stale_wallet() helper function.

    This helper centralizes stale wallet detection that was previously
    duplicated in the main loop and W submenu. It checks if the configured
    mnemonic file still exists, and if not, clears both mnemonic_file and
    mnemonic_password from config to prevent "file not found" errors."""
    content = SCRIPT_PATH.read_text()

    # Function must exist
    assert "check_stale_wallet()" in content

    # Must show warning dialog when stale config detected (so user understands
    # why their active wallet disappeared)
    assert "Stale Wallet Config" in content


def test_tui_script_has_ensure_active_wallet_helper() -> None:
    """The script must include ensure_active_wallet() helper.

    This is the core of the harmonization - a single function that handles
    all wallet selection logic across 9 different call sites. It replaces
    inconsistent inline checks with unified behavior:
    - No wallets: Show error
    - 1 wallet: Auto-select without prompting
    - Multiple wallets: Show picker dialog

    Critical: Must warn about subshell usage since it modifies globals."""
    content = SCRIPT_PATH.read_text()

    # Core function must exist
    assert "ensure_active_wallet()" in content

    # Must include warning comment about subshell usage (modifies CURRENT_WALLET
    # and WALLET_INFO globals, so calling from subshell loses changes)
    assert "Do not call from subshells" in content

    # Must track whether wallet was just changed to control password storage
    # offers (only offer when wallet actually changed, not every time)
    assert "wallet_just_changed" in content


def test_tui_script_has_offer_maker_password_storage_helper() -> None:
    """The script must include offer_maker_password_storage() helper.

    Unlike other operations, Maker specifically needs stored password for
    automatic restart after crashes. This helper provides a context-specific
    explanation of WHY the password needs to be stored (not just "convenience"
    but "functionality required for unattended operation")."""
    content = SCRIPT_PATH.read_text()

    # Function must exist
    assert "offer_maker_password_storage()" in content

    # Must explain the auto-restart requirement in the dialog text so user
    # understands the functional necessity, not just convenience
    maker_block = content.split("offer_maker_password_storage()", 1)[1]
    assert "automatic restart" in maker_block.lower()


def test_tui_script_no_maker_prepare_wallet() -> None:
    """The old maker_prepare_wallet() function must be completely removed.

    This function was replaced by the combination of ensure_active_wallet()
    and offer_maker_password_storage(). Its logic was split: wallet selection
    went to ensure_active_wallet(), Maker-specific password handling went to
    offer_maker_password_storage()."""
    content = SCRIPT_PATH.read_text()

    # Old function must not exist anywhere in the script
    assert "maker_prepare_wallet()" not in content


def test_tui_script_ensure_active_wallet_no_password_when_already_active() -> None:
    """CRITICAL: When wallet already active, NO password offer.

    Regression risk: If ensure_active_wallet offers password storage even when
    wallet was already active, the user gets nagged on EVERY operation
    (Send, Balance, History, etc.) until they either store the password
    or say "No" every single time.

    Expected behavior: If CURRENT_WALLET already valid, return immediately
    without any password-related dialogs."""
    content = SCRIPT_PATH.read_text()

    # Extract ensure_active_wallet function body
    ensure_block = content.split("ensure_active_wallet()", 1)[1]
    ensure_block = ensure_block.split("offer_maker_password_storage()", 1)[0]

    # Find the early return path when wallet already valid
    early_path = ensure_block.split("Already have a valid wallet", 1)[1]
    early_path = early_path.split("return 0", 1)[0]

    # In this early return path, there must be NO password storage offer
    assert "prompt_and_store_password" not in early_path, (
        "Password offer should not happen when wallet already active"
    )
    assert "Store Password" not in early_path, (
        "Password dialog should not appear when wallet already active"
    )


def test_tui_script_ensure_active_wallet_offers_password_on_change() -> None:
    """ensure_active_wallet() must only offer password storage when wallet changed.

    This is the "wallet_just_changed" mechanism: password storage is only
    appropriate when the user just selected or auto-selected a wallet.
    If the wallet was already active, offering again would be nagging."""
    content = SCRIPT_PATH.read_text()

    # Extract ensure_active_wallet function body
    ensure_block = content.split("ensure_active_wallet()", 1)[1]
    ensure_block = ensure_block.split("offer_maker_password_storage()", 1)[0]

    # Password offer must be gated by wallet_just_changed flag
    assert 'if [ "$wallet_just_changed" = "yes" ]' in ensure_block, (
        "Password offer must be conditional on wallet_just_changed flag"
    )

    # The actual password prompt function must be called in this branch
    assert "prompt_and_store_password" in ensure_block, (
        "Password storage helper must be called when wallet changed"
    )


def test_tui_script_offer_maker_skips_if_already_stored() -> None:
    """offer_maker_password_storage must skip if password already stored.

    Prevents double-prompt scenario: If ensure_active_wallet just stored
    the password (because wallet changed), and then Maker START immediately
    calls offer_maker_password_storage, we must not ask again.

    This is checked by looking up stored password first and returning early."""
    content = SCRIPT_PATH.read_text()

    # Extract offer_maker_password_storage function body
    maker_block = content.split("offer_maker_password_storage()", 1)[1]
    maker_block = maker_block.split("# Helper:", 1)[0]

    # Must check if password already stored in config
    assert "get_stored_mnemonic_password" in maker_block, (
        "Must check existing stored password"
    )

    # Must return early (skip dialog) if already stored
    assert 'if [ -n "$stored_pwd" ]; then' in maker_block, (
        "Must check if stored_pwd is non-empty"
    )
    assert "return 0" in maker_block, "Must return early if password already stored"


def test_tui_script_maker_start_call_order_correct() -> None:
    """Maker START must call ensure_active_wallet BEFORE offer_maker_password_storage.

    Order matters: ensure_active_wallet may change CURRENT_WALLET and
    may offer/store password (if wallet changed). After that completes,
    offer_maker_password_storage checks if password is still missing
    and offers Maker-specific explanation.

    If order reversed, we'd check for stored password before wallet selection
    completes, which makes no sense."""
    content = SCRIPT_PATH.read_text()

    # Extract Maker START case block
    start_block = content.split("START)", 1)[1].split("STOP)", 1)[0]

    # Find positions of both function calls
    ensure_pos = start_block.find("ensure_active_wallet")
    offer_pos = start_block.find("offer_maker_password_storage")

    # Both must exist
    assert ensure_pos != -1, "ensure_active_wallet must be called in Maker START"
    assert offer_pos != -1, "offer_maker_password_storage must be called in Maker START"

    # ensure_active_wallet must come FIRST
    assert ensure_pos < offer_pos, (
        "Wrong call order: ensure_active_wallet must come before offer_maker_password_storage"
    )


# =============================================================================
# check_stale_wallet call sites (6 submenu loops)
# =============================================================================


def test_tui_script_main_loop_uses_check_stale_wallet() -> None:
    """Main menu loop must call check_stale_wallet.

    Previously had inline stale check code. Now delegates to helper
    for consistency and maintainability."""
    content = SCRIPT_PATH.read_text()

    # Extract main loop (from while true to case statement)
    main_loop = content.split("while true; do", 1)[1]
    main_loop = main_loop.split("case $CHOICE in", 1)[0]

    # Must call the helper
    assert "check_stale_wallet" in main_loop, (
        "Main loop must refresh wallet state via check_stale_wallet"
    )


def test_tui_script_wallet_submenu_uses_check_stale_wallet() -> None:
    """W submenu loop must call check_stale_wallet.

    Previously had inline stale check. Now unified with main loop behavior."""
    content = SCRIPT_PATH.read_text()

    # Extract W submenu block
    w_block = content.split("W)\n", 1)[1].split("M)\n", 1)[0]

    # Must call the helper to keep WALLET_INFO fresh
    assert "check_stale_wallet" in w_block, "W submenu must refresh wallet state"


def test_tui_script_bal_submenu_uses_check_stale_wallet() -> None:
    """BAL submenu loop must call check_stale_wallet.

    New addition - previously had NO stale check, so WALLET_INFO could
    be stale if user deleted wallet file while in submenu."""
    content = SCRIPT_PATH.read_text()

    # Extract BAL submenu block
    bal_block = content.split("BAL)", 1)[1].split("HIST)", 1)[0]

    # Must now call the helper
    assert "check_stale_wallet" in bal_block, "BAL submenu must refresh wallet state"


def test_tui_script_maker_submenu_uses_check_stale_wallet() -> None:
    """Maker submenu loop must call check_stale_wallet.

    New addition - previously had NO stale check."""
    content = SCRIPT_PATH.read_text()

    # Extract Maker submenu block
    maker_block = content.split("M)\n", 1)[1].split("C)\n", 1)[0]

    # Must now call the helper
    assert "check_stale_wallet" in maker_block, (
        "Maker submenu must refresh wallet state"
    )


def test_tui_script_bonds_submenu_uses_check_stale_wallet() -> None:
    """Bonds submenu loop must call check_stale_wallet.

    New addition - previously had NO stale check."""
    content = SCRIPT_PATH.read_text()

    # Extract Bonds submenu block
    bonds_block = content.split("BONDS)", 1)[1].split("LOG)", 1)[0]

    # Must now call the helper
    assert "check_stale_wallet" in bonds_block, (
        "Bonds submenu must refresh wallet state"
    )


def test_tui_script_update_submenu_uses_check_stale_wallet() -> None:
    """Update submenu loop must call check_stale_wallet.

    New addition - previously had NO stale check."""
    content = SCRIPT_PATH.read_text()

    # Extract Update submenu block
    update_block = content.split("U)\n", 1)[1].split("I)\n", 1)[0]

    # Must now call the helper
    assert "check_stale_wallet" in update_block, (
        "Update submenu must refresh wallet state"
    )


# =============================================================================
# ensure_active_wallet call sites (9 former "No wallet configured" locations)
# =============================================================================


def test_tui_script_send_uses_ensure_active_wallet() -> None:
    """S (Send) must use ensure_active_wallet.

    Replaces inline 'No wallet configured' check with unified helper.
    Old behavior: 3 different error messages, inconsistent actions.
    New behavior: Single error message, consistent auto-select/picker logic."""
    content = SCRIPT_PATH.read_text()

    # Extract Send case block
    s_block = content.split("S)\n", 1)[1].split("W)\n", 1)[0]

    # Must use unified helper
    assert "ensure_active_wallet" in s_block, "Send must use unified wallet check"

    # Old inline error message must be GONE (replaced by unified message)
    assert "Set up a wallet first (W -> NEW or SEL)" not in s_block, (
        "Old inline error message must be removed"
    )


def test_tui_script_bal_uses_ensure_active_wallet() -> None:
    """BAL must use ensure_active_wallet.

    Replaces inline 'No wallet configured' check."""
    content = SCRIPT_PATH.read_text()

    # Extract BAL case block
    bal_block = content.split("BAL)", 1)[1].split("HIST)", 1)[0]

    # Must use unified helper
    assert "ensure_active_wallet" in bal_block, "BAL must use unified wallet check"


def test_tui_script_hist_uses_ensure_active_wallet() -> None:
    """HIST must use ensure_active_wallet.

    Replaces inline 'No wallet configured' check."""
    content = SCRIPT_PATH.read_text()

    # Extract HIST case block
    hist_block = content.split("HIST)", 1)[1].split("FREEZE)", 1)[0]

    # Must use unified helper
    assert "ensure_active_wallet" in hist_block, "HIST must use unified wallet check"


def test_tui_script_freeze_uses_ensure_active_wallet() -> None:
    """FREEZE must use ensure_active_wallet.

    Replaces inline 'No wallet configured' check."""
    content = SCRIPT_PATH.read_text()

    # Extract FREEZE case block
    freeze_block = content.split("FREEZE)", 1)[1].split("NEW)", 1)[0]

    # Must use unified helper
    assert "ensure_active_wallet" in freeze_block, (
        "FREEZE must use unified wallet check"
    )


def test_tui_script_seed_uses_ensure_active_wallet() -> None:
    """SEED must use ensure_active_wallet.

    Replaces inline 'No wallet configured' check."""
    content = SCRIPT_PATH.read_text()

    # Extract SEED case block
    seed_block = content.split("SEED)", 1)[1].split("BACK)", 1)[0]

    # Must use unified helper
    assert "ensure_active_wallet" in seed_block, "SEED must use unified wallet check"


def test_tui_script_bonds_list_uses_ensure_active_wallet() -> None:
    """BONDS LIST must use ensure_active_wallet.

    Replaces inline 'No wallet configured' check."""
    content = SCRIPT_PATH.read_text()

    # Extract LIST case block
    list_block = content.split("LIST)", 1)[1].split("CREATE)", 1)[0]

    # Must use unified helper
    assert "ensure_active_wallet" in list_block, (
        "BONDS LIST must use unified wallet check"
    )


def test_tui_script_bonds_create_uses_ensure_active_wallet() -> None:
    """BONDS CREATE must use ensure_active_wallet.

    Replaces inline 'No wallet configured' check."""
    content = SCRIPT_PATH.read_text()

    # Extract CREATE case block
    create_block = content.split("CREATE)", 1)[1].split("BACK)", 1)[0]

    # Must use unified helper
    assert "ensure_active_wallet" in create_block, (
        "BONDS CREATE must use unified wallet check"
    )


def test_tui_script_maker_start_uses_both_helpers() -> None:
    """Maker START must use ensure_active_wallet + offer_maker_password_storage.

    Two-step process:
    1. ensure_active_wallet: Select/active wallet, offer password if just changed
    2. offer_maker_password_storage: Maker-specific password offer with explanation

    This replaces the old maker_prepare_wallet() monolithic function."""
    content = SCRIPT_PATH.read_text()

    # Extract START case block
    start_block = content.split("START)", 1)[1].split("STOP)", 1)[0]

    # Must call both helpers in correct order
    assert "ensure_active_wallet" in start_block, (
        "Maker START must use ensure_active_wallet"
    )
    assert "offer_maker_password_storage" in start_block, (
        "Maker START must use offer_maker_password_storage"
    )


def test_tui_script_maker_restart_uses_both_helpers() -> None:
    """Maker RESTART must use ensure_active_wallet + offer_maker_password_storage.

    Same two-step process as START."""
    content = SCRIPT_PATH.read_text()

    # Extract RESTART case block
    restart_block = content.split("RESTART)", 1)[1].split("BONDS)", 1)[0]

    # Must call both helpers
    assert "ensure_active_wallet" in restart_block, (
        "Maker RESTART must use ensure_active_wallet"
    )
    assert "offer_maker_password_storage" in restart_block, (
        "Maker RESTART must use offer_maker_password_storage"
    )


def test_tui_script_unified_error_message() -> None:
    """All "No wallet" errors must use unified message.

    Previously 3 different messages:
    - "W → NEW or SEL"
    - "Select Active Wallet or Create New Wallet first"
    - "W → SEL or NEW"

    Now all use: "Create or import a wallet first (W → NEW or IMP)"""
    content = SCRIPT_PATH.read_text()

    # Extract ensure_active_wallet function body
    ensure_block = content.split("ensure_active_wallet()", 1)[1]
    ensure_block = ensure_block.split("offer_maker_password_storage()", 1)[0]

    # Must use unified error message
    assert "Create or import a wallet first" in ensure_block, (
        "Must use unified error message part 1"
    )
    assert "NEW or IMP" in ensure_block, "Must use unified error message part 2"


# ---------------------------------------------------------------------------
# Subshell-based Password Prompt Tests
# ---------------------------------------------------------------------------


def test_tui_script_send_uses_subshell_for_password() -> None:
    """SEND must call ensure_wallet_password inside subshell.

    Bug fix regression test: Previously ensure_wallet_password was called
    outside the subshell, causing MNEMONIC_PASSWORD to leak into the main
    shell. This made the password prompt be skipped on subsequent operations
    (e.g. after BAL was called before SEND).

    Using a subshell isolates the password variable automatically."""
    content = SCRIPT_PATH.read_text()

    # Extract SEND case block
    s_block = content.split("S)\n", 1)[1].split("W)\n", 1)[0]

    # Find the subshell opening parenthesis
    subshell_open = s_block.rfind("(\n", 0, s_block.find("ensure_wallet_password"))

    # Find ensure_wallet_password position
    ensure_pos = s_block.find("ensure_wallet_password")

    # ensure_wallet_password must exist
    assert ensure_pos != -1, "ensure_wallet_password must be called in SEND"

    # ensure_wallet_password must be INSIDE the subshell
    assert subshell_open != -1, "SEND must use a subshell"
    assert subshell_open < ensure_pos, (
        "ensure_wallet_password must be inside the subshell"
    )


def test_tui_script_seed_uses_subshell_for_password() -> None:
    """SEED must call ensure_wallet_password inside subshell.

    Same isolation requirement as SEND - the password check must run in a
    subshell to prevent MNEMONIC_PASSWORD from leaking into the main shell."""
    content = SCRIPT_PATH.read_text()

    # Extract SEED case block
    seed_block = content.split("SEED)", 1)[1].split("BACK)", 1)[0]

    # Find the subshell opening parenthesis
    subshell_open = seed_block.rfind("(\n", 0, seed_block.find("ensure_wallet_password"))

    # Find ensure_wallet_password position
    ensure_pos = seed_block.find("ensure_wallet_password")

    # ensure_wallet_password must exist
    assert ensure_pos != -1, "ensure_wallet_password must be called in SEED"

    # ensure_wallet_password must be INSIDE the subshell
    assert subshell_open != -1, "SEED must use a subshell"
    assert subshell_open < ensure_pos, (
        "ensure_wallet_password must be inside the subshell"
    )


def test_tui_script_send_password_after_clear_in_subshell() -> None:
    """SEND must clear screen first, then run password check in subshell.

    Updated test: Previously checked that ensure_wallet_password was called
    BEFORE clear (outside subshell). Now checks correct order: clear first, 
    then subshell containing ensure_wallet_password."""
    content = SCRIPT_PATH.read_text()
    s_block = content.split("S)\n", 1)[1].split("W)\n", 1)[0]

    clear_pos = s_block.find("clear")
    subshell_pos = s_block.find("(\n")
    ensure_pos = s_block.find("ensure_wallet_password")

    assert clear_pos != -1, "clear must exist"
    assert subshell_pos != -1, "subshell must exist"  
    assert ensure_pos != -1, "ensure_wallet_password must exist"

    # Order: clear -> subshell -> ensure_wallet_password
    assert clear_pos < subshell_pos < ensure_pos, (
        "Required: clear first, then subshell containing ensure_wallet_password"
    )


def test_tui_script_send_aborts_with_exit_in_subshell() -> None:
    """When password fails, SEND must use 'exit 1' inside the subshell.

    Updated test: Previously checked for 'continue' in parent shell.
    Now checks for 'exit 1' inside the subshell to abort only the subshell,
    not the entire script."""
    content = SCRIPT_PATH.read_text()
    s_block = content.split("S)\n", 1)[1].split("W)\n", 1)[0]

    # Extract subshell content
    subshell_start = s_block.find("(\n")
    subshell_end = s_block.find(")\n", subshell_start)
    assert subshell_start != -1, "subshell must exist"
    
    subshell_content = s_block[subshell_start:subshell_end]

    # Inside subshell: ensure_wallet_password || exit 1
    assert "ensure_wallet_password" in subshell_content
    assert "exit 1" in subshell_content, (
        "Subshell must use 'exit 1' to abort on password failure"
    )


def test_tui_script_seed_no_manual_exit_code_handling() -> None:
    """SEED must not use manual SEED_EXIT checking.

    Using a subshell makes manual exit code tracking redundant - the subshell
    exit code is sufficient."""
    content = SCRIPT_PATH.read_text()
    seed_block = content.split("SEED)", 1)[1].split("BACK)", 1)[0]

    # Old code had SEED_EXIT=$? and if [ "$SEED_EXIT" -ne 0 ]
    assert "SEED_EXIT=$?" not in seed_block, (
        "SEED_EXIT handling removed - subshell exit code is sufficient"
    )
    assert 'if [ "$SEED_EXIT" -ne 0 ]' not in seed_block, (
        "Manual exit code check removed - handled by subshell || exit"
    )


def test_tui_script_seed_pause_inside_subshell() -> None:
    """SEED pause logic must be inside subshell.

    Combined test: Previously had separate tests for pause behavior.
    Now simplified: pause inside subshell means it only runs on success
    (subshell exits before pause on failure)."""
    content = SCRIPT_PATH.read_text()
    seed_block = content.split("SEED)", 1)[1].split("BACK)", 1)[0]

    # Find subshell boundaries
    subshell_start = seed_block.find("(\n")
    subshell_end = seed_block.find(")\n", subshell_start)
    assert subshell_start != -1, "subshell must exist"
    
    subshell_content = seed_block[subshell_start:subshell_end]
    after_subshell = seed_block[subshell_end:]

    # Pause prompt must be INSIDE subshell
    assert "Press [Enter] to continue" in subshell_content, (
        "Pause must be inside subshell (executed only on success)"
    )

    # After subshell should just have clear and case end
    assert "clear" in after_subshell, "clear should follow subshell"