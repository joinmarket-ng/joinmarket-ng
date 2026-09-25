"""Tests for the tumbler-specific maker policy overrides."""

from __future__ import annotations

from jmcore.models import OfferType
from maker.config import MakerConfig, OfferConfig

from tumbler.maker_policy import apply_tumbler_maker_policy

# BIP39 test vector — never used on mainnet.
_TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)


def _baseline_config(**overrides: object) -> MakerConfig:
    kwargs: dict[str, object] = {"mnemonic": _TEST_MNEMONIC}
    kwargs.update(overrides)
    return MakerConfig(**kwargs)  # type: ignore[arg-type]


def test_policy_forces_zero_absolute_fee_offer() -> None:
    """A vanilla relative-fee config is rewritten to a 0-sat sw0absoffer."""
    config = _baseline_config(
        offer_type=OfferType.SW0_RELATIVE,
        cj_fee_relative="0.001",
        cj_fee_absolute=500,
    )

    apply_tumbler_maker_policy(config)

    assert config.offer_type == OfferType.SW0_ABSOLUTE
    assert config.cj_fee_absolute == 0
    # The relative fee is left untouched: it's irrelevant for absolute
    # offers, and zeroing it would trip ``OfferConfig`` validation if
    # the user later flipped back to a relative offer.
    assert config.cj_fee_relative == "0.001"


def test_policy_disables_fidelity_bond() -> None:
    """A bond-enabled config is forced to ``no_fidelity_bond=True``."""
    config = _baseline_config(no_fidelity_bond=False)
    apply_tumbler_maker_policy(config)
    assert config.no_fidelity_bond is True


def test_policy_preserves_taproot_pit() -> None:
    config = _baseline_config(
        address_type="p2tr",
        offer_type=OfferType.TR0_RELATIVE,
        offer_configs=[OfferConfig(offer_type=OfferType.TR0_RELATIVE)],
    )

    apply_tumbler_maker_policy(config)

    assert config.offer_type == OfferType.TR0_ABSOLUTE
    assert config.get_effective_offer_configs()[0].offer_type == OfferType.TR0_ABSOLUTE
    assert config.cj_fee_absolute == 0
    assert config.no_fidelity_bond is True
    assert config.offer_configs == []


def test_policy_clears_multi_offer_configs() -> None:
    """Multi-offer takes precedence; tumbler must clear it to enforce policy."""
    config = _baseline_config(
        offer_configs=[
            OfferConfig(offer_type=OfferType.SW0_RELATIVE, cj_fee_relative="0.002"),
            OfferConfig(offer_type=OfferType.SW0_ABSOLUTE, cj_fee_absolute=1000),
        ],
    )

    apply_tumbler_maker_policy(config)

    assert config.offer_configs == []
    # ``get_effective_offer_configs`` falls back to single-offer fields
    # when the list is empty; verify the fallback respects the policy.
    effective = config.get_effective_offer_configs()
    assert len(effective) == 1
    assert effective[0].offer_type == OfferType.SW0_ABSOLUTE
    assert effective[0].cj_fee_absolute == 0


def test_policy_is_idempotent() -> None:
    """Re-applying the policy on an already-policed config is a no-op."""
    config = _baseline_config()
    apply_tumbler_maker_policy(config)
    snapshot = (
        config.offer_type,
        config.cj_fee_absolute,
        config.no_fidelity_bond,
        list(config.offer_configs),
    )

    apply_tumbler_maker_policy(config)

    assert (
        config.offer_type,
        config.cj_fee_absolute,
        config.no_fidelity_bond,
        list(config.offer_configs),
    ) == snapshot


def test_policy_returns_same_instance() -> None:
    """Mutates in place and returns the same object for chaining."""
    config = _baseline_config()
    result = apply_tumbler_maker_policy(config)
    assert result is config
