"""Managed node LND configuration, environment, and pinned release checks."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from jmcore.settings import BitcoinSettings, TorSettings
from pydantic import SecretStr

from jmswap import lnd_release
from jmswap.lnd_config import LndEnvironment, render_lnd_conf
from jmswap.managed_lnd import onion_endpoint_of
from jmswap.managed_nodes import ManagedNodeError, allocate_node, new_registry

BINARY = Path("/opt/lnd")


def settings(**bitcoin: object) -> SimpleNamespace:
    tor = TorSettings(socks_port=19050, control_port=19051)
    return SimpleNamespace(bitcoin=BitcoinSettings(**bitcoin), tor=tor)


def test_environment_prefers_the_rpc_cookie() -> None:
    env = LndEnvironment.from_settings(
        settings(rpc_url="http://127.0.0.1:38342", rpc_cookie_file="/data/.cookie"),
        network="signet",
        lnd_binary=BINARY,
    )

    assert env.bitcoind_host == "127.0.0.1:38342"
    assert env.bitcoind_cookie == Path("/data/.cookie")
    assert env.bitcoind_password is None
    assert (env.tor_socks, env.tor_control) == ("127.0.0.1:19050", "127.0.0.1:19051")


def test_environment_defaults_the_network_rpc_port() -> None:
    env = LndEnvironment.from_settings(
        settings(rpc_url="core.local", rpc_user="u", rpc_password=SecretStr("hunter2")),
        network="signet",
        lnd_binary=BINARY,
    )

    assert env.bitcoind_host == "core.local:38332"
    assert (env.bitcoind_user, env.bitcoind_password) == ("u", "hunter2")
    assert "hunter2" not in repr(env)


@pytest.mark.parametrize(
    ("bitcoin", "tor", "message"),
    [
        ({"backend_type": "neutrino"}, {}, "Bitcoin Core"),
        ({}, {"control_enabled": False}, "Tor control"),
    ],
)
def test_environment_rejects_unsupported_hosts(
    bitcoin: dict[str, object], tor: dict[str, object], message: str
) -> None:
    config = SimpleNamespace(bitcoin=BitcoinSettings(**bitcoin), tor=TorSettings(**tor))

    with pytest.raises(ManagedNodeError, match=message):
        LndEnvironment.from_settings(config, network="signet", lnd_binary=BINARY)


def test_lnd_conf_is_private_tor_only_and_taproot() -> None:
    registry = new_registry("signet", "ab" * 32)
    node = allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
    env = LndEnvironment(
        network="signet",
        lnd_binary=BINARY,
        bitcoind_host="127.0.0.1:38332",
        bitcoind_cookie=Path("/data/.cookie"),
        tor_password="secret",
    )

    conf = render_lnd_conf(node, env)

    for line in (
        "listen=127.0.0.1:20001",
        "rpclisten=127.0.0.1:20002",
        "norest=true",
        "nobootstrap=true",
        "bitcoin.signet=true",
        "bitcoind.rpcpolling=true",
        "bitcoind.rpccookie=/data/.cookie",
        "tor.active=true",
        "tor.v3=true",
        "tor.streamisolation=true",
        "tor.password=secret",
        "protocol.simple-taproot-chans=true",
        "protocol.option-scid-alias=true",
    ):
        assert line in conf.splitlines()
    assert "rpcuser" not in conf and "zmq" not in conf


def test_lnd_conf_rejects_injected_lines() -> None:
    registry = new_registry("signet", "ab" * 32)
    node = allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
    env = LndEnvironment(
        network="signet",
        lnd_binary=BINARY,
        bitcoind_host="127.0.0.1:38332",
        bitcoind_user="u",
        bitcoind_password="p\nlisten=0.0.0.0:9735",
    )

    with pytest.raises(ManagedNodeError, match="line breaks"):
        render_lnd_conf(node, env)


def test_onion_endpoint_comes_from_the_node_own_uri() -> None:
    me, other = "02" + "11" * 32, "03" + "22" * 32
    onion = "b" * 56 + ".onion:9735"
    uris = [f"{other}@{'c' * 56}.onion:9735", f"{me}@203.0.113.1:9735", f"{me}@{onion}"]

    assert onion_endpoint_of(me, uris) == onion
    assert onion_endpoint_of(me, uris[:2]) is None


def _archive(lnd: bytes, lncli: bytes, prefix: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in (("lnd", lnd), ("lncli", lncli)):
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> bytes:
    prefix = f"lnd-test-x-{lnd_release.LND_VERSION}"
    archive = _archive(b"lnd binary", b"lncli binary", prefix)
    digests = dict(lnd_release._DIGESTS)
    digests["test-x"] = (
        hashlib.sha256(archive).hexdigest(),
        hashlib.sha256(b"lnd binary").hexdigest(),
        hashlib.sha256(b"lncli binary").hexdigest(),
    )
    monkeypatch.setattr(lnd_release, "_DIGESTS", digests)
    return archive


def test_verified_release_is_installed_private(tmp_path: Path, pinned: bytes) -> None:
    binary = lnd_release.extract_verified(pinned, "test-x", tmp_path / "bin")

    assert binary.read_bytes() == b"lnd binary"
    assert binary.stat().st_mode & 0o777 == 0o700


def test_tampered_archive_installs_nothing(tmp_path: Path, pinned: bytes) -> None:
    with pytest.raises(lnd_release.LndReleaseError, match="archive digest"):
        lnd_release.extract_verified(pinned + b"\0", "test-x", tmp_path / "bin")
    assert not (tmp_path / "bin").exists()


def test_tampered_binary_installs_nothing(
    tmp_path: Path, pinned: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = f"lnd-test-x-{lnd_release.LND_VERSION}"
    archive = _archive(b"lnd binary", b"evil", prefix)
    digests = dict(lnd_release._DIGESTS)
    digests["test-x"] = (hashlib.sha256(archive).hexdigest(), *digests["test-x"][1:])
    monkeypatch.setattr(lnd_release, "_DIGESTS", digests)

    with pytest.raises(lnd_release.LndReleaseError, match="lncli digest"):
        lnd_release.extract_verified(archive, "test-x", tmp_path / "bin")
    assert not (tmp_path / "bin").exists()
