"""Unit tests for the reference-container directory onion probe.

The probe gates the reference E2E suites: it proves that the legacy client can
actually reach the generated hidden service and complete a v5 handshake, rather
than only proving that Tor wrote a hostname file.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "tests/e2e/reference/check_directory_onion.py"
)
_SPEC = importlib.util.spec_from_file_location("check_directory_onion", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
check_directory_onion_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_directory_onion_module)

check_directory_onion = check_directory_onion_module.check_directory_onion

ONION = "a" * 56 + ".onion"


def _read_socks_request(conn: socket.socket) -> tuple[str, int]:
    """Consume a SOCKS5 greeting plus CONNECT request and return the target."""
    greeting = conn.recv(3)
    assert greeting[:1] == b"\x05"
    conn.sendall(b"\x05\x00")

    header = conn.recv(4)
    assert header[:2] == b"\x05\x01"
    assert header[3] == 3, "probe must use SOCKS5 domain addressing for onions"
    host_length = conn.recv(1)[0]
    host = conn.recv(host_length).decode("ascii")
    port = struct.unpack("!H", conn.recv(2))[0]
    return host, port


def _socks_success_reply() -> bytes:
    return b"\x05\x00\x00\x01" + b"\x00\x00\x00\x00" + struct.pack("!H", 0)


@pytest.fixture
def socks_server() -> Iterator[Callable[[Callable[[socket.socket], None]], int]]:
    """Serve exactly one SOCKS5 connection with a caller-supplied handler."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    thread: threading.Thread | None = None

    def serve(handler: Callable[[socket.socket], None]) -> int:
        nonlocal thread

        def run() -> None:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(5.0)
                handler(conn)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return int(server.getsockname()[1])

    try:
        yield serve
    finally:
        if thread is not None:
            thread.join(timeout=5.0)
        server.close()


def _directory_response(accepted: bool) -> bytes:
    inner = {
        "app-name": "joinmarket",
        "directory": True,
        "proto-ver-min": 5,
        "proto-ver-max": 5,
        "features": {},
        "accepted": accepted,
    }
    outer = {"type": 795, "line": json.dumps(inner)}
    return json.dumps(outer).encode("ascii") + b"\r\n"


def test_accepts_reachable_directory(socks_server: Any) -> None:
    """A completed SOCKS CONNECT plus accepted handshake passes the probe."""
    observed: dict[str, Any] = {}

    def handler(conn: socket.socket) -> None:
        observed["target"] = _read_socks_request(conn)
        conn.sendall(_socks_success_reply())
        observed["handshake"] = conn.recv(4096)
        conn.sendall(_directory_response(accepted=True))

    port = socks_server(handler)

    check_directory_onion(ONION, socks_port=port, timeout=5.0)

    assert observed["target"] == (ONION, 5222)
    frame = json.loads(observed["handshake"].decode("ascii").strip())
    assert frame["type"] == 793
    assert json.loads(frame["line"])["proto-ver"] == 5


def test_rejects_unreachable_onion(socks_server: Any) -> None:
    """A non-zero SOCKS reply means the hidden service is not reachable yet."""

    def handler(conn: socket.socket) -> None:
        _read_socks_request(conn)
        # REP=0x04 (host unreachable), the descriptor-not-published case.
        conn.sendall(b"\x05\x04\x00\x01" + b"\x00\x00\x00\x00" + struct.pack("!H", 0))

    port = socks_server(handler)

    with pytest.raises(ConnectionError, match="could not reach directory onion"):
        check_directory_onion(ONION, socks_port=port, timeout=5.0)


def test_rejects_directory_that_never_answers_handshake(socks_server: Any) -> None:
    """A silent directory must fail the probe instead of appearing healthy."""

    def handler(conn: socket.socket) -> None:
        _read_socks_request(conn)
        conn.sendall(_socks_success_reply())
        conn.recv(4096)
        # Close without replying, matching the observed stalled-handshake failure.

    port = socks_server(handler)

    with pytest.raises(ConnectionError, match="SOCKS proxy closed the connection"):
        check_directory_onion(ONION, socks_port=port, timeout=5.0)


def test_rejects_refused_handshake(socks_server: Any) -> None:
    """A directory that answers but refuses the handshake is not ready."""

    def handler(conn: socket.socket) -> None:
        _read_socks_request(conn)
        conn.sendall(_socks_success_reply())
        conn.recv(4096)
        conn.sendall(_directory_response(accepted=False))

    port = socks_server(handler)

    with pytest.raises(ConnectionError, match="rejected probe handshake"):
        check_directory_onion(ONION, socks_port=port, timeout=5.0)
