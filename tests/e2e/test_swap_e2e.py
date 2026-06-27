"""
End-to-end tests for the swap ecosystem (docker ``swap`` profile).

These run against the real services brought up by the ``swap`` compose profile:
a Nostr relay (``nostr-relay``) and two LDK Server Lightning nodes
(``ldk-provider``, ``ldk-client``). The relay-backed discovery/RPC path is fully
exercised here; the LDK-backed swap completion (which needs funded channels) is
a documented follow-up.

Run with::

    docker compose --profile swap up -d
    pytest -m swap --fail-on-skip
"""

from __future__ import annotations

import asyncio

import pytest
from coincurve import PrivateKey

from .conftest import is_port_open

pytestmark = [pytest.mark.swap, pytest.mark.docker]

NOSTR_RELAY_URL = "ws://127.0.0.1:7000"


def _relay_up() -> bool:
    return is_port_open("127.0.0.1", 7000, timeout=1.0)


@pytest.mark.timeout(60)
async def test_advertise_and_discover_over_real_relay() -> None:
    """A provider advert published to the real relay is discovered by a client."""
    if not _relay_up():
        pytest.skip("nostr-relay (swap profile) not running on :7000")

    from jmswap.models import ProviderTerms, SwapDirection
    from jmswap.nostr import RelayTransport, SwapNostrNode

    transport = RelayTransport((NOSTR_RELAY_URL,))
    provider = SwapNostrNode(PrivateKey().secret, transport)
    client = SwapNostrNode(PrivateKey().secret, transport)

    terms = ProviderTerms(
        provider_id=provider.pubkey,
        provider_pubkey="02" + "11" * 32,
        network="regtest",
        directions=[SwapDirection.SWAP_IN, SwapDirection.SWAP_OUT],
        name="e2e-provider",
    )
    await provider.advertise(terms)
    await asyncio.sleep(1.0)  # allow the relay to persist the event
    adverts = await client.discover(timeout=5.0)
    assert any(a.nostr_pubkey == provider.pubkey for a in adverts)


def _ldk_creds(container: str, network: str = "regtest") -> tuple[str, str] | None:
    """Read an LDK Server container's API key (hex) and TLS cert via docker cp.

    Returns ``(api_key_hex, cert_pem_path)`` or ``None`` if unavailable.
    """
    import shutil
    import subprocess
    import tempfile

    if shutil.which("docker") is None:
        return None
    tmp = tempfile.mkdtemp(prefix="ldk-creds-")
    key_path = f"{tmp}/api_key"
    cert_path = f"{tmp}/tls.crt"
    try:
        subprocess.run(
            ["docker", "cp", f"{container}:/data/{network}/api_key", key_path],
            check=True,
            capture_output=True,
            timeout=20,
        )
        subprocess.run(
            ["docker", "cp", f"{container}:/data/tls.crt", cert_path],
            check=True,
            capture_output=True,
            timeout=20,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    with open(key_path, "rb") as fh:
        api_key_hex = fh.read().hex()
    return api_key_hex, cert_path


@pytest.mark.timeout(120)
async def test_ldk_server_node_id_over_grpc() -> None:
    """Validate the LdkServerBackend gRPC/protobuf/HMAC/TLS stack against LDK Server."""
    if not is_port_open("127.0.0.1", 3536, timeout=1.0):
        pytest.skip("ldk-provider (swap profile) gRPC not running on :3536")
    creds = _ldk_creds("jm-ldk-provider")
    if creds is None:
        pytest.skip("could not read LDK Server credentials from the container")
        return

    try:
        import httpx  # noqa: F401
    except ImportError:
        pytest.skip("httpx[http2] not installed")

    from jmswap.ldk import HttpxLdkTransport, LdkServerBackend

    api_key_hex, cert_path = creds
    backend = LdkServerBackend(
        HttpxLdkTransport("https://127.0.0.1:3536", verify=cert_path), api_key_hex
    )
    node_id = await backend.node_id()
    assert len(node_id) == 66 and all(c in "0123456789abcdef" for c in node_id.lower())


@pytest.mark.timeout(60)
async def test_swap_rpc_roundtrip_over_real_relay() -> None:
    """A swap RPC DM round-trips through the real relay between two nodes."""
    if not _relay_up():
        pytest.skip("nostr-relay (swap profile) not running on :7000")

    from typing import Any

    from jmswap.nostr import RelayTransport, SwapNostrNode

    transport = RelayTransport((NOSTR_RELAY_URL,))
    provider = SwapNostrNode(PrivateKey().secret, transport)
    client = SwapNostrNode(PrivateKey().secret, transport)

    async def handler(
        method: str, params: dict[str, Any], sender: str
    ) -> dict[str, Any]:
        return {"pong": params.get("ping"), "method": method}

    serve_task = asyncio.create_task(provider.serve(handler))
    try:
        await asyncio.sleep(0.5)
        result = await client.call(provider.pubkey, "ping", {"ping": 42}, timeout=15)
        assert result == {"pong": 42, "method": "ping"}
    finally:
        serve_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve_task
