"""Coupled setup edits validate one candidate and never overwrite detected conflicts."""

from __future__ import annotations

import os
import stat
import tomllib
import traceback
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from jmcore import config_file
from jmcore.config_file import ConfigFileError, update_config_values

ORIGINAL = """# Keep this installation's notes.
[wallet]
address_type = "p2wpkh" # Existing pit

[lightning]
mode = "off"

[tor]
socks_port = 9050
"""


def validate_setup(candidate: Any) -> None:
    if (
        candidate.get("lightning", {}).get("mode") == "managed"
        and candidate.get("wallet", {}).get("address_type", "p2wpkh") != "p2tr"
    ):
        raise ValueError("managed mode requires Taproot")


def test_coupled_values_are_validated_together_before_one_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(ORIGINAL)
    calls = []

    def validate(candidate: Any) -> None:
        assert path.read_text() == ORIGINAL
        validate_setup(candidate)
        calls.append(candidate)

    real_write = config_file.atomic_write_private
    write = Mock(wraps=real_write)
    monkeypatch.setattr(config_file, "atomic_write_private", write)
    update_config_values(
        path,
        {
            "lightning": {"mode": "managed"},
            "wallet": {"address_type": "p2tr"},
            "tor": {"socks_port": 19050},
        },
        validate=validate,
    )
    write.assert_called_once()
    assert len(calls) == 1
    result = path.read_text()
    assert "# Keep this installation's notes." in result and "# Existing pit" in result
    parsed = tomllib.loads(result)
    assert parsed["lightning"]["mode"] == "managed"
    assert parsed["wallet"]["address_type"] == "p2tr"
    assert parsed["tor"]["socks_port"] == 19050


def test_invalid_candidate_never_persists_an_intermediate_edit(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(ORIGINAL)
    with pytest.raises(ConfigFileError, match="candidate validation failed"):
        update_config_values(path, {"lightning": {"mode": "managed"}}, validate=validate_setup)
    assert path.read_text() == ORIGINAL


def test_validator_defaults_and_mutations_are_not_materialized(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"

    def validate(candidate: Any) -> None:
        candidate["wallet"]["address_type"] = "validator-mutated"
        candidate["defaulted"] = {"secret": "must not be written"}

    update_config_values(path, {"wallet": {"address_type": "p2wpkh"}}, validate=validate)
    assert tomllib.loads(path.read_text()) == {"wallet": {"address_type": "p2wpkh"}}


@pytest.mark.parametrize("updates", [{}, {"wallet": {"address_type": "p2wpkh"}}])
def test_noop_still_validates_but_does_not_replace_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    updates: Any,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(ORIGINAL)
    before = path.stat().st_ino
    validate, write = Mock(), Mock()
    monkeypatch.setattr(config_file, "atomic_write_private", write)
    update_config_values(path, updates, validate=validate)
    validate.assert_called_once()
    write.assert_not_called()
    assert path.read_text() == ORIGINAL and path.stat().st_ino == before


def test_missing_noop_config_is_not_created_or_a_wallet_freshness_assertion(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "config.toml"
    validate = Mock()
    update_config_values(path, {}, validate=validate)
    validate.assert_called_once_with({})
    assert not path.parent.exists()


@pytest.mark.parametrize(
    "original, value",
    [
        ("true", 1),
        ("1", True),
        ("1", 1.0),
        ("1.0", 1),
        ('"text"', False),
    ],
)
def test_equal_numeric_boolean_values_do_not_bypass_type_stability(
    tmp_path: Path,
    original: str,
    value: Any,
) -> None:
    path = tmp_path / "config.toml"
    before = f"[example]\nvalue = {original}\n"
    path.write_text(before)
    with pytest.raises(ConfigFileError, match="config value is a"):
        update_config_values(path, {"example": {"value": value}}, validate=lambda candidate: None)
    assert path.read_text() == before


def test_banner_repair_is_not_skipped_for_equal_values(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("""[wallet]
# =====
# Lightning Settings
# =====
address_type = "p2tr"
[lightning]
mode = "off"
""")
    update_config_values(path, {"wallet": {"address_type": "p2tr"}}, validate=validate_setup)
    result = path.read_text()
    assert result.index("address_type") < result.index("# Lightning Settings")
    assert tomllib.loads(result)["wallet"]["address_type"] == "p2tr"


@pytest.mark.parametrize("conflict", ["edit", "replace-same-bytes", "missing-to-present"])
def test_detected_conflicts_are_not_retried_or_overwritten(tmp_path: Path, conflict: str) -> None:
    path = tmp_path / "config.toml"
    if conflict != "missing-to-present":
        path.write_text(ORIGINAL)
    external = '# External editor\n[wallet]\naddress_type = "p2tr"\n'

    def validate(candidate: Any) -> None:
        if conflict == "replace-same-bytes":
            replacement = tmp_path / "replacement.toml"
            replacement.write_text(ORIGINAL)
            replacement.replace(path)
        else:
            path.write_text(external)

    with pytest.raises(ConfigFileError, match="changed during editing"):
        update_config_values(path, {"wallet": {"address_type": "p2tr"}}, validate=validate)
    assert path.read_text() == (ORIGINAL if conflict == "replace-same-bytes" else external)


@pytest.mark.skipif(
    os.name == "nt", reason="configured symlink aliases require POSIX test privileges"
)
def test_alias_and_original_target_are_preserved(tmp_path: Path) -> None:
    target = tmp_path / "real.toml"
    target.write_text(ORIGINAL)
    alias = tmp_path / "config.toml"
    alias.symlink_to(target)
    update_config_values(alias, {"wallet": {"address_type": "p2tr"}}, validate=validate_setup)
    assert alias.is_symlink() and alias.resolve() == target
    assert tomllib.loads(target.read_text())["wallet"]["address_type"] == "p2tr"


@pytest.mark.skipif(
    os.name == "nt", reason="configured symlink aliases require POSIX test privileges"
)
def test_retargeting_never_redirects_a_transaction_or_reads_the_new_target(tmp_path: Path) -> None:
    target, other, alias = (tmp_path / name for name in ("real.toml", "other.toml", "config.toml"))
    target.write_text(ORIGINAL)
    other.write_text(ORIGINAL)
    other.chmod(0o644)
    alias.symlink_to(target)

    def validate(candidate: Any) -> None:
        alias.unlink()
        alias.symlink_to(other)

    with pytest.raises(ConfigFileError, match="alias changed"):
        update_config_values(alias, {"wallet": {"address_type": "p2tr"}}, validate=validate)
    assert target.read_text() == other.read_text() == ORIGINAL
    assert stat.S_IMODE(other.stat().st_mode) == 0o644
    assert alias.resolve() == other


def test_callback_errors_do_not_expose_sensitive_values(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    secret = "private-config-value"

    def validate(candidate: Any) -> None:
        raise ValueError(secret)

    with pytest.raises(ConfigFileError) as error:
        update_config_values(path, {"bitcoin": {"rpc_password": secret}}, validate=validate)
    assert secret not in str(error.value)
    assert secret not in "".join(traceback.format_exception(error.value))
    assert not path.exists()
