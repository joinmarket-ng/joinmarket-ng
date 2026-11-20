"""Tests for labels shown by direct-send interactive UTXO selection."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
import typer
from loguru import logger

from jmwallet.cli.send import _select_input_utxos
from jmwallet.wallet.models import UTXOInfo


def _utxo(
    *,
    label: str | None = None,
    locktime: int | None = None,
    vout: int = 0,
    frozen: bool = False,
) -> UTXOInfo:
    return UTXOInfo(
        txid="a" * 64,
        vout=vout,
        value=100_000,
        address="bcrt1qselectorlabel",
        confirmations=6,
        scriptpubkey="0014" + "00" * 20,
        path="m/84'/1'/0'/0/0",
        mixdepth=0,
        label=label,
        frozen=frozen,
        locktime=locktime,
    )


@pytest.mark.asyncio
async def test_selector_preserves_user_and_fidelity_bond_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-send selection does not replace existing BIP-329 annotations."""
    user_labeled = _utxo(label="personal note")
    fidelity_bond = _utxo(label="bond note", locktime=1_900_000_000)
    wallet = AsyncMock()
    wallet.mixdepth_count = 1
    wallet.get_utxos = AsyncMock(return_value=[user_labeled, fidelity_bond])
    wallet.get_utxo_label_from_wallet = Mock(return_value="cj-out")
    wallet.get_locked_input_outpoints = Mock(return_value=set())

    captured: list[UTXOInfo] = []

    def fake_select(utxos, *_args, **_kwargs):  # noqa: ANN001, ANN202
        captured.extend(utxos)
        return [user_labeled]

    import jmwallet.utxo_selector

    monkeypatch.setattr(jmwallet.utxo_selector, "select_utxos_interactive", fake_select)

    selected = await _select_input_utxos(
        wallet=wallet,
        backend=Mock(),
        amount=50_000,
        mixdepth=None,
        interactive=True,
        destination="bcrt1qq6hag67dl53wl99vzg42z8eyzfz2xlkvwk6f7m",
        fee_rate=1.0,
    )

    assert selected == ([user_labeled], 0, None)
    assert [utxo.label for utxo in captured] == ["personal note", "bond note"]
    wallet.get_utxo_label_from_wallet.assert_not_called()


@pytest.mark.asyncio
async def test_selector_classifies_unlabeled_regular_utxo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct-send selection classifies an unlabeled regular UTXO."""
    unlabeled = _utxo()
    wallet = AsyncMock()
    wallet.mixdepth_count = 1
    wallet.get_utxos = AsyncMock(return_value=[unlabeled])
    wallet.get_utxo_label_from_wallet = Mock(return_value="cj-out")
    wallet.get_locked_input_outpoints = Mock(return_value=set())

    import jmwallet.utxo_selector

    monkeypatch.setattr(
        jmwallet.utxo_selector,
        "select_utxos_interactive",
        lambda utxos, *_args, **_kwargs: list(utxos),
    )

    selected = await _select_input_utxos(
        wallet=wallet,
        backend=Mock(),
        amount=50_000,
        mixdepth=None,
        interactive=True,
        destination="bcrt1qq6hag67dl53wl99vzg42z8eyzfz2xlkvwk6f7m",
        fee_rate=1.0,
    )

    assert selected == ([unlabeled], 0, None)
    assert unlabeled.label == "cj-out"
    wallet.get_utxo_label_from_wallet.assert_called_once_with(unlabeled.address)


@pytest.mark.asyncio
async def test_sweep_falls_back_to_expired_fidelity_bonds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automatic sweep admits expired bonds when no regular coins qualify."""
    mtp = 1_700_000_000
    frozen_regular = _utxo(frozen=True)
    expired_bond = _utxo(label="bond note", locktime=mtp - 1000, vout=1)
    wallet = AsyncMock()
    wallet.mixdepth_count = 1
    wallet.get_balance = AsyncMock(return_value=200_000)
    wallet.get_utxos = AsyncMock(return_value=[frozen_regular, expired_bond])
    wallet.get_locked_input_outpoints = Mock(return_value=set())
    backend = AsyncMock()
    backend.get_median_time_past = AsyncMock(return_value=mtp)
    monkeypatch.setattr(
        "jmwallet.wallet.spend._is_signable_fidelity_bond",
        lambda _wallet, _utxo: True,
    )

    selected = await _select_input_utxos(
        wallet=wallet,
        backend=backend,
        amount=0,
        mixdepth=0,
        interactive=False,
        destination="bcrt1qq6hag67dl53wl99vzg42z8eyzfz2xlkvwk6f7m",
        fee_rate=1.0,
    )

    assert selected == ([expired_bond], 0, mtp)


@pytest.mark.asyncio
async def test_sweep_never_selects_frozen_bonds() -> None:
    """Automatic sweep refuses to run when the only coins are frozen bonds."""
    frozen_bond = _utxo(label="bond note", locktime=1_000_000, frozen=True)
    wallet = AsyncMock()
    wallet.mixdepth_count = 1
    wallet.get_balance = AsyncMock(return_value=100_000)
    wallet.get_utxos = AsyncMock(return_value=[frozen_bond])
    wallet.get_locked_input_outpoints = Mock(return_value=set())
    backend = AsyncMock()
    backend.get_median_time_past = AsyncMock(return_value=1_700_000_000)

    with pytest.raises(typer.Exit):
        await _select_input_utxos(
            wallet=wallet,
            backend=backend,
            amount=0,
            mixdepth=0,
            interactive=False,
            destination="bcrt1qq6hag67dl53wl99vzg42z8eyzfz2xlkvwk6f7m",
            fee_rate=1.0,
        )
    backend.get_median_time_past.assert_not_called()


@pytest.mark.asyncio
async def test_interactive_selection_suspends_logging_during_the_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The selector TUI owns the terminal: log records are dropped inside.

    Parity with the freeze manager: without the guard, loguru's stderr sink
    writes into the fullscreen curses layout (mixed-output race). Asserted
    behaviorally: a log call made while the TUI runs is dropped; logging
    works before and after the TUI again.
    """
    records: list = []
    handler_id = logger.add(lambda message: records.append(message), level="DEBUG")
    try:
        wallet = AsyncMock()
        wallet.mixdepth_count = 1
        wallet.get_utxos = AsyncMock(return_value=[_utxo(label="cj-out")])
        wallet.get_utxo_label_from_wallet = Mock(return_value="cj-out")
        wallet.get_locked_input_outpoints = Mock(return_value=set())

        import jmwallet.utxo_selector

        def fake_select(utxos, *_args, **_kwargs):  # noqa: ANN001, ANN202
            logger.error("suppressed-while-tui-runs")
            return []

        monkeypatch.setattr(jmwallet.utxo_selector, "select_utxos_interactive", fake_select)

        logger.info("marker-before-tui")
        assert any("marker-before-tui" in str(record) for record in records)

        await _select_input_utxos(
            wallet=wallet,
            backend=Mock(),
            amount=50_000,
            mixdepth=None,
            interactive=True,
            destination="bcrt1qq6hag67dl53wl99vzg42z8eyzfz2xlkvwk6f7m",
            fee_rate=1.0,
        )
        # Suppressed: the log call made inside the TUI never reached the sink.
        assert not any("suppressed-while-tui-runs" in str(record) for record in records)
        # Re-enabled: the log call after the TUI exited reached the sink.
        assert any("UTXO selection cancelled" in str(record) for record in records)
    finally:
        logger.remove(handler_id)
