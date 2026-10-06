from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "jmcore/src/jmcore/data/menu.joinmarket-ng.sh"
)


def _functions(*names: str) -> str:
    content = SCRIPT_PATH.read_text()
    return "\n".join(
        name
        + "() {"
        + content.split("\n" + name + "() {", 1)[1].split("\n}", 1)[0]
        + "\n}"
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


@pytest.mark.parametrize("separator", ["\n", "\u0085", "\u2028", "\r\n"])
def test_writer_binding_roundtrip_preserves_quoted_separators(
    tmp_path: Path, separator: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jmcore.settings import JoinMarketSettings
    from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint
    from maker.cli import _staged_wallet_binding

    stage = tmp_path / ".maker.env"
    passphrase = f'one  {separator}  "two" \\ three  '
    fingerprint = get_mnemonic_fingerprint("abandon " * 11 + "about", passphrase)
    result = subprocess.run(
        [
            "bash",
            "-c",
            _functions("write_maker_env")
            + '\nMAKER_ENV="$1"\nwrite_maker_env "$2" "$3" "$4"',
            "test",
            str(stage),
            f"password  {separator}  continuation  ",
            passphrase,
            fingerprint,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert passphrase not in result.stdout + result.stderr
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(tmp_path / "absent.toml"))
    assert _staged_wallet_binding(stage, JoinMarketSettings(), None) == (
        fingerprint,
        None,
    )


@pytest.mark.parametrize("metadata", ["absent", "legacy"])
@pytest.mark.parametrize("binding", ["matching", "unbound", "mismatch", "canceled"])
@pytest.mark.parametrize("configured", [True, False])
def test_restage_preserves_wallet_or_requires_explicit_credential(
    tmp_path: Path, metadata: str, binding: str, configured: bool
) -> None:
    from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

    mnemonic = "abandon " * 11 + "about"
    passphrase = "test restaging passphrase"
    fingerprint = get_mnemonic_fingerprint(mnemonic, passphrase)
    intended_passphrase = "explicit configured passphrase" if configured else passphrase
    intended_fingerprint = get_mnemonic_fingerprint(mnemonic, intended_passphrase)
    wallet = tmp_path / "wallet.mnemonic"
    wallet.write_text(mnemonic)
    if metadata == "legacy":
        wallet.with_suffix(".mnemonic.meta").write_text(
            f'{{"fingerprint":"{fingerprint}"}}'
        )
    config = tmp_path / "config.toml"
    config_text = f'[wallet]\nmnemonic_file = "{wallet}"\n'
    if configured:
        config_text += f'bip39_passphrase = "{intended_passphrase}"\n'
    config.write_text(config_text)
    stage = tmp_path / ".maker.env"
    original = f'BIP39_PASSPHRASE="{passphrase}"\n'
    if binding != "unbound":
        expected = fingerprint if binding == "matching" else "11223344"
        original += f'EXPECTED_FINGERPRINT="{expected}"\n'
    stage.write_text(original)
    pending = tmp_path / "pending.env"
    prompts = tmp_path / "prompts"
    prompts.touch()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("MNEMONIC", "BIP39", "JOINMARKET", "WALLET__", "EXPECTED_")
        )
    }
    env.update(
        TUI_PYTHON=sys.executable,
        CONFIG_FILE=str(config),
        JOINMARKET_CONFIG_FILE=str(config),
        JOINMARKET_DATA_DIR=str(tmp_path),
        CURRENT_WALLET=str(wallet),
        MAKER_ENV=str(stage),
        FINGERPRINT_CACHE=str(tmp_path / "fingerprint"),
        TEST_PENDING=str(pending),
        TEST_PROMPTS=str(prompts),
        TEST_PASSPHRASE=intended_passphrase,
        TEST_CANCEL=str(binding == "canceled").lower(),
    )
    helpers = _functions(
        "get_stored_mnemonic_password",
        "get_stored_bip39_passphrase",
        "get_maker_env_bip39_passphrase",
        "get_maker_env_expected_fingerprint",
        "write_maker_env",
        "wallet_requires_passphrase_prompt",
        "cache_wallet_fingerprint",
        "ensure_wallet_unlocked_global",
        "stage_maker_password",
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            helpers
            + """
jm-wallet() { return 2; }
whiptail() {
    printf 'prompt\n' >> "$TEST_PROMPTS"
    [ "$TEST_CANCEL" != true ] || return 1
    case "$2" in
        ' BIP39 Passphrase ') printf '%s' "$TEST_PASSPHRASE" >&2 ;;
        ' Wallet Fingerprint ') return 0 ;;
        *) return 1 ;;
    esac
}
stage_maker_password "$CURRENT_WALLET" "$TEST_PENDING"
""",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert stage.read_text() == original
    assert passphrase not in result.stdout + result.stderr
    assert intended_passphrase not in result.stdout + result.stderr
    if binding == "canceled" and not configured:
        assert result.returncode != 0
        assert not pending.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert pending.read_text() == (
            f'BIP39_PASSPHRASE="{intended_passphrase}"\n'
            f'EXPECTED_FINGERPRINT="{intended_fingerprint}"\n'
        )
    expected_prompts = (
        0
        if configured
        else {"matching": 0, "unbound": 2, "mismatch": 2, "canceled": 1}[binding]
    )
    assert len(prompts.read_text().splitlines()) == expected_prompts


EXACT_VALUES = [
    "\none",
    "one\ntwo",
    "one\n",
    "one\n\n",
    "\n",
    "\n\n",
    "one\r\n",
    'one "\\\n',
    "one\u0085\u2028",
]


def _credential_env(tmp_path: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("MNEMONIC", "BIP39", "JOINMARKET", "WALLET__", "EXPECTED_")
        )
    }
    wallet = tmp_path / "wallet.mnemonic"
    wallet.write_text("abandon " * 11 + "about")
    config = tmp_path / "config.toml"
    config.write_text(f'[wallet]\nmnemonic_file = "{wallet}"\n')
    env.update(
        TUI_PYTHON=sys.executable,
        CONFIG_FILE=str(config),
        JOINMARKET_CONFIG_FILE=str(config),
        JOINMARKET_DATA_DIR=str(tmp_path),
        CURRENT_WALLET=str(wallet),
        MAKER_ENV=str(tmp_path / ".maker.env"),
        FINGERPRINT_CACHE=str(tmp_path / "fingerprint"),
    )
    return env


@pytest.mark.parametrize("value", EXACT_VALUES + [""])
def test_tui_writer_and_getters_preserve_complete_credentials(
    tmp_path: Path, value: str
) -> None:
    env = _credential_env(tmp_path)
    env["TEST_VALUE"] = value
    result = subprocess.run(
        [
            "bash",
            "-c",
            _functions(
                "write_maker_env",
                "get_maker_env_password",
                "get_maker_env_bip39_passphrase",
            )
            + """
write_maker_env "$TEST_VALUE" "$TEST_VALUE" 11223344 || exit 1
password=$(get_maker_env_password && printf '.')
status=$?
if [ -n "$TEST_VALUE" ]; then
    [ "$status" = 0 ] || exit 1
    [ "${password%.}" = "$TEST_VALUE" ] || exit 1
else
    [ "$status" = 1 ] || exit 1
fi
passphrase=$(get_maker_env_bip39_passphrase && printf '.') || exit 1
printf '%s' "${passphrase%.}"
""",
        ],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == value.encode()


@pytest.mark.parametrize(
    "source",
    [
        "config",
        "stage",
        "missing-key",
        "empty-key",
        "invalid",
        "invalid-restage",
        "prompt",
        "cancel",
    ],
)
@pytest.mark.parametrize("value", ["one\n", "\n\n", "one\ntwo", "one\r\n"])
def test_tui_unlock_uses_exact_passphrase_and_preserves_absence_contract(
    tmp_path: Path, source: str, value: str
) -> None:
    from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

    env = _credential_env(tmp_path)
    env["TEST_VALUE"] = value
    env["TEST_SOURCE"] = source
    fingerprint = get_mnemonic_fingerprint("abandon " * 11 + "about", value)
    config = Path(env["CONFIG_FILE"])
    config.write_text(config.read_text() + f"bip39_passphrase = {json.dumps(value)}\n")
    stage = Path(env["MAKER_ENV"])
    if source == "stage":
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        stage.write_bytes(
            f'BIP39_PASSPHRASE="{escaped}"\nEXPECTED_FINGERPRINT="{fingerprint}"\n'.encode()
        )
    elif source == "missing-key":
        stage.write_text('MNEMONIC_PASSWORD="password"\n')
    elif source == "empty-key":
        empty_fingerprint = get_mnemonic_fingerprint("abandon " * 11 + "about", "")
        stage.write_text(
            f'BIP39_PASSPHRASE=""\nEXPECTED_FINGERPRINT="{empty_fingerprint}"\n'
        )
    elif source in {"invalid", "invalid-restage"}:
        stage.write_text('BIP39_PASSPHRASE="unfinished\n')
        if source == "invalid-restage":
            config.write_text(f'[wallet]\nmnemonic_file = "{env["CURRENT_WALLET"]}"\n')
    elif source in {"prompt", "cancel"}:
        config.write_text(
            f'[wallet]\nmnemonic_file = "{env["CURRENT_WALLET"]}"\nbip39_passphrase_enabled = true\n'
        )
    result = subprocess.run(
        [
            "bash",
            "-c",
            _functions(
                "get_stored_bip39_passphrase",
                "get_maker_env_bip39_passphrase",
                "get_maker_env_expected_fingerprint",
                "wallet_requires_passphrase_prompt",
                "cache_wallet_fingerprint",
                "ensure_wallet_unlocked_global",
            )
            + """
whiptail() {
    [ "$TEST_SOURCE" != cancel ] || return 1
    case "$2" in
        ' BIP39 Passphrase ') printf '%s' "$TEST_VALUE" >&2 ;;
        ' Wallet Fingerprint ') return 0 ;;
        *) return 1 ;;
    esac
}
if [ "$TEST_SOURCE" = invalid-restage ]; then
    ensure_wallet_unlocked_global restage || exit 1
else
    ensure_wallet_unlocked_global || exit 1
fi
printf '%s' "$BIP39_PASSPHRASE"
""",
        ],
        env=env,
        capture_output=True,
        timeout=30,
    )
    if source in {"invalid", "cancel"}:
        assert result.returncode != 0
        assert result.stdout == b""
    else:
        assert result.returncode == 0, result.stderr
        expected = "" if source == "empty-key" else value
        assert result.stdout == expected.encode()
        assert Path(
            env["FINGERPRINT_CACHE"]
        ).read_text().strip() == get_mnemonic_fingerprint(
            "abandon " * 11 + "about", expected
        )


@pytest.mark.parametrize("metadata", ["absent", "legacy"])
@pytest.mark.parametrize("mode", ["ordinary", "restage-accept", "restage-reject"])
@pytest.mark.parametrize("value", ["one\n", "one\n\n", "\n"])
def test_upgrade_does_not_silently_replace_truncated_bound_wallet(
    tmp_path: Path, metadata: str, mode: str, value: str
) -> None:
    from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

    env = _credential_env(tmp_path)
    old = value.rstrip("\n")
    old_fingerprint = get_mnemonic_fingerprint("abandon " * 11 + "about", old)
    new_fingerprint = get_mnemonic_fingerprint("abandon " * 11 + "about", value)
    wallet = Path(env["CURRENT_WALLET"])
    if metadata == "legacy":
        wallet.with_suffix(".mnemonic.meta").write_text(
            json.dumps({"fingerprint": old_fingerprint})
        )
    config = Path(env["CONFIG_FILE"])
    config.write_text(config.read_text() + f"bip39_passphrase = {json.dumps(value)}\n")
    original = f'BIP39_PASSPHRASE="{old}"\nEXPECTED_FINGERPRINT="{old_fingerprint}"\n'
    Path(env["MAKER_ENV"]).write_text(original)
    env.update(TEST_MODE=mode, TEST_OLD_FP=old_fingerprint, TEST_NEW_FP=new_fingerprint)
    result = subprocess.run(
        [
            "bash",
            "-c",
            _functions(
                "get_stored_bip39_passphrase",
                "get_maker_env_bip39_passphrase",
                "get_maker_env_expected_fingerprint",
                "wallet_requires_passphrase_prompt",
                "cache_wallet_fingerprint",
                "ensure_wallet_unlocked_global",
            )
            + """
whiptail() {
    [ "$2" = ' Restore Exact Passphrase ' ] || return 1
    [[ "$4" == *"$TEST_OLD_FP"* && "$4" == *"$TEST_NEW_FP"* ]] || return 1
    [[ "$*" == *--defaultno* ]] || return 1
    [ "$TEST_MODE" = restage-accept ]
}
if [ "$TEST_MODE" = ordinary ]; then
    ensure_wallet_unlocked_global || exit 1
else
    ensure_wallet_unlocked_global restage || exit 1
fi
printf '%s' "$BIP39_PASSPHRASE"
""",
        ],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert Path(env["MAKER_ENV"]).read_text() == original
    if mode == "restage-reject":
        assert result.returncode != 0
        assert not Path(env["FINGERPRINT_CACHE"]).exists()
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == (old if mode == "ordinary" else value).encode()


@pytest.mark.parametrize("source", ["config", "stage", "prompt", "cancel"])
@pytest.mark.parametrize("value", ["password\n\n", "\n"])
def test_tui_password_unlock_preserves_complete_value(
    tmp_path: Path, source: str, value: str
) -> None:
    env = _credential_env(tmp_path)
    env.update(TEST_VALUE=value, TEST_SOURCE=source)
    if source == "config":
        config = Path(env["CONFIG_FILE"])
        config.write_text(
            config.read_text() + f"mnemonic_password = {json.dumps(value)}\n"
        )
    elif source == "stage":
        Path(env["MAKER_ENV"]).write_bytes(f'MNEMONIC_PASSWORD="{value}"\n'.encode())
    result = subprocess.run(
        [
            "bash",
            "-c",
            _functions(
                "get_stored_mnemonic_password",
                "get_maker_env_password",
                "ensure_wallet_password",
            )
            + """
jm-wallet() { return 1; }
verify_wallet_password() { [ "$2" = "$TEST_VALUE" ]; }
whiptail() {
    # Config/staging reuse must not hide truncation by falling back to a prompt.
    [ "$TEST_SOURCE" = prompt ] || [ "$TEST_SOURCE" = cancel ] || return 1
    [ "$TEST_SOURCE" != cancel ] || return 1
    printf '%s' "$TEST_VALUE" >&2
}
ensure_wallet_password "$CURRENT_WALLET" || exit 1
printf '%s' "$MNEMONIC_PASSWORD"
""",
        ],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == (1 if source == "cancel" else 0), result.stderr
    assert result.stdout == (b"" if source == "cancel" else value.encode())


@pytest.mark.parametrize("value", EXACT_VALUES)
def test_config_to_maker_staging_binds_complete_passphrase(
    tmp_path: Path, value: str
) -> None:
    from jmcore.maker_env import parse_maker_env
    from jmcore.settings import JoinMarketSettings
    from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint
    from maker.cli import _staged_wallet_binding

    env = _credential_env(tmp_path)
    config = Path(env["CONFIG_FILE"])
    config.write_text(config.read_text() + f"bip39_passphrase = {json.dumps(value)}\n")
    result = subprocess.run(
        [
            "bash",
            "-c",
            _functions(
                "get_stored_mnemonic_password",
                "get_stored_bip39_passphrase",
                "get_maker_env_bip39_passphrase",
                "get_maker_env_expected_fingerprint",
                "wallet_requires_passphrase_prompt",
                "cache_wallet_fingerprint",
                "ensure_wallet_unlocked_global",
                "write_maker_env",
                "stage_maker_password",
            )
            + """
jm-wallet() { return 2; }
whiptail() { return 1; }
stage_maker_password "$CURRENT_WALLET"
""",
        ],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b""
    stage = Path(env["MAKER_ENV"])
    fingerprint = get_mnemonic_fingerprint("abandon " * 11 + "about", value)
    assert parse_maker_env(stage.read_bytes().decode()) == {
        "BIP39_PASSPHRASE": value,
        "EXPECTED_FINGERPRINT": fingerprint,
    }
    assert _staged_wallet_binding(stage, JoinMarketSettings(), None) == (
        fingerprint,
        None,
    )


@pytest.mark.parametrize("cancel", [True, False])
@pytest.mark.parametrize("value", ["one\n\n", "\n", 'one "\\\r\n'])
def test_bip39_storage_confirms_and_saves_complete_value(
    tmp_path: Path, value: str, cancel: bool
) -> None:
    import tomllib

    from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

    env = _credential_env(tmp_path)
    env.update(TEST_VALUE=value, TEST_CANCEL=str(cancel).lower())
    config = Path(env["CONFIG_FILE"])
    original = config.read_bytes()
    result = subprocess.run(
        [
            "bash",
            "-c",
            _functions(
                "get_stored_mnemonic_password",
                "set_config_value",
                "store_bip39_passphrase",
                "cache_wallet_fingerprint",
                "prompt_and_store_bip39_passphrase",
            )
            + """
whiptail() {
    case "$2" in
        ' Security Warning '| ' Wallet Fingerprint '| ' Passphrase Stored ') return 0 ;;
        ' BIP39 Passphrase ')
            [ "$TEST_CANCEL" != true ] || return 1
            printf '%s' "$TEST_VALUE" >&2 ;;
        *) return 1 ;;
    esac
}
prompt_and_store_bip39_passphrase
""",
        ],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == (1 if cancel else 0), result.stderr
    assert result.stdout == b""
    if cancel:
        assert config.read_bytes() == original
    else:
        assert tomllib.loads(config.read_text())["wallet"]["bip39_passphrase"] == value
        assert Path(
            env["FINGERPRINT_CACHE"]
        ).read_text().strip() == get_mnemonic_fingerprint(
            "abandon " * 11 + "about", value
        )
