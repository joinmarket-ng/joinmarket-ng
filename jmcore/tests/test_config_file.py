"""Subprocess coverage for the narrow TUI TOML config helper."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import tomlkit


def _run_helper(
    command: str,
    config_path: Path,
    section: str,
    key: str,
    *,
    value: str | None = None,
    as_bool: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    argv = [
        sys.executable,
        "-m",
        "jmcore.config_file",
        command,
        "--config",
        str(config_path),
        "--section",
        section,
        "--key",
        key,
    ]
    if as_bool:
        argv.append("--bool")
    return subprocess.run(
        argv,
        input=None if value is None else value.encode(),
        check=False,
        capture_output=True,
    )


def test_set_get_and_remove_are_table_scoped_on_sparse_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "# Sparse user config\n"
        "[maker]\n"
        'mnemonic_file = "maker-owned.mnemonic" # unrelated key\n'
        "\n"
        "[tui]\n"
    )
    wallet_path = r'/wallets/quoted "wallet" #1\\path & more.mnemonic'
    password = r'password # "quoted" \\ & value'

    assert (
        _run_helper("set", config_path, "wallet", "mnemonic_file", value=wallet_path).returncode
        == 0
    )
    assert (
        _run_helper("set", config_path, "wallet", "mnemonic_password", value=password).returncode
        == 0
    )
    assert _run_helper("set", config_path, "tui", "log_level", value="INFO").returncode == 0

    assert _run_helper("get", config_path, "wallet", "mnemonic_file").stdout.decode() == wallet_path
    assert (
        _run_helper("get", config_path, "wallet", "mnemonic_password").stdout.decode() == password
    )
    assert (
        _run_helper("get", config_path, "maker", "mnemonic_file").stdout == b"maker-owned.mnemonic"
    )

    result = _run_helper("remove", config_path, "wallet", "mnemonic_password")
    assert result.returncode == 0, result.stderr.decode()
    config_text = config_path.read_text()
    assert "maker-owned.mnemonic" in config_text
    assert password not in config_text
    assert tomlkit.parse(config_text)["tui"]["log_level"] == "INFO"


def test_set_adds_active_table_to_legacy_commented_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "# Legacy configuration\n"
        "# [wallet]\n"
        '# mnemonic_file = ""\n'
        '# mnemonic_password = ""\n'
        "\n"
        "# [maker]\n"
        "# min_size = 100000\n"
    )

    result = _run_helper(
        "set",
        config_path,
        "wallet",
        "mnemonic_file",
        value="legacy-wallet.mnemonic",
    )

    assert result.returncode == 0, result.stderr.decode()
    config_text = config_path.read_text()
    assert "# [wallet]" in config_text
    assert '# mnemonic_file = ""' in config_text
    assert tomlkit.parse(config_text)["wallet"]["mnemonic_file"] == "legacy-wallet.mnemonic"


def test_set_creates_missing_config_with_private_atomic_write(tmp_path: Path) -> None:
    config_path = tmp_path / "nested" / "config.toml"

    result = _run_helper("set", config_path, "wallet", "mnemonic_file", value="wallet.mnemonic")

    assert result.returncode == 0, result.stderr.decode()
    assert tomlkit.parse(config_path.read_text()) == {
        "wallet": {"mnemonic_file": "wallet.mnemonic"}
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert not list(config_path.parent.glob(".config.toml.*.tmp"))

    empty_config = tmp_path / "empty.toml"
    empty_config.write_text("")
    empty_result = _run_helper("set", empty_config, "tui", "log_level", value="WARNING")
    assert empty_result.returncode == 0, empty_result.stderr.decode()
    assert tomlkit.parse(empty_config.read_text())["tui"]["log_level"] == "WARNING"


def test_get_and_remove_absent_values_are_noops_without_rewrite(tmp_path: Path) -> None:
    missing_config = tmp_path / "missing.toml"
    get_result = _run_helper("get", missing_config, "wallet", "mnemonic_file")
    remove_result = _run_helper("remove", missing_config, "wallet", "mnemonic_file")

    assert get_result.returncode == 0
    assert get_result.stdout == b""
    assert remove_result.returncode == 0
    assert not missing_config.exists()

    config_path = tmp_path / "config.toml"
    config_path.write_text('[wallet]\nmnemonic_file = "wallet.mnemonic"\n')
    before = config_path.stat()
    before_text = config_path.read_text()

    result = _run_helper("remove", config_path, "wallet", "mnemonic_password")

    assert result.returncode == 0, result.stderr.decode()
    after = config_path.stat()
    assert config_path.read_text() == before_text
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns


def test_set_same_value_and_repeated_remove_do_not_rewrite_or_retain_secret(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    secret = 'removable "secret" # value'
    assert (
        _run_helper("set", config_path, "wallet", "mnemonic_password", value=secret).returncode == 0
    )
    before = config_path.stat()

    same_value = _run_helper("set", config_path, "wallet", "mnemonic_password", value=secret)

    assert same_value.returncode == 0, same_value.stderr.decode()
    assert config_path.stat().st_ino == before.st_ino

    assert _run_helper("remove", config_path, "wallet", "mnemonic_password").returncode == 0
    after_remove = config_path.stat()
    assert secret not in config_path.read_text()
    assert _run_helper("remove", config_path, "wallet", "mnemonic_password").returncode == 0
    assert config_path.stat().st_ino == after_remove.st_ino


def test_malformed_toml_and_invalid_target_shapes_are_unchanged(tmp_path: Path) -> None:
    malformed_config = tmp_path / "malformed.toml"
    malformed_text = '[wallet\nmnemonic_file = "old"\n'
    secret = "must-not-appear-in-errors"
    malformed_config.write_text(malformed_text)

    malformed_result = _run_helper(
        "set",
        malformed_config,
        "wallet",
        "mnemonic_password",
        value=secret,
    )

    assert malformed_result.returncode == 2
    assert malformed_config.read_text() == malformed_text
    assert secret.encode() not in malformed_result.stderr

    malformed_remove = _run_helper("remove", malformed_config, "wallet", "mnemonic_password")
    assert malformed_remove.returncode == 2
    assert malformed_config.read_text() == malformed_text

    invalid_shape_config = tmp_path / "invalid-shape.toml"
    invalid_shape_text = "[wallet]\nmnemonic_file = 1\n"
    invalid_shape_config.write_text(invalid_shape_text)
    for command in ("get", "set", "remove"):
        result = _run_helper(
            command,
            invalid_shape_config,
            "wallet",
            "mnemonic_file",
            value="new" if command == "set" else None,
        )
        assert result.returncode == 2
        assert invalid_shape_config.read_text() == invalid_shape_text


def test_set_preserves_a_config_symlink_alias(tmp_path: Path) -> None:
    target = tmp_path / "configured.toml"
    target.write_text("# managed elsewhere\n[wallet]\n")
    config_alias = tmp_path / "config.toml"
    config_alias.symlink_to(target)

    result = _run_helper(
        "set",
        config_alias,
        "wallet",
        "mnemonic_password",
        value='alias "password" # \\ &',
    )

    assert result.returncode == 0, result.stderr.decode()
    assert config_alias.is_symlink()
    assert (
        tomlkit.parse(target.read_text())["wallet"]["mnemonic_password"]
        == 'alias "password" # \\ &'
    )
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def _template_shaped_config() -> str:
    return (
        "# ============================================================================\n"
        "# Wallet Settings\n"
        "# ============================================================================\n"
        "\n"
        "[wallet]\n"
        "# Number of mixing depths\n"
        "# mixdepth_count = 5\n"
        "\n"
        "# Mnemonic file settings (optional defaults)\n"
        "# mnemonic_file =\n"
        "# mnemonic_password =\n"
        "\n"
        "# ============================================================================\n"
        "# Logging Settings\n"
        "# ============================================================================\n"
        "\n"
        "[logging]\n"
        "# Log level\n"
        'level = "INFO"\n'
        "\n"
        "# sensitive = false\n"
        "\n"
        "# ============================================================================\n"
        "# TUI Settings\n"
        "# ============================================================================\n"
        "\n"
        "[tui]\n"
        '# log_level = "INFO"\n'
    )


def test_set_places_new_keys_before_the_next_chapter_banner(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_template_shaped_config())

    assert (
        _run_helper("set", config_path, "wallet", "mnemonic_file", value="wallet.m").returncode == 0
    )
    assert (
        _run_helper("set", config_path, "wallet", "mnemonic_password", value="pw").returncode == 0
    )

    config_text = config_path.read_text()
    logging_banner = config_text.index("# Logging Settings")
    assert config_text.index('mnemonic_file = "wallet.m"') < logging_banner
    assert config_text.index('mnemonic_password = "pw"') < logging_banner
    parsed = tomlkit.parse(config_text)
    assert parsed["wallet"]["mnemonic_file"] == "wallet.m"
    assert parsed["wallet"]["mnemonic_password"] == "pw"
    assert parsed["logging"]["level"] == "INFO"


def test_set_places_new_key_before_banner_in_chapter_with_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_template_shaped_config())

    assert _run_helper("set", config_path, "logging", "sensitive", value="true").returncode == 0

    config_text = config_path.read_text()
    assert config_text.index('level = "INFO"') < config_text.index('sensitive = "true"')
    assert config_text.index('sensitive = "true"') < config_text.index("# TUI Settings")


def test_set_updates_a_well_placed_key_in_place(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _template_shaped_config().replace("# mnemonic_file =\n", 'mnemonic_file = "old.mnemonic"\n')
    )

    assert _run_helper("set", config_path, "wallet", "mnemonic_file", value="new.m").returncode == 0

    config_text = config_path.read_text()
    assert config_text.count('mnemonic_file = "new.m"') == 1
    assert config_text.index('mnemonic_file = "new.m"') < config_text.index("# Logging Settings")
    assert "# mnemonic_password =" in config_text

    before = config_path.stat()
    same_value = _run_helper("set", config_path, "wallet", "mnemonic_file", value="new.m")
    assert same_value.returncode == 0, same_value.stderr.decode()
    assert config_path.stat().st_ino == before.st_ino


def test_set_relocates_a_key_written_after_the_chapter_banner(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    broken = _template_shaped_config().replace(
        "\n[logging]\n", 'mnemonic_file = "wallet.m"\n\n[logging]\n'
    )
    assert broken.index('mnemonic_file = "wallet.m"') > broken.index("# Logging Settings")
    config_path.write_text(broken)

    result = _run_helper("set", config_path, "wallet", "mnemonic_file", value="wallet.m")

    assert result.returncode == 0, result.stderr.decode()
    config_text = config_path.read_text()
    assert config_text.count('mnemonic_file = "wallet.m"') == 1
    assert config_text.index('mnemonic_file = "wallet.m"') < config_text.index("# Logging Settings")
    parsed = tomlkit.parse(config_text)
    assert parsed["wallet"]["mnemonic_file"] == "wallet.m"
    assert parsed["logging"]["level"] == "INFO"


def test_set_get_and_remove_boolean_values(tmp_path: Path) -> None:
    """Booleans round-trip as TOML booleans with a stable text rendering."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("[wallet]\n")

    assert (
        _run_helper(
            "set", config_path, "wallet", "wallet_with_passphrase", value="true", as_bool=True
        ).returncode
        == 0
    )
    config_text = config_path.read_text()
    assert "wallet_with_passphrase = true" in config_text
    assert tomlkit.parse(config_text)["wallet"]["wallet_with_passphrase"] is True
    assert _run_helper("get", config_path, "wallet", "wallet_with_passphrase").stdout == b"true"

    # A trailing newline from the stdin producer is tolerated.
    assert (
        _run_helper(
            "set", config_path, "wallet", "wallet_with_passphrase", value="false\n", as_bool=True
        ).returncode
        == 0
    )
    assert tomlkit.parse(config_path.read_text())["wallet"]["wallet_with_passphrase"] is False
    assert _run_helper("get", config_path, "wallet", "wallet_with_passphrase").stdout == b"false"

    result = _run_helper("remove", config_path, "wallet", "wallet_with_passphrase")
    assert result.returncode == 0, result.stderr.decode()
    assert "wallet_with_passphrase" not in config_path.read_text()


def test_boolean_values_reject_invalid_input_and_type_changes(tmp_path: Path) -> None:
    """--bool accepts only 'true'/'false' and never changes a key's type."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[wallet]\nflag = true\nname = "value"\n')

    invalid = _run_helper("set", config_path, "wallet", "flag", value="yes", as_bool=True)
    assert invalid.returncode == 2
    assert config_path.read_text() == '[wallet]\nflag = true\nname = "value"\n'

    # String write to a boolean key and boolean write to a string key both fail.
    assert _run_helper("set", config_path, "wallet", "flag", value="false").returncode == 2
    assert (
        _run_helper("set", config_path, "wallet", "name", value="true", as_bool=True).returncode
        == 2
    )
    assert config_path.read_text() == '[wallet]\nflag = true\nname = "value"\n'
