"""Regression tests for wallet backend transport security."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import patch

import pytest

from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.backends.neutrino import NeutrinoBackend

BackendFactory = Callable[[str], DescriptorWalletBackend | NeutrinoBackend]


@pytest.mark.parametrize(
    "backend_factory",
    [
        lambda url: DescriptorWalletBackend(rpc_url=url),
        lambda url: NeutrinoBackend(neutrino_url=url),
    ],
)
def test_remote_http_warning_omits_endpoint_details(backend_factory: BackendFactory) -> None:
    url = "http://rpc-user:rpc-password@node.example:8332"

    with patch("jmwallet.backends._transport_security.logger.warning") as warning:
        backend = backend_factory(url)
    asyncio.run(backend.close())

    warning.assert_called_once()
    message = warning.call_args.args[0]
    assert "node.example" not in message
    assert "rpc-user" not in message
    assert "rpc-password" not in message


@pytest.mark.parametrize(
    "backend_factory",
    [
        lambda url: DescriptorWalletBackend(rpc_url=url),
        lambda url: NeutrinoBackend(neutrino_url=url),
    ],
)
@pytest.mark.parametrize(
    "url",
    [
        "https://node.example:8332",
        "http://127.0.0.1:8332",
        "http://[::1]:8332",
        "http://LOCALHOST.:8332",
    ],
)
def test_secure_or_loopback_backend_urls_do_not_warn(
    backend_factory: BackendFactory, url: str
) -> None:
    with patch("jmwallet.backends._transport_security.logger.warning") as warning:
        backend = backend_factory(url)
    asyncio.run(backend.close())

    warning.assert_not_called()
