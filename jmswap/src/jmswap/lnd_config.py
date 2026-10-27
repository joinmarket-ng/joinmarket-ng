"""Generated LND configuration for managed nodes.

Tor Lightning peer traffic, direct HTTP Bitcoin Core RPC polling (no ZMQ),
private production Taproot channels with SCID aliases, no REST listener, and
no bootstrapping to random peers. Tor activation does not protect Core RPC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jmcore.constants import BITCOIN_RPC_PORTS

from jmswap.managed_nodes import ManagedNode, ManagedNodeError, NodeState


def _rpc_target(value: str, default_port: int) -> tuple[str, str | None]:
    """Translate the representable address and retain only a static launch refusal.

    Read-only node RPC clients do not use this Core transport. Keeping a
    representable unsupported target inspectable must never approve a launch.
    """
    rejection = None
    if any(ord(char) < 33 or ord(char) == 127 or char == "\\" for char in value):
        rejection = (
            "Bitcoin Core RPC endpoint contains unsupported whitespace or control characters"
        )
    try:
        parsed = urlsplit(value if "://" in value else f"http://{value}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ManagedNodeError(
            "Bitcoin Core RPC endpoint has malformed host or port syntax"
        ) from None
    if not host:
        raise ManagedNodeError("bitcoin.rpc_url has no host")
    if parsed.scheme != "http":
        rejection = "Managed LND uses direct HTTP Core RPC, not HTTPS or a SOCKS URL; use a secured local tunnel or manual mode"
    elif parsed.username is not None or parsed.password is not None:
        rejection = "Managed LND requires separate Bitcoin RPC user/password settings or a cookie; URL credentials are unsupported"
    elif parsed.path not in {"", "/"} or "?" in value or "#" in value:
        rejection = "Managed LND cannot preserve Bitcoin RPC URL paths, queries, or fragments"
    elif host.lower().rstrip(".").endswith(".onion"):
        rejection = "Managed LND Core RPC does not use Tor; supply a direct secured tunnel endpoint instead of an onion hostname"
    elif port == 0 or parsed.netloc.endswith(":"):
        rejection = "Bitcoin Core RPC endpoint requires a port in 1..65535 when specified"
    return _host_port(host, default_port if port is None else port), rejection


def _host_port(host: str, port: int) -> str:
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        host = f"[{host}]"
    return f"{host}:{port}"


@dataclass(frozen=True)
class LndEnvironment:
    """Host services every managed node of this installation uses."""

    network: str
    lnd_binary: Path
    bitcoind_host: str
    bitcoind_cookie: Path | None = None
    bitcoind_user: str | None = None
    bitcoind_password: str | None = field(default=None, repr=False)
    tor_socks: str = "127.0.0.1:9050"
    tor_control: str = "127.0.0.1:9051"
    tor_password: str | None = field(default=None, repr=False)
    _rpc_launch_error: str | None = field(default=None, repr=False)

    def validate_for_launch(self) -> None:
        """Approve only supported launch transport, not unrelated existing-node reads."""
        if self._rpc_launch_error is not None:
            raise ManagedNodeError(self._rpc_launch_error)
        # Also check manually constructed environments. No stored refusal does
        # not by itself establish that this structured target is representable.
        if any(character in self.bitcoind_host for character in "/?#@"):
            raise ManagedNodeError(
                "Managed LND Core RPC target must be a hostname and optional port"
            )
        default_port = BITCOIN_RPC_PORTS.get(self.network)
        if default_port is None:
            raise ManagedNodeError("unsupported managed Bitcoin network")
        _, error = _rpc_target(self.bitcoind_host, default_port)
        if error is not None:
            raise ManagedNodeError(error)

    @classmethod
    def from_settings(cls, settings: Any, *, network: str, lnd_binary: Path) -> LndEnvironment:
        """Describe settings; launch/render must additionally validate_for_launch.

        Unsupported future-launch transport does not disable authenticated RPC
        inspection of an already-running node using its recorded credentials.
        """
        bitcoin = settings.bitcoin
        if bitcoin.backend_type == "neutrino":
            raise ManagedNodeError("managed Lightning nodes need a Bitcoin Core backend")
        target, rejection = _rpc_target(bitcoin.rpc_url, BITCOIN_RPC_PORTS[network])
        if bitcoin.backend_type != "descriptor_wallet":
            rejection = "managed Lightning nodes need the supported Bitcoin Core backend"
        tor = settings.tor
        if not tor.control_enabled:
            raise ManagedNodeError(
                "managed Lightning nodes need the Tor control port to create their onion services"
            )
        cookie = Path(bitcoin.rpc_cookie_file).expanduser() if bitcoin.rpc_cookie_file else None
        password = bitcoin.rpc_password.get_secret_value() if cookie is None else None
        tor_password = tor.password.get_secret_value() if tor.password is not None else None
        return cls(
            network=network,
            lnd_binary=lnd_binary,
            bitcoind_host=target,
            bitcoind_cookie=cookie,
            bitcoind_user=bitcoin.rpc_user if cookie is None else None,
            bitcoind_password=password,
            tor_socks=_host_port(tor.socks_host, tor.socks_port),
            tor_control=_host_port(tor.control_host, tor.control_port),
            tor_password=tor_password or None,
            _rpc_launch_error=rejection,
        )


def _value(value: str) -> str:
    if any(character in value for character in "\r\n"):
        raise ManagedNodeError("LND configuration values cannot contain line breaks")
    return value


def render_lnd_conf(node: ManagedNode, env: LndEnvironment, *, quarantined: bool = False) -> str:
    """The complete ``lnd.conf`` of one managed node."""
    env.validate_for_launch()
    refuse_inbound = quarantined or node.state in {NodeState.RETIRING, NodeState.RECOVERING}
    sections: dict[str, list[str]] = {
        "Application Options": [
            f"listen=127.0.0.1:{node.p2p_port}",
            f"rpclisten=127.0.0.1:{node.rpc_port}",
            "norest=true",
            "tlsdisableautofill=true",
            "nobootstrap=true",
            f"maxpendingchannels={0 if refuse_inbound else 4}",
            "acceptortimeout=120s",
            "debuglevel=info",
            "maxlogfiles=3",
            "maxlogfilesize=10",
        ],
        "Bitcoin": [f"bitcoin.{env.network}=true", "bitcoin.node=bitcoind"],
        "Bitcoind": [
            f"bitcoind.rpchost={_value(env.bitcoind_host)}",
            "bitcoind.rpcpolling=true",
            "bitcoind.blockpollinginterval=15s",
            "bitcoind.txpollinginterval=15s",
            "bitcoind.estimatemode=CONSERVATIVE",
        ],
        "tor": [
            "tor.active=true",
            "tor.v3=true",
            "tor.streamisolation=true",
            "tor.encryptkey=true",
            f"tor.socks={_value(env.tor_socks)}",
            f"tor.control={_value(env.tor_control)}",
        ],
        "logging": ["logging.console.disable=true"],
        "protocol": ["protocol.simple-taproot-chans=true", "protocol.option-scid-alias=true"],
    }
    if env.bitcoind_cookie is not None:
        sections["Bitcoind"].append(f"bitcoind.rpccookie={_value(str(env.bitcoind_cookie))}")
    else:
        sections["Bitcoind"].append(f"bitcoind.rpcuser={_value(env.bitcoind_user or '')}")
        sections["Bitcoind"].append(f"bitcoind.rpcpass={_value(env.bitcoind_password or '')}")
    if env.tor_password:
        sections["tor"].append(f"tor.password={_value(env.tor_password)}")
    lines = ["# Generated by jm-lightning; changes are overwritten on every start."]
    for name, options in sections.items():
        lines.extend(["", f"[{name}]", *options])
    return "\n".join(lines) + "\n"
