"""CLI-level tests for :mod:`jmtumbler.cli`.

These exercise the thin wrapper behaviours that surround the runner: option
validation, wallet-name defaulting from the mnemonic, and neutrino fee
handling. Everything else (planning, running, persistence) already has
dedicated unit coverage and is intentionally not re-exercised here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from jmtumbler.builder import PlanBuilder, TumbleParameters
from jmtumbler.cli import app
from jmtumbler.persistence import save_plan
from jmtumbler.plan import Plan

runner = CliRunner()


def _build_plan(wallet_name: str) -> Plan:
    params = TumbleParameters(
        destinations=["bcrt1qdest0000000000000000000000000000000000abc"],
        mixdepth_balances={0: 1_000_000, 1: 500_000},
        seed=1,
    )
    return PlanBuilder(wallet_name, params).build()


class _FakeSettings:
    """Minimal ``settings`` stand-in for :func:`jmtumbler.cli._resolve_wallet_name`.

    We only touch ``get_data_dir`` and ``network_config.network``; the
    neutrino-path test additionally touches ``bitcoin.backend_type``.
    """

    class _Net:
        def __init__(self, network: str) -> None:
            class _N:
                value = network

            self.network = _N()

    class _Bitcoin:
        def __init__(self, backend_type: str) -> None:
            self.backend_type = backend_type

    def __init__(self, data_dir: Path, network: str = "regtest", backend: str = "") -> None:
        self._data_dir = data_dir
        self.network_config = self._Net(network)
        self.bitcoin = self._Bitcoin(backend)

    def get_data_dir(self) -> Path:
        return self._data_dir


class TestStatusDefaultsWalletFromMnemonic:
    def test_resolves_wallet_from_mnemonic_fingerprint(self, tmp_path: Path) -> None:
        wallet_name = "jm_abc12345_regtest"
        plan = _build_plan(wallet_name)
        save_plan(plan, tmp_path)
        settings = _FakeSettings(tmp_path)

        class _Resolved:
            mnemonic = "abandon " * 11 + "about"
            bip39_passphrase = ""

        with (
            patch("jmtumbler.cli.setup_cli", return_value=settings),
            patch("jmtumbler.cli.resolve_mnemonic", return_value=_Resolved()),
            patch("jmtumbler.cli._wallet_name_from_mnemonic", return_value=wallet_name),
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0, result.stdout
        assert wallet_name in result.stdout

    def test_reports_error_when_no_wallet_and_no_mnemonic(self, tmp_path: Path) -> None:
        settings = _FakeSettings(tmp_path)
        with (
            patch("jmtumbler.cli.setup_cli", return_value=settings),
            patch("jmtumbler.cli.resolve_mnemonic", return_value=None),
        ):
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 1


class TestDeleteDefaultsWalletFromMnemonic:
    def test_resolves_wallet_from_mnemonic_fingerprint(self, tmp_path: Path) -> None:
        wallet_name = "jm_abc12345_regtest"
        plan = _build_plan(wallet_name)
        save_plan(plan, tmp_path)
        settings = _FakeSettings(tmp_path)

        class _Resolved:
            mnemonic = "abandon " * 11 + "about"
            bip39_passphrase = ""

        with (
            patch("jmtumbler.cli.setup_cli", return_value=settings),
            patch("jmtumbler.cli.resolve_mnemonic", return_value=_Resolved()),
            patch("jmtumbler.cli._wallet_name_from_mnemonic", return_value=wallet_name),
        ):
            result = runner.invoke(app, ["delete", "--yes"])

        assert result.exit_code == 0, result.stdout
        assert "Deleted" in result.stdout
        assert not (tmp_path / "schedules" / f"{wallet_name}.yaml").exists()


class TestRunFeeOptions:
    def test_rejects_fee_rate_with_block_target(self, tmp_path: Path) -> None:
        settings = _FakeSettings(tmp_path)

        with (
            patch("jmtumbler.cli.setup_cli", return_value=settings),
            patch("jmtumbler.cli.ensure_config_file"),
            patch("jmtumbler.cli.resolve_mnemonic") as m_resolve,
        ):
            result = runner.invoke(
                app, ["run", "-w", "w", "--fee-rate", "2", "--block-target", "6"]
            )
        assert result.exit_code == 1
        # Mutex guard must short-circuit before touching the mnemonic.
        m_resolve.assert_not_called()

    def test_rejects_neutrino_without_fee_rate(self, tmp_path: Path) -> None:
        settings = _FakeSettings(tmp_path, backend="neutrino")

        with (
            patch("jmtumbler.cli.setup_cli", return_value=settings),
            patch("jmtumbler.cli.ensure_config_file"),
            patch("jmtumbler.cli.resolve_mnemonic") as m_resolve,
        ):
            result = runner.invoke(app, ["run", "-w", "w", "--backend", "neutrino"])
        assert result.exit_code == 1
        # Neutrino guard must short-circuit before touching the mnemonic.
        m_resolve.assert_not_called()

    def test_accepts_neutrino_with_fee_rate(self, tmp_path: Path) -> None:
        # When --fee-rate is supplied on neutrino, the guard must pass and
        # execution must progress past mnemonic resolution into plan loading.
        settings = _FakeSettings(tmp_path, backend="neutrino")

        class _Resolved:
            mnemonic = "abandon " * 11 + "about"
            bip39_passphrase = ""
            creation_height = None

        with (
            patch("jmtumbler.cli.setup_cli", return_value=settings),
            patch("jmtumbler.cli.ensure_config_file"),
            patch("jmtumbler.cli.resolve_mnemonic", return_value=_Resolved()) as m_resolve,
            patch("jmtumbler.cli._wallet_name_from_mnemonic", return_value="w"),
        ):
            result = runner.invoke(app, ["run", "--backend", "neutrino", "--fee-rate", "2"])
        # Plan does not exist → _load_or_error exits 1, but only *after* the
        # guard accepts the configuration.
        assert result.exit_code == 1
        m_resolve.assert_called_once()
