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


def test_tui_script_new_wallet_offers_word_count_choice() -> None:
    """Creating a new wallet must let the user pick 12 or 24 seed words
    and pass --words to jm-wallet generate (issue #457)."""
    content = SCRIPT_PATH.read_text()
    # A menu with both options must appear in the NEW branch.
    assert '"24" "24 words' in content
    assert '"12" "12 words' in content
    # generate must honour the chosen word count.
    assert 'jm-wallet generate --words "$WORDS"' in content


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
    hash is available (issue #451 point 1)."""
    content = SCRIPT_PATH.read_text()
    assert "get_commit_hash" in content
    # Current label uses "v${CURRENT_VERSION}" and appends the short commit
    # when present.
    assert 'CURRENT_LABEL="v${CURRENT_VERSION} (${CURRENT_COMMIT})"' in content


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
    """The launcher binary is `jm-ng`, not `jm-tui`; both restart hints
    in the update flow must use the correct name (issue #451 point 7)."""
    content = SCRIPT_PATH.read_text()
    update_block = content.split("    U)\n", 1)[1].split("\n    C)\n", 1)[0]
    assert "jm-tui" not in update_block
    # Restart hint appears twice: in the confirm dialog and the post-update
    # message.
    assert update_block.count("jm-ng") >= 2


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
