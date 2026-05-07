"""Tests for the central protocol command registry."""

from __future__ import annotations

import pytest

from jmcore.protocol_commands import (
    COMMAND_SPECS,
    LEGACY_COMMANDS,
    TX_EXTENSION_COMMANDS,
    ZKP_COMMANDS,
    Command,
    Direction,
    FeatureGate,
    is_feature_enabled,
    parse_command,
    strip_prefix,
    with_prefix,
)


class TestCommandEnum:
    def test_legacy_command_values_are_unprefixed(self) -> None:
        assert Command.FILL.value == "fill"
        assert Command.IOAUTH.value == "ioauth"
        assert Command.ORDERBOOK.value == "orderbook"

    def test_all_commands_have_specs(self) -> None:
        """Every Command member must have a CommandSpec entry."""
        missing = [c for c in Command if c not in COMMAND_SPECS]
        assert not missing, f"Commands missing specs: {missing}"

    def test_no_duplicate_values(self) -> None:
        """No two Command members may share a wire value."""
        values = [c.value for c in Command]
        assert len(values) == len(set(values))


class TestSubsets:
    def test_legacy_subset(self) -> None:
        assert Command.FILL in LEGACY_COMMANDS
        assert Command.ORDERBOOK in LEGACY_COMMANDS
        assert Command.ZKPREQ not in LEGACY_COMMANDS

    def test_zkp_subset_has_six_commands(self) -> None:
        # JMP-0005: zkpparams, zkpissue_in, zkpcred_in, zkpreq, zkpcred, zkpreg
        assert len(ZKP_COMMANDS) == 6
        assert Command.ZKPREQ in ZKP_COMMANDS
        assert Command.ZKPREG in ZKP_COMMANDS

    def test_tx_extension_subset_has_five_commands(self) -> None:
        # JMP-0006: cjext, txext, sigext, txfreeze, sigfinal
        assert len(TX_EXTENSION_COMMANDS) == 5
        assert Command.CJEXT in TX_EXTENSION_COMMANDS
        assert Command.SIGFINAL in TX_EXTENSION_COMMANDS

    def test_subsets_partition_all_commands(self) -> None:
        union = LEGACY_COMMANDS | ZKP_COMMANDS | TX_EXTENSION_COMMANDS
        assert union == set(Command)
        # Disjoint
        assert not LEGACY_COMMANDS & ZKP_COMMANDS
        assert not LEGACY_COMMANDS & TX_EXTENSION_COMMANDS
        assert not ZKP_COMMANDS & TX_EXTENSION_COMMANDS


class TestCommandSpecs:
    def test_pubkey_is_unencrypted(self) -> None:
        # Pubkey announcement bootstraps encryption, so it must be plaintext.
        assert COMMAND_SPECS[Command.PUBKEY].encrypted is False

    def test_ioauth_is_encrypted(self) -> None:
        assert COMMAND_SPECS[Command.IOAUTH].encrypted is True

    def test_orderbook_is_public_broadcast(self) -> None:
        spec = COMMAND_SPECS[Command.ORDERBOOK]
        assert spec.direction is Direction.PUBLIC
        assert spec.broadcast is True

    def test_zkpreg_is_public_broadcast(self) -> None:
        spec = COMMAND_SPECS[Command.ZKPREG]
        assert spec.direction is Direction.PUBLIC
        assert spec.broadcast is True
        assert spec.feature is FeatureGate.ZKP

    @pytest.mark.parametrize("cmd", list(ZKP_COMMANDS))
    def test_all_zkp_commands_gated(self, cmd: Command) -> None:
        assert COMMAND_SPECS[cmd].feature is FeatureGate.ZKP

    @pytest.mark.parametrize("cmd", list(TX_EXTENSION_COMMANDS))
    def test_all_extension_commands_gated(self, cmd: Command) -> None:
        assert COMMAND_SPECS[cmd].feature is FeatureGate.TX_EXTENSION


class TestPrefixHelpers:
    def test_with_prefix_adds_bang(self) -> None:
        assert with_prefix(Command.FILL) == "!fill"
        assert with_prefix("auth") == "!auth"

    def test_with_prefix_idempotent(self) -> None:
        assert with_prefix("!fill") == "!fill"

    def test_strip_prefix(self) -> None:
        assert strip_prefix("!fill") == "fill"
        assert strip_prefix("fill") == "fill"


class TestParseCommand:
    def test_parses_known_command(self) -> None:
        assert parse_command("!fill") is Command.FILL
        assert parse_command("fill") is Command.FILL

    def test_parses_command_with_args(self) -> None:
        assert parse_command("!fill 0 100000 abc") is Command.FILL
        assert parse_command("zkpreq 12345 abc def") is Command.ZKPREQ

    def test_returns_none_for_unknown(self) -> None:
        assert parse_command("!unknown") is None
        assert parse_command("xyzzy") is None


class TestFeatureGate:
    def test_legacy_always_enabled(self) -> None:
        assert is_feature_enabled(Command.FILL, zkp_enabled=False, tx_extension_enabled=False)

    def test_zkp_requires_zkp(self) -> None:
        assert not is_feature_enabled(Command.ZKPREQ, zkp_enabled=False, tx_extension_enabled=False)
        assert is_feature_enabled(Command.ZKPREQ, zkp_enabled=True, tx_extension_enabled=False)

    def test_extension_requires_zkp_and_extension(self) -> None:
        # Extension without ZKP is invalid even if extension flag is on.
        assert not is_feature_enabled(Command.CJEXT, zkp_enabled=False, tx_extension_enabled=True)
        # ZKP alone isn't enough for extension commands.
        assert not is_feature_enabled(Command.CJEXT, zkp_enabled=True, tx_extension_enabled=False)
        assert is_feature_enabled(Command.CJEXT, zkp_enabled=True, tx_extension_enabled=True)
