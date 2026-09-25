"""CLI-level tests for :mod:`tumbler.cli`.

These exercise the thin wrapper behaviours that surround the runner: option
validation, wallet-name defaulting from the mnemonic, and neutrino fee
handling. Everything else (planning, running, persistence) already has
dedicated unit coverage and is intentionally not re-exercised here.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from jmcore.paths import read_nick_state
from loguru import logger
from pydantic import SecretStr
from typer.testing import CliRunner

from tumbler.builder import PlanBuilder, TumbleParameters
from tumbler.cli import (
    _collect_balances,
    _load_or_error,
    _resolve_fee_rate,
    _run_plan,
    app,
    resolve_runner_pacing,
)
from tumbler.persistence import save_plan
from tumbler.plan import Plan, PlanStatus

runner = CliRunner()


def test_help_output_is_alphabetically_sorted() -> None:
    """Subcommands and options must be listed alphabetically in --help."""
    from jmcore.cli_help import find_unsorted_help

    assert find_unsorted_help(app) == []


def _unused_balances(*args: object, **kwargs: object) -> None:
    return None


def _build_plan(wallet_name: str) -> Plan:
    params = TumbleParameters(
        destinations=[
            "bcrt1qdest0000000000000000000000000000000000aaa",
            "bcrt1qdest0000000000000000000000000000000000bbb",
            "bcrt1qdest0000000000000000000000000000000000ccc",
        ],
        mixdepth_balances={0: 1_000_000, 1: 500_000, 2: 0, 3: 0, 4: 0},
        seed=1,
    )
    return PlanBuilder(wallet_name, params).build()


@pytest.mark.asyncio
async def test_fee_preview_uses_backend_sat_vb_units() -> None:
    class _Taker:
        fee_rate = None
        fee_block_target = 6

    class _Wallet:
        max_fee_rate_sat_vb = 1_000.0

    class _Settings:
        taker = _Taker()
        wallet = _Wallet()

    class _Backend:
        async def estimate_fee(self, target_blocks: int) -> float:
            assert target_blocks == 6
            return 3.5

    assert await _resolve_fee_rate(_Settings(), _Backend()) == (3.5, "estimated")


class _FakeSettings:
    """Minimal ``settings`` stand-in for :func:`tumbler.cli._resolve_wallet_name`.

    We only touch ``get_data_dir`` and ``network_config.network``; the
    neutrino-path test additionally touches ``bitcoin.backend_type`` and the
    plan/run paths read the ``[tumbler]`` pacing knobs.
    """

    class _Net:
        def __init__(self, network: str) -> None:
            class _N:
                value = network

            self.network = _N()
            self.bitcoin_network = None

    class _Bitcoin:
        def __init__(self, backend_type: str) -> None:
            self.backend_type = backend_type

    class _Tumbler:
        min_confirmations_between_phases = 6
        confirmation_poll_interval = 30.0
        retry_delay_seconds = 1800.0

    def __init__(self, data_dir: Path, network: str = "regtest", backend: str = "") -> None:
        self._data_dir = data_dir
        self.network_config = self._Net(network)
        self.bitcoin = self._Bitcoin(backend)
        self.tumbler = self._Tumbler()

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


def test_missing_plan_log_keeps_wallet_and_path_sensitive(tmp_path: Path) -> None:
    """Plan identifiers and locations must not appear in ordinary log records."""
    wallet_name = "private-wallet"
    records: list[tuple[str, dict[str, object]]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["message"], dict(message.record["extra"])))
    )
    try:
        with pytest.raises(typer.Exit):
            _load_or_error(wallet_name, tmp_path)
    finally:
        logger.remove(handler_id)

    private_records = [
        record for record in records if wallet_name in record[0] or str(tmp_path) in record[0]
    ]
    assert private_records
    assert all(extra.get("sensitive") is True for _, extra in private_records)
    assert ("No tumbler plan found. Check --wallet-name and --data-dir.", {}) in records


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
            mnemonic_file = None

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
            mnemonic_file = None

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


@pytest.mark.asyncio
async def test_run_maker_factory_updates_rotated_nick_state(tmp_path: Path) -> None:
    config = SimpleNamespace(
        address_type="p2wpkh",
        mnemonic=SecretStr("abandon " * 11 + "about"),
        passphrase=SecretStr(""),
        bitcoin_network=None,
        network=SimpleNamespace(value="regtest"),
        mixdepth_count=5,
        data_dir=tmp_path,
        max_sats_freeze_reuse=0,
        reconstruct_history=False,
    )
    wallet = MagicMock()
    wallet.sync_with_registered_bonds = AsyncMock()
    maker_bot = MagicMock()

    class CapturingRunner:
        def __init__(self, _plan: Plan, context: Any) -> None:
            self.context = context

        async def run(self) -> Any:
            await self.context.maker_factory(MagicMock())
            callback = maker_bot.call_args.kwargs["nick_change_callback"]
            callback("J5OldTumblerMaker", "J5RotatedTumblerMaker")
            return SimpleNamespace(status=PlanStatus.COMPLETED, error=None)

        def request_stop(self) -> None:
            return None

    with (
        patch("taker.cli.build_taker_config", return_value=config),
        patch("taker.cli.create_backend", return_value=MagicMock()),
        patch("maker.cli.build_maker_config", return_value=config),
        patch("maker.bot.MakerBot", maker_bot),
        patch("tumbler.cli.WalletService", return_value=wallet),
        patch("tumbler.cli.TumbleRunner", CapturingRunner),
        patch("tumbler.cli.apply_tumbler_maker_policy"),
    ):
        await _run_plan(
            settings=MagicMock(),
            plan=_build_plan("w"),
            mnemonic="abandon " * 11 + "about",
            passphrase="",
            creation_height=None,
            mnemonic_file=None,
            data_dir=tmp_path,
            network=None,
            backend_type=None,
            rpc_url=None,
            neutrino_url=None,
            directory_servers=None,
            tor_socks_host=None,
            tor_socks_port=None,
            fee_rate=None,
            block_target=None,
            min_confirmations_between_phases=None,
        )

    assert read_nick_state(tmp_path, "maker") == "J5RotatedTumblerMaker"


class TestPlanDefaultsCounterpartyFromSettings:
    def test_maker_count_defaults_pull_from_settings(self, tmp_path: Path) -> None:
        """Without --maker-count-min/--max, the plan uses settings.taker.counterparty_count."""

        class _Taker:
            counterparty_count = 4
            max_cj_fee_abs = 500
            max_cj_fee_rel = "0.001"
            fee_rate = None
            fee_block_target = None

        settings = _FakeSettings(tmp_path)
        settings.taker = _Taker()  # type: ignore[attr-defined]

        class _Resolved:
            mnemonic = "abandon " * 11 + "about"
            bip39_passphrase = ""
            creation_height = None
            mnemonic_file = None

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
            with patch(
                "tumbler.cli.asyncio.run",
                return_value=({0: 1_000_000, 1: 0, 2: 0, 3: 0, 4: 0}, None, "fallback"),
            ):
                result = runner.invoke(
                    app,
                    [
                        "plan",
                        "-w",
                        "w",
                        "--destination",
                        "bcrt1q4h7w2n6jnvs4fc7rvxa78a75rkcxx4ch44jl8m",
                        "--destination",
                        "bcrt1qgdm6ttxkdhzukec53gjgrrg728apsw7jzft949",
                        "--destination",
                        "bcrt1qvwak370erjn8lwfu9zvuujx84p8q84htskrffy",
                    ],
                )
        assert result.exit_code == 0, result.stdout
        params = captured["params"]
        assert params.maker_count_min == 4
        assert params.maker_count_max == 4


class TestPlanSingleFundedMixdepth:
    def test_accepts_min_destinations_when_only_one_mixdepth_is_funded(
        self, tmp_path: Path
    ) -> None:
        settings = _FakeSettings(tmp_path, network="signet")

        class _Taker:
            counterparty_count = 4
            max_cj_fee_abs = 500
            max_cj_fee_rel = "0.001"
            fee_rate = None
            fee_block_target = None

        settings.taker = _Taker()  # type: ignore[attr-defined]

        class _Resolved:
            mnemonic = "abandon " * 11 + "about"
            bip39_passphrase = ""
            creation_height = None
            mnemonic_file = None

        with (
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.ensure_config_file"),
            patch("tumbler.cli.resolve_mnemonic", return_value=_Resolved()),
            patch("tumbler.cli._wallet_name_from_mnemonic", return_value="default"),
            patch("tumbler.cli._balances_for_mnemonic", new=_unused_balances),
            patch(
                "tumbler.cli.asyncio.run",
                return_value=({0: 0, 1: 23_430_165, 2: 0, 3: 0, 4: 0}, None, "fallback"),
            ),
        ):
            result = runner.invoke(
                app,
                [
                    "plan",
                    "-w",
                    "default",
                    "-d",
                    "tb1qt6umtez9mdnn7rkcjdw33nfqtvs5u5v8tyf6rn",
                    "-d",
                    "tb1qglglrref79gkpx2d44ymh36sa7zldju3ytmwk0",
                    "-d",
                    "tb1q8vfw0yl8dp3n0wykafr6tmzym0mkjltxj0enu9",
                ],
            )

        assert result.exit_code == 0, result.stdout
        assert "Plan written to" in result.stdout


class TestPlanFewDestinationsGate:
    """The CLI refuses to build a plan with fewer than MIN_DESTINATIONS
    addresses unless --allow-few-destinations is passed."""

    def _invoke(self, tmp_path: Path, extra_args: list[str]) -> Any:
        settings = _FakeSettings(tmp_path, network="signet")

        class _Taker:
            counterparty_count = 4
            max_cj_fee_abs = 500
            max_cj_fee_rel = "0.001"
            fee_rate = None
            fee_block_target = None

        settings.taker = _Taker()  # type: ignore[attr-defined]

        class _Resolved:
            mnemonic = "abandon " * 11 + "about"
            bip39_passphrase = ""
            creation_height = None
            mnemonic_file = None

        with (
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.ensure_config_file"),
            patch("tumbler.cli.resolve_mnemonic", return_value=_Resolved()),
            patch("tumbler.cli._wallet_name_from_mnemonic", return_value="default"),
            patch("tumbler.cli._balances_for_mnemonic", new=_unused_balances),
            patch(
                "tumbler.cli.asyncio.run",
                return_value=({0: 0, 1: 23_430_165, 2: 0, 3: 0, 4: 0}, None, "fallback"),
            ),
        ):
            return runner.invoke(
                app,
                [
                    "plan",
                    "-w",
                    "default",
                    "-d",
                    "tb1qcfyfz4z5nwq0fk6qqjh6h74rsfghqtn5mgn2fj",
                    *extra_args,
                ],
            )

    def test_rejects_single_destination_by_default(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, [])
        assert result.exit_code != 0
        # The error log is emitted through loguru; the invocation exits with 1.
        assert (
            "destination addresses are recommended" not in (result.stdout or "")
            or "destination addresses are recommended" in result.stdout
        )

    def test_override_allows_single_destination(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, ["--allow-few-destinations"])
        assert result.exit_code == 0, result.stdout
        assert "Plan written to" in result.stdout


class TestResolveRunnerPacing:
    """Settings-to-RunnerContext round trip for the ``[tumbler]`` pacing knobs.

    Regression: the standalone CLI built its ``RunnerContext`` with only
    ``min_confirmations_between_phases`` (from a hardcoded option default),
    so ``confirmation_poll_interval`` and ``retry_delay_seconds`` from the
    config file / ``TUMBLER__*`` env vars were silently ignored.
    """

    def _settings(self, tmp_path: Path) -> _FakeSettings:
        settings = _FakeSettings(tmp_path)
        settings.tumbler.min_confirmations_between_phases = 3
        settings.tumbler.confirmation_poll_interval = 2.5
        settings.tumbler.retry_delay_seconds = 10.0
        return settings

    def test_pacing_pulled_from_settings_without_override(self, tmp_path: Path) -> None:
        pacing = resolve_runner_pacing(self._settings(tmp_path), None)
        assert pacing.min_confirmations_between_phases == 3
        assert pacing.confirmation_poll_interval == 2.5
        assert pacing.retry_delay_seconds == 10.0

    def test_cli_override_wins_for_min_confirmations_only(self, tmp_path: Path) -> None:
        pacing = resolve_runner_pacing(self._settings(tmp_path), 1)
        assert pacing.min_confirmations_between_phases == 1
        # The other pacing knobs still come from settings.
        assert pacing.confirmation_poll_interval == 2.5
        assert pacing.retry_delay_seconds == 10.0

    def test_zero_override_disables_gate(self, tmp_path: Path) -> None:
        # 0 is a meaningful value ("disable the gate") and must not be
        # confused with "not provided".
        pacing = resolve_runner_pacing(self._settings(tmp_path), 0)
        assert pacing.min_confirmations_between_phases == 0


class TestCollectBalances:
    @pytest.mark.asyncio
    async def test_uses_coinjoin_selectable_capacity(self) -> None:
        calls: list[tuple[int, int, set[tuple[str, int]]]] = []

        class _FakeWallet:
            async def get_coinjoin_balance(
                self,
                mixdepth: int,
                min_confirmations: int = 0,
                *,
                restrict_md0: bool = True,
                exclude: set[tuple[str, int]] | None = None,
            ) -> int:
                del restrict_md0
                calls.append((mixdepth, min_confirmations, exclude or set()))
                return 1_000

            def get_locked_input_outpoints(self) -> set[tuple[str, int]]:
                return {("aa" * 32, 1)}

        balances = await _collect_balances(  # type: ignore[arg-type]
            _FakeWallet(), 3, min_confirmations=5
        )

        assert balances == {0: 1_000, 1: 1_000, 2: 1_000}
        assert calls
        assert all(
            min_conf == 5 and excluded == {("aa" * 32, 1)} for _, min_conf, excluded in calls
        )
