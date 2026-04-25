"""CLI-level tests for :mod:`tumbler.cli`.

These exercise the thin wrapper behaviours that surround the runner: option
validation, wallet-name defaulting from the mnemonic, and neutrino fee
handling. Everything else (planning, running, persistence) already has
dedicated unit coverage and is intentionally not re-exercised here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from tumbler.builder import PlanBuilder, TumbleParameters
from tumbler.cli import app
from tumbler.persistence import save_plan
from tumbler.plan import Plan

runner = CliRunner()


def _unused_balances(*args: object, **kwargs: object) -> None:
    return None


def _build_plan(wallet_name: str) -> Plan:
    params = TumbleParameters(
        destinations=["bcrt1qdest0000000000000000000000000000000000abc"],
        mixdepth_balances={0: 1_000_000, 1: 500_000},
        seed=1,
    )
    return PlanBuilder(wallet_name, params).build()


class _FakeSettings:
    """Minimal ``settings`` stand-in for :func:`tumbler.cli._resolve_wallet_name`.

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
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.resolve_mnemonic", return_value=_Resolved()),
            patch("tumbler.cli._wallet_name_from_mnemonic", return_value=wallet_name),
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0, result.stdout
        assert wallet_name in result.stdout

    def test_reports_error_when_no_wallet_and_no_mnemonic(self, tmp_path: Path) -> None:
        settings = _FakeSettings(tmp_path)
        with (
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.resolve_mnemonic", return_value=None),
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
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.resolve_mnemonic", return_value=_Resolved()),
            patch("tumbler.cli._wallet_name_from_mnemonic", return_value=wallet_name),
        ):
            result = runner.invoke(app, ["delete", "--yes"])

        assert result.exit_code == 0, result.stdout
        assert "Deleted" in result.stdout
        assert not (tmp_path / "schedules" / f"{wallet_name}.yaml").exists()


class TestRunFeeOptions:
    def test_rejects_fee_rate_with_block_target(self, tmp_path: Path) -> None:
        settings = _FakeSettings(tmp_path)

        with (
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.ensure_config_file"),
            patch("tumbler.cli.resolve_mnemonic") as m_resolve,
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
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.ensure_config_file"),
            patch("tumbler.cli.resolve_mnemonic") as m_resolve,
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
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.ensure_config_file"),
            patch("tumbler.cli.resolve_mnemonic", return_value=_Resolved()) as m_resolve,
            patch("tumbler.cli._wallet_name_from_mnemonic", return_value="w"),
        ):
            result = runner.invoke(app, ["run", "--backend", "neutrino", "--fee-rate", "2"])
        # Plan does not exist → _load_or_error exits 1, but only *after* the
        # guard accepts the configuration.
        assert result.exit_code == 1
        m_resolve.assert_called_once()


class TestRunCounterpartiesOption:
    def test_counterparties_flag_is_accepted(self, tmp_path: Path) -> None:
        # --counterparties plumbs through option parsing without tripping
        # the fee or backend guards. Plan-load still fails (no plan on disk)
        # but the option must at least be recognised by typer.
        settings = _FakeSettings(tmp_path, backend="neutrino")

        class _Resolved:
            mnemonic = "abandon " * 11 + "about"
            bip39_passphrase = ""
            creation_height = None

        with (
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.ensure_config_file"),
            patch("tumbler.cli.resolve_mnemonic", return_value=_Resolved()),
            patch("tumbler.cli._wallet_name_from_mnemonic", return_value="w"),
        ):
            result = runner.invoke(
                app,
                [
                    "run",
                    "--backend",
                    "neutrino",
                    "--fee-rate",
                    "2",
                    "--counterparties",
                    "3",
                ],
            )
        # Plan doesn't exist → exits 1 after option parsing, but typer must
        # not reject the flag itself.
        assert result.exit_code == 1
        assert "No such option" not in result.stdout

    def test_counterparties_rejects_out_of_range(self, tmp_path: Path) -> None:
        settings = _FakeSettings(tmp_path, backend="neutrino")
        with (
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.ensure_config_file"),
        ):
            result = runner.invoke(
                app,
                [
                    "run",
                    "--backend",
                    "neutrino",
                    "--fee-rate",
                    "2",
                    "--counterparties",
                    "99",
                ],
            )
        assert result.exit_code != 0


class TestPlanDefaultsCounterpartyFromSettings:
    def test_maker_count_defaults_pull_from_settings(self, tmp_path: Path) -> None:
        """Without --maker-count-min/--max, the plan uses settings.taker.counterparty_count."""

        class _Taker:
            counterparty_count = 4

        settings = _FakeSettings(tmp_path)
        settings.taker = _Taker()  # type: ignore[attr-defined]

        class _Resolved:
            mnemonic = "abandon " * 11 + "about"
            bip39_passphrase = ""
            creation_height = None

        captured: dict[str, TumbleParameters] = {}

        class _FakeBuilder:
            def __init__(self, wallet_name: str, params: TumbleParameters) -> None:
                captured["params"] = params
                self.params = params
                self.wallet_name = wallet_name

            def build(self):  # type: ignore[no-untyped-def]
                from tumbler.builder import PlanBuilder

                return PlanBuilder(self.wallet_name, self.params).build()

        with (
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.ensure_config_file"),
            patch("tumbler.cli.resolve_mnemonic", return_value=_Resolved()),
            patch("tumbler.cli._wallet_name_from_mnemonic", return_value="w"),
            patch("tumbler.cli._balances_for_mnemonic", new=_unused_balances),
            patch("tumbler.cli.PlanBuilder", _FakeBuilder),
        ):
            # _balances_for_mnemonic is executed inside asyncio.run; stub the
            # run result directly so the CLI sees the expected balance map.
            with patch("tumbler.cli.asyncio.run", return_value={0: 1_000_000, 1: 0}):
                result = runner.invoke(
                    app,
                    [
                        "plan",
                        "-w",
                        "w",
                        "--destination",
                        "bcrt1qdest0000000000000000000000000000000000abc",
                    ],
                )
        assert result.exit_code == 0, result.stdout
        params = captured["params"]
        assert params.maker_count_min == 4
        assert params.maker_count_max == 4


class TestPlanSingleFundedMixdepth:
    def test_accepts_two_destinations_when_only_one_mixdepth_is_funded(
        self, tmp_path: Path
    ) -> None:
        settings = _FakeSettings(tmp_path, network="signet")

        class _Taker:
            counterparty_count = 4

        settings.taker = _Taker()  # type: ignore[attr-defined]

        class _Resolved:
            mnemonic = "abandon " * 11 + "about"
            bip39_passphrase = ""
            creation_height = None

        with (
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.ensure_config_file"),
            patch("tumbler.cli.resolve_mnemonic", return_value=_Resolved()),
            patch("tumbler.cli._wallet_name_from_mnemonic", return_value="default"),
            patch("tumbler.cli._balances_for_mnemonic", new=_unused_balances),
            patch(
                "tumbler.cli.asyncio.run",
                return_value={0: 0, 1: 23_430_165, 2: 0, 3: 0, 4: 0},
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "plan",
                    "-w",
                    "default",
                    "-d",
                    "tb1qcfyfz4z5nwq0fk6qqjh6h74rsfghqtn5mgn2fj",
                    "-d",
                    "tb1qc60pcxcupzw589hwq0fcjamatsvg39k5q2el82",
                ],
            )

        assert result.exit_code == 0, result.stdout
        assert "Plan written to" in result.stdout
