from __future__ import annotations

from jmcore.protocol import FEATURE_COFUNDED_CHANNEL_RING_V1

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

    pool.enable_cofunded_channel_ring_v1()
    after = pool._build_client_kwargs("directory", 5222)
    assert after[FEATURE_COFUNDED_CHANNEL_RING_V1] is True
