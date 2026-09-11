from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from jmcore.directory_client import DirectoryClient
from jmcore.protocol import (
    FEATURE_COFUNDED_CHANNEL_RING_V1,
    FEATURE_COFUNDED_CHANNEL_RING_V2,
    MessageType,
)


def _accepted_handshake() -> bytes:
    return json.dumps(
        {
            "type": MessageType.DN_HANDSHAKE.value,
            "line": json.dumps(
                {
                    "accepted": True,
                    "proto-ver-min": 5,
                    "proto-ver-max": 5,
                    "features": {},
                    "nick": "directory",
                }
            ),
        }
    ).encode()


async def _handshake_features(*, cofunded_channel_ring_v2: bool = False) -> dict[str, bool]:
    connection = AsyncMock()
    connection.receive.return_value = _accepted_handshake()
    client = DirectoryClient(
        "directory", 5222, "regtest", cofunded_channel_ring_v2=cofunded_channel_ring_v2
    )
    client.connection = connection

    await client._handshake()

    handshake = json.loads(connection.send.await_args.args[0])
    return json.loads(handshake["line"])["features"]


@pytest.mark.asyncio
async def test_directory_handshake_omits_ring_capabilities_when_disabled() -> None:
    features = await _handshake_features()

    assert FEATURE_COFUNDED_CHANNEL_RING_V1 not in features
    assert FEATURE_COFUNDED_CHANNEL_RING_V2 not in features


@pytest.mark.asyncio
async def test_directory_handshake_advertises_private_ring_v2_only() -> None:
    features = await _handshake_features(cofunded_channel_ring_v2=True)

    assert features[FEATURE_COFUNDED_CHANNEL_RING_V2] is True
    assert FEATURE_COFUNDED_CHANNEL_RING_V1 not in features
