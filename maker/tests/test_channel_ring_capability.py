from __future__ import annotations

from jmcore.protocol import (
    FEATURE_CHANNEL_INPUT_V1,
    FEATURE_COFUNDED_CHANNEL_RING_V1,
    FEATURE_COFUNDED_CHANNEL_RING_V2,
)

from maker.config import MakerConfig
from maker.directory_pool import MakerDirectoryPool


def test_maker_capability_is_disabled_until_backend_validation() -> None:
    config = MakerConfig(mnemonic="abandon " * 11 + "about", directory_servers=[])
    pool = MakerDirectoryPool(
        config=config,
        nick_identity=object(),
        neutrino_compat=False,
    )
    before = pool._build_client_kwargs("directory", 5222)
    assert before[FEATURE_COFUNDED_CHANNEL_RING_V1] is False
    assert before[FEATURE_CHANNEL_INPUT_V1] is False

    pool.enable_cofunded_channel_ring_v1()
    after = pool._build_client_kwargs("directory", 5222)
    assert after[FEATURE_COFUNDED_CHANNEL_RING_V1] is True

    pool.enable_channel_input_v1()
    after_channel_input = pool._build_client_kwargs("directory", 5222)
    assert after_channel_input[FEATURE_CHANNEL_INPUT_V1] is True

    pool.disable_channel_input_v1()
    after_disable = pool._build_client_kwargs("directory", 5222)
    assert after_disable[FEATURE_CHANNEL_INPUT_V1] is False


def test_private_maker_capability_advertises_v2_only_after_validation() -> None:
    config = MakerConfig(mnemonic="abandon " * 11 + "about", directory_servers=[])
    pool = MakerDirectoryPool(
        config=config,
        nick_identity=object(),
        neutrino_compat=False,
    )

    before = pool._build_client_kwargs("directory", 5222)
    assert before[FEATURE_COFUNDED_CHANNEL_RING_V1] is False
    assert before[FEATURE_COFUNDED_CHANNEL_RING_V2] is False

    pool.enable_cofunded_channel_ring_v2()
    after = pool._build_client_kwargs("directory", 5222)
    assert after[FEATURE_COFUNDED_CHANNEL_RING_V1] is False
    assert after[FEATURE_COFUNDED_CHANNEL_RING_V2] is True
