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
    assert "No fidelity bonds" in list_block
    # Must capture jm-wallet output so it can be inspected before deciding
    # which TUI element to show.
    assert "jm-wallet list-bonds" in list_block
    assert "BONDS_OUT" in list_block


def test_tui_script_maker_start_has_wallet_picker() -> None:
    """Maker START must offer wallet selection before password prompts
    when multiple wallets exist (issue #454)."""
    content = SCRIPT_PATH.read_text()
    assert "maker_prepare_wallet()" in content
    assert "Start Maker -- Select Wallet" in content


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
    pre-setting LOGGING__LEVEL before launching jm-ng."""
    content = SCRIPT_PATH.read_text()
    assert 'if [ -z "${LOGGING__LEVEL:-}"' in content
    assert 'export LOGGING__LEVEL="WARNING"' in content


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
