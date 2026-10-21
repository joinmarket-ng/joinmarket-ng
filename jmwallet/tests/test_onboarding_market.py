"""Role setup never treats a checkbox, saved origin, or node quiescence as history."""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from jmcore.crypto import mnemonic_to_seed
from jmcore.market_keys import WalletMarketKeys
from jmcore.market_store import MarketStore
from jmcore.paths import get_market_store_path, get_used_commitments_path
from pydantic import SecretStr
from typer.testing import CliRunner

from jmwallet import onboarding, setup_lightning, setup_market, setup_wallet
from jmwallet.onboarding import app

PHRASE = " ".join(["all"] * 12)
PASSWORD = "private-role-wallet-password"
runner = CliRunner()


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("BIP39_PASSPHRASE", raising=False)
    monkeypatch.delenv("EXPECTED_FINGERPRINT", raising=False)
    directory = tmp_path / "data"
    password = tmp_path / "password"
    password.write_text(PASSWORD)
    monkeypatch.setattr(setup_wallet, "generate_mnemonic_secure", Mock(return_value=PHRASE))
    return {
        "directory": directory,
        "password": password,
        "wallet": directory / "wallets/default.mnemonic",
        "args": [
            "--data-dir",
            str(directory),
            "--yes",
            "--taproot",
            "--lightning",
            "manual",
            "--network",
            "regtest",
            "--wallet-action",
            "generate",
            "--wallet-password-file",
            str(password),
            "--role",
            "maker",
            "--sell-podle",
            "--rent-bonds",
            "--no-lease-bond",
            "--no-bip39-passphrase",
        ],
    }


def ledger_state(state: Any, passphrase: str = "") -> str:
    keys = WalletMarketKeys(mnemonic_to_seed(PHRASE, passphrase), "regtest")
    try:
        return MarketStore.diagnose_wallet(
            get_market_store_path(state["directory"]),
            wallet_id=keys.ledger_identity(),
            commitments_path=get_used_commitments_path(state["directory"], create=False),
        )["state"]
    finally:
        keys.close()


def test_fresh_seller_configuration_waits_for_actual_activation_and_never_starts_services(
    fixture: Any,
) -> None:
    state = fixture
    result = runner.invoke(
        app,
        [*state["args"], "--activate-market-ledger", "--market-history-excluded-since-generation"],
    )
    assert result.exit_code == 0, result.output
    assert ledger_state(state) == "ready"
    document = tomllib.loads((state["directory"] / "config.toml").read_text())
    assert document["maker"]["sell_podle"] is True and document["maker"]["rent_bonds"] is True
    assert document["maker"]["lease_bond"] is False
    assert "role" not in document
    assert "no seller service started" in result.output
    assert "eligible owned bonds" in result.output
    assert PHRASE not in result.output and PASSWORD not in result.output


@pytest.mark.parametrize("affirm", [False, True])
def test_history_exclusion_is_explained_before_generation_and_explicitly_covers_whole_interval(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
    affirm: bool,
) -> None:
    state = fixture
    events = []
    original_echo = onboarding.typer.echo

    def echo(value: Any = None, *args: Any, **kwargs: Any) -> None:
        if isinstance(value, str) and value.startswith("Before generation:"):
            events.append("notice")
        original_echo(value, *args, **kwargs)

    def generate(*args: Any) -> str:
        assert events == ["notice"]
        events.append("generate")
        return PHRASE

    monkeypatch.setattr(onboarding.typer, "echo", echo)
    monkeypatch.setattr(setup_wallet, "generate_mnemonic_secure", generate)
    args = [a for a in state["args"] if a != "--yes"]
    result = runner.invoke(
        app,
        [*args, "--rpc-url", "http://127.0.0.1:18443", "--activate-market-ledger"],
        input="y\n" + ("y\n" if affirm else "n\n"),
    )
    assert (result.exit_code == 0) is affirm, result.output
    assert "SINCE GENERATION" in result.output and "THROUGH ACTIVATION" in result.output
    assert events == ["notice", "generate"]
    assert ledger_state(state) == ("ready" if affirm else "absent")


@pytest.mark.parametrize("missing", ["activation", "history"])
def test_yes_and_node_quiescence_never_supply_market_authority(fixture: Any, missing: str) -> None:
    state = fixture
    flags = ["--applications-quiesced"]
    flags += (
        ["--market-history-excluded-since-generation"]
        if missing == "activation"
        else ["--activate-market-ledger"]
    )
    result = runner.invoke(app, [*state["args"], *flags])
    assert result.exit_code != 0
    assert state["wallet"].exists() and ledger_state(state) == "absent"
    document = tomllib.loads((state["directory"] / "config.toml").read_text())
    assert not document.get("maker", {}).get("sell_podle", False)
    assert not document.get("maker", {}).get("rent_bonds", False)


def test_activation_precedes_seed_display_and_failure_permanently_consumes_eligibility(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    events = []
    real_activate, real_echo = MarketStore.activate_wallet, onboarding.typer.echo

    def activate(store: MarketStore, *args: Any, **kwargs: Any) -> None:
        assert "seed" not in events
        document = tomllib.loads((state["directory"] / "config.toml").read_text())
        assert not document.get("maker", {}).get("sell_podle", False)
        events.append("activate")
        real_activate(store, *args, **kwargs)

    def echo(value: Any = None, *args: Any, **kwargs: Any) -> None:
        if value == PHRASE:
            events.append("seed")
        real_echo(value, *args, **kwargs)

    monkeypatch.setattr(MarketStore, "activate_wallet", activate)
    monkeypatch.setattr(onboarding.typer, "echo", echo)
    result = runner.invoke(
        app,
        [
            *state["args"],
            "--activate-market-ledger",
            "--market-history-excluded-since-generation",
            "--show-generated-seed",
        ],
    )
    assert result.exit_code == 0, result.output
    assert events == ["activate", "seed"]


def test_refused_activation_keeps_seed_backup_available_but_cannot_be_retried(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    selected = []
    original = onboarding._wallet_stage

    def select(*args: Any, **kwargs: Any) -> Any:
        wallet = original(*args, **kwargs)
        selected.append(wallet)
        return wallet

    monkeypatch.setattr(onboarding, "_wallet_stage", select)
    result = runner.invoke(
        app, [*state["args"], "--activate-market-ledger", "--show-generated-seed"]
    )
    assert result.exit_code != 0 and PHRASE in result.output
    assert selected[0].publication is not None
    assert (
        selected[0].publication.newly_created is True
    )  # Historical fact, not remaining authority.
    assert selected[0].claim_market_publication() is None
    with pytest.raises(onboarding.typer.Exit):
        onboarding._role_market_stage(
            state["directory"] / "config.toml",
            selected[0],
            data_dir=state["directory"],
            role_updates={"maker": {"sell_podle": True}},
            needs_ledger=True,
            activate=True,
            history_excluded=True,
            passphrase_file=None,
            no_passphrase=True,
            interactive=False,
        )
    assert not (state["directory"] / "market").exists()


@pytest.mark.parametrize("action", ["existing", "import", "resume"])
def test_older_wallet_paths_never_get_fresh_activation(
    fixture: Any, action: str, tmp_path: Path
) -> None:
    state = fixture
    args = list(state["args"])
    if action == "import":
        words = tmp_path / "words"
        words.write_text(PHRASE)
        args[args.index("--wallet-action") + 1] = "import"
        args += ["--import-mnemonic-file", str(words)]
    else:
        setup_wallet.publish_setup_wallet(
            state["wallet"], SecretStr(PASSWORD), operation="generate"
        )
        if action == "resume":
            args += ["--resume-wallet-publication"]
        else:
            args[args.index("--wallet-action") + 1] = "existing"
    result = runner.invoke(
        app, [*args, "--activate-market-ledger", "--market-history-excluded-since-generation"]
    )
    assert result.exit_code != 0 and "Only uninterrupted generation" in result.output
    assert ledger_state(state) == "absent"
    assert PHRASE not in result.output


def test_ready_existing_wallet_configures_without_activation_or_history_claim(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    first = runner.invoke(
        app,
        [*state["args"], "--activate-market-ledger", "--market-history-excluded-since-generation"],
    )
    assert first.exit_code == 0, first.output
    activate = Mock(side_effect=AssertionError("no reactivation"))
    monkeypatch.setattr(MarketStore, "activate_wallet", activate)
    args = list(state["args"])
    args[args.index("--wallet-action") + 1] = "existing"
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "Existing seller-ledger binding checked" in result.output
    activate.assert_not_called()


@pytest.mark.parametrize("mode", ["--configure-only", "--dry-run"])
def test_preview_and_configuration_modes_never_touch_ledger_or_wallet_secrets(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    state = fixture
    monkeypatch.setattr(
        onboarding, "read_private_file", Mock(side_effect=AssertionError("secret read"))
    )
    create = Mock(side_effect=AssertionError("ledger effect"))
    monkeypatch.setattr(setup_market, "prepare_seller_ledger", create)
    result = runner.invoke(
        app,
        [
            *state["args"],
            mode,
            "--activate-market-ledger",
            "--market-history-excluded-since-generation",
        ],
    )
    assert result.exit_code == 0, result.output
    create.assert_not_called()
    assert not state["wallet"].exists() and not (state["directory"] / "market").exists()


def test_activation_failure_preserves_partial_state_and_does_not_enable_seller_flags(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    monkeypatch.setattr(
        MarketStore,
        "_read_commitments_history",
        Mock(side_effect=OSError("private-activation-error")),
    )
    result = runner.invoke(
        app,
        [*state["args"], "--activate-market-ledger", "--market-history-excluded-since-generation"],
    )
    assert result.exit_code != 0
    assert ledger_state(state) == "recovery_required"
    assert state["wallet"].exists()
    document = tomllib.loads((state["directory"] / "config.toml").read_text())
    assert not document.get("maker", {}).get("sell_podle", False)
    assert "private-activation-error" not in result.output and PHRASE not in result.output


def test_role_selection_never_promotes_taproot_and_paid_lease_is_separate_opt_in(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "plain"
    result = runner.invoke(
        app, ["--data-dir", str(directory), "--yes", "--configure-only", "--role", "both"]
    )
    assert result.exit_code == 0, result.output
    document = tomllib.loads((directory / "config.toml").read_text())
    assert document["wallet"]["address_type"] == "p2wpkh" and document["lightning"]["mode"] == "off"
    assert not document.get("maker", {}).get("lease_bond", False)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "--yes",
            "--configure-only",
            "--taproot",
            "--lightning",
            "manual",
            "--role",
            "maker",
            "--lease-bond",
            "--lease-bond-max-sats",
            "12345",
            "--lease-bond-max-rate",
            "0.015",
        ],
    )
    assert result.exit_code == 0, result.output
    maker = tomllib.loads((directory / "config.toml").read_text())["maker"]
    assert (
        maker["lease_bond"] is True
        and maker["lease_bond_max_sats"] == 12345
        and maker["lease_bond_max_rate"] == 0.015
    )


def test_paid_leasing_and_caps_are_not_enabled_before_wallet_selection(fixture: Any) -> None:
    state = fixture
    state["wallet"].parent.mkdir(parents=True)
    state["wallet"].write_bytes(b"previous wallet that must not be replaced")
    args = [
        a for a in state["args"] if a not in {"--sell-podle", "--rent-bonds", "--no-lease-bond"}
    ]
    result = runner.invoke(
        app,
        [
            *args,
            "--no-sell-podle",
            "--no-rent-bonds",
            "--lease-bond",
            "--lease-bond-max-sats",
            "12345",
        ],
    )
    assert result.exit_code != 0
    maker = tomllib.loads((state["directory"] / "config.toml").read_text()).get("maker", {})
    assert not maker.get("lease_bond", False) and "lease_bond_max_sats" not in maker
    assert state["wallet"].read_bytes() == b"previous wallet that must not be replaced"


@pytest.mark.parametrize("kind", ["lease", "taker"])
def test_buyer_permissions_apply_after_identity_without_seller_ledger(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    state = fixture
    args = [
        a for a in state["args"] if a not in {"--sell-podle", "--rent-bonds", "--no-lease-bond"}
    ]
    args[args.index("--role") + 1] = "maker" if kind == "lease" else "taker"
    args += (
        ["--no-sell-podle", "--no-rent-bonds", "--lease-bond"]
        if kind == "lease"
        else ["--external-podle-mode", "prefer"]
    )
    prepare = Mock(side_effect=AssertionError("buyers do not initialize seller state"))
    monkeypatch.setattr(setup_market, "prepare_seller_ledger", prepare)
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    document = tomllib.loads((state["directory"] / "config.toml").read_text())
    if kind == "lease":
        assert document["maker"]["lease_bond"] is True
    else:
        assert document["taker"]["external_podle_mode"] == "prefer"
    assert not (state["directory"] / "market").exists()
    prepare.assert_not_called()


def test_bip39_identity_is_resolved_before_activation_and_public_aliases_stay_consistent(
    fixture: Any,
    tmp_path: Path,
) -> None:
    state = fixture
    state["directory"].mkdir()
    config = state["directory"] / "config.toml"
    config.write_text("[wallet]\nwallet_with_passphrase = false\n")
    password = tmp_path / "bip39"
    password.write_text("private-bip39-identity")
    args = [a for a in state["args"] if a != "--no-bip39-passphrase"]
    result = runner.invoke(
        app,
        [
            *args,
            "--bip39-passphrase-file",
            str(password),
            "--activate-market-ledger",
            "--market-history-excluded-since-generation",
        ],
    )
    assert result.exit_code == 0, result.output
    assert ledger_state(state, "private-bip39-identity") == "ready"
    wallet = tomllib.loads(config.read_text())["wallet"]
    assert wallet["wallet_with_passphrase"] is True and wallet["bip39_passphrase_enabled"] is True
    assert "private-bip39-identity" not in result.output + config.read_text()


def test_single_identity_choice_is_reused_for_ledger_and_node_initialization(
    fixture: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    passphrase = tmp_path / "bip39"
    passphrase.write_text("private-shared-identity")
    read = onboarding.read_private_file
    reads = []

    def counted(path: Path) -> bytes:
        reads.append(path)
        return read(path)

    def initialize(settings: Any, mnemonic: SecretStr, chosen: SecretStr, **kwargs: Any) -> Any:
        assert chosen.get_secret_value() == "private-shared-identity"
        assert ledger_state(state, "private-shared-identity") == "ready"
        assert settings.wallet.bip39_passphrase_enabled is True
        return SimpleNamespace(nodes=[object()])

    initializer = Mock(side_effect=initialize)
    monkeypatch.setattr(setup_lightning, "initialize_managed_nodes", initializer)
    monkeypatch.setattr(onboarding, "read_private_file", counted)
    args = [a for a in state["args"] if a != "--no-bip39-passphrase"]
    args[args.index("--lightning") + 1] = "managed"
    result = runner.invoke(
        app,
        [
            *args,
            "--bip39-passphrase-file",
            str(passphrase),
            "--activate-market-ledger",
            "--market-history-excluded-since-generation",
            "--create-managed-nodes",
            "--applications-quiesced",
        ],
    )
    assert result.exit_code == 0, result.output
    assert reads.count(passphrase) == 1
    initializer.assert_called_once()
    assert "private-shared-identity" not in result.output


def test_wallet_only_passphrase_choice_is_not_ignored_or_persisted_as_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BIP39_PASSPHRASE", raising=False)
    monkeypatch.delenv("EXPECTED_FINGERPRINT", raising=False)
    monkeypatch.setattr(setup_wallet, "generate_mnemonic_secure", Mock(return_value=PHRASE))
    password = tmp_path / "password"
    password.write_text(PASSWORD)
    passphrase = tmp_path / "bip39"
    passphrase.write_text("private-wallet-only-passphrase")
    directory = tmp_path / "plain"
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "--yes",
            "--wallet-action",
            "generate",
            "--wallet-password-file",
            str(password),
            "--bip39-passphrase-file",
            str(passphrase),
        ],
    )
    assert result.exit_code == 0, result.output
    text = (directory / "config.toml").read_text()
    assert tomllib.loads(text)["wallet"]["bip39_passphrase_enabled"] is True
    assert "private-wallet-only-passphrase" not in text + result.output
    assert not (directory / "market").exists()
