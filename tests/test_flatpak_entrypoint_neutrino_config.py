from __future__ import annotations

from pathlib import Path


def _entrypoint_text() -> str:
    entrypoint = (
        Path(__file__).resolve().parents[1] / "flatpak" / "jam-ng-entrypoint.sh"
    )
    return entrypoint.read_text(encoding="utf-8")


def test_neutrino_start_reads_prefetch_related_settings_from_config() -> None:
    text = _entrypoint_text()

    assert 'read_config_value "bitcoin" "neutrino_clearnet_initial_sync" "true"' in text
    assert 'read_config_value "bitcoin" "neutrino_prefetch_filters" "true"' in text
    assert (
        'read_config_value "bitcoin" "neutrino_prefetch_lookback_blocks" "105120"'
        in text
    )


def test_neutrino_start_passes_prefetch_env_to_neutrinod() -> None:
    text = _entrypoint_text()

    assert 'CLEARNET_INITIAL_SYNC="${clearnet_initial_sync}" \\' in text
    assert 'PREFETCH_FILTERS="${prefetch_filters}" \\' in text
    assert 'PREFETCH_LOOKBACK="${prefetch_lookback_blocks}" \\' in text
