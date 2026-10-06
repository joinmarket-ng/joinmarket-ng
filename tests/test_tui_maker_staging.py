from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "jmcore/src/jmcore/data/menu.joinmarket-ng.sh"
)


def _functions(*names: str) -> str:
    content = SCRIPT_PATH.read_text()
    return "\n".join(
        name + "()" + content.split(name + "()", 1)[1].split("\n}", 1)[0] + "\n}"
        for name in names
    )


@pytest.mark.parametrize("state", ["absent", "private", "readable", "symlink"])
def test_tui_write_maker_env_replaces_privately(tmp_path: Path, state: str) -> None:
    destination = tmp_path / ".maker.env"
    old_file = tmp_path / "old-env"
    old_file.write_text("old value\n")
    if state == "symlink":
        destination.symlink_to(old_file)
    elif state != "absent":
        destination.write_text("old value\n")
        destination.chmod(0o644 if state == "readable" else 0o600)
    password = 'test "quoted" \\ password'
    result = subprocess.run(
        [
            "bash",
            "-c",
            _functions("write_maker_env")
            + """
MAKER_ENV="$1"
umask 022
had_previous=0
if [ -e "$MAKER_ENV" ]; then
    exec 3< "$MAKER_ENV"
    had_previous=1
fi
mv() {
    test "$(stat -c %a "$3")" = 600 || return 1
    if [ -e "$MAKER_ENV" ]; then
        test "$(cat "$MAKER_ENV")" = 'old value' || return 1
    fi
    command mv "$@"
}
write_maker_env "$2" 'test passphrase' 11223344 || exit 1
if [ "$had_previous" = 1 ]; then
    test "$(cat <&3)" = 'old value' || exit 1
fi
test "$(umask)" = 0022
""",
            "test",
            str(destination),
            password,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not destination.is_symlink()
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.read_text() == (
        'MNEMONIC_PASSWORD="test \\"quoted\\" \\\\ password"\n'
        'BIP39_PASSPHRASE="test passphrase"\nEXPECTED_FINGERPRINT="11223344"\n'
    )
    assert old_file.read_text() == "old value\n"
    assert not list(tmp_path.glob(".maker.env.*"))


@pytest.mark.parametrize(
    "failure", ["sed", "mktemp", "chmod", "printf", "mv", "directory"]
)
def test_tui_stage_maker_password_propagates_write_failure(
    tmp_path: Path, failure: str
) -> None:
    destination = tmp_path / ".maker.env"
    fingerprint_cache = tmp_path / "fingerprint"
    fingerprint_cache.write_text("11223344\n")
    if failure == "directory":
        destination.mkdir()
        injected_failure = ""
    else:
        destination.write_text("old value\n")
        injected_failure = f"{failure}() {{ return 1; }}"
    result = subprocess.run(
        [
            "bash",
            "-c",
            _functions("write_maker_env", "stage_maker_password")
            + "\n"
            + injected_failure
            + """
MAKER_ENV="$1"
FINGERPRINT_CACHE="$2"
jm-wallet() { return 0; }
get_stored_mnemonic_password() { :; }
ensure_wallet_password() { MNEMONIC_PASSWORD=test-secret; }
ensure_wallet_unlocked_global() { BIP39_PASSPHRASE=test-passphrase; }
cache_wallet_fingerprint() { return 0; }
stage_maker_password test-wallet
""",
            "test",
            str(destination),
            str(fingerprint_cache),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, (
        "staging must not report success after a failed write"
    )
    if failure == "directory":
        assert list(destination.iterdir()) == []
    else:
        assert destination.read_text() == "old value\n"
    assert not list(tmp_path.glob(".maker.env.*"))


def test_writer_has_private_creation_before_replacement() -> None:
    writer = _functions("write_maker_env")
    assert "( umask 077;" in writer
    assert writer.index("umask 077") < writer.index("mktemp") < writer.index("mv -fT")
