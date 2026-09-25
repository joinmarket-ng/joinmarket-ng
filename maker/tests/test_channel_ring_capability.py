from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from jmcore.channel_ring import ChannelRingConfig
from jmcore.config import TorControlConfig
from jmcore.models import NetworkType, OfferType
from jmcore.protocol import FEATURE_COFUNDED_CHANNEL_RING_V1

from maker.bot import MakerBot
from maker.config import MakerConfig
from maker.directory_pool import MakerDirectoryPool

TEST_MNEMONIC = "abandon " * 11 + "about"
# An explicit Tor control block keeps these tests off the operator's config
# file, which the environment-derived default would otherwise read.
ISOLATED_TOR_CONTROL = TorControlConfig(enabled=False)


def test_maker_capability_is_disabled_until_backend_validation() -> None:
    config = MakerConfig(
        mnemonic=TEST_MNEMONIC,
        directory_servers=[],
        tor_control=ISOLATED_TOR_CONTROL,
    )
    pool = MakerDirectoryPool(
        config=config,
        nick_identity=object(),
        neutrino_compat=False,
    )
    before = pool._build_client_kwargs("directory", 5222)
    assert before[FEATURE_COFUNDED_CHANNEL_RING_V1] is False

    pool.enable_cofunded_channel_ring_v1()
    after = pool._build_client_kwargs("directory", 5222)
    assert after[FEATURE_COFUNDED_CHANNEL_RING_V1] is True


def _ring_maker_config(data_dir: Path, *, enabled: bool) -> MakerConfig:
    return MakerConfig(
        mnemonic=TEST_MNEMONIC,
        directory_servers=["localhost:5222"],
        network=NetworkType.REGTEST,
        address_type="p2tr",
        offer_type=OfferType.TR0_RELATIVE,
        data_dir=data_dir,
        tor_control=ISOLATED_TOR_CONTROL,
        channel_ring=ChannelRingConfig(
            enabled=enabled,
            lnd_grpc_url="https://127.0.0.1:10009",
            lnd_tls_cert_path=data_dir / "tls.cert",
            lnd_macaroon_path=data_dir / "admin.macaroon",
            onion_endpoint="a" * 56 + ".onion:9735",
            persistence_directory=data_dir / "rings",
        ),
    )


def _taproot_maker_dependencies() -> tuple[MagicMock, MagicMock]:
    wallet = MagicMock()
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    wallet.address_type = "p2tr"
    backend = MagicMock()
    backend.can_resolve_foreign_prevouts.return_value = True
    return wallet, backend


def test_explicit_buyout_with_enabled_ring_is_rejected_before_advertising(tmp_path: Path) -> None:
    """One round funds its change from a buyout escrow or a ring, never both."""
    wallet, backend = _taproot_maker_dependencies()
    config = _ring_maker_config(tmp_path, enabled=True)

    with pytest.raises(ValueError, match="cannot be used by the same maker"):
        MakerBot(wallet=wallet, backend=backend, config=config, buyout=MagicMock())


def test_enabled_ring_without_buyout_is_accepted(tmp_path: Path) -> None:
    wallet, backend = _taproot_maker_dependencies()
    bot = MakerBot(
        wallet=wallet, backend=backend, config=_ring_maker_config(tmp_path, enabled=True)
    )

    assert bot.buyout is None
    assert bot._channel_ring_store is not None
    # The capability is only advertised after backend validation in start().
    assert bot.channel_ring_capability_validated is False


def test_buyout_without_ring_is_accepted(tmp_path: Path) -> None:
    wallet, backend = _taproot_maker_dependencies()
    buyout = MagicMock()

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("maker.bot.bound_buyout_mixdepth", lambda *args, **kwargs: 2)
        bot = MakerBot(
            wallet=wallet,
            backend=backend,
            config=_ring_maker_config(tmp_path, enabled=False),
            buyout=buyout,
        )

    assert bot.buyout is buyout
    assert bot.buyout_mixdepth == 2
    assert bot._channel_ring_store is None
