#!/usr/bin/env python3
"""Verify a directory onion through the reference container's SOCKS proxy."""

from __future__ import annotations

import argparse
import json
import socket
import struct


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("SOCKS proxy closed the connection")
        data.extend(chunk)
    return bytes(data)


def _consume_socks_address(sock: socket.socket, address_type: int) -> None:
    if address_type == 1:
        _recv_exact(sock, 4)
    elif address_type == 3:
        _recv_exact(sock, _recv_exact(sock, 1)[0])
    elif address_type == 4:
        _recv_exact(sock, 16)
    else:
        raise ConnectionError(f"Unsupported SOCKS address type: {address_type}")
    _recv_exact(sock, 2)


def check_directory_onion(
    onion: str,
    *,
    directory_port: int = 5222,
    socks_host: str = "127.0.0.1",
    socks_port: int = 9050,
    timeout: float = 20.0,
) -> None:
    host = onion.removesuffix(".onion") + ".onion"
    encoded_host = host.encode("ascii")
    if len(encoded_host) > 255:
        raise ValueError("Onion hostname is too long for SOCKS5 domain encoding")

    with socket.create_connection((socks_host, socks_port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2) != b"\x05\x00":
            raise ConnectionError("SOCKS proxy rejected unauthenticated negotiation")

        request = (
            b"\x05\x01\x00\x03"
            + bytes([len(encoded_host)])
            + encoded_host
            + struct.pack("!H", directory_port)
        )
        sock.sendall(request)
        response = _recv_exact(sock, 4)
        if response[:1] != b"\x05" or response[1] != 0:
            raise ConnectionError(
                f"SOCKS proxy could not reach directory onion: {response.hex()}"
            )
        _consume_socks_address(sock, response[3])

        handshake = {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": "NOT-SERVING-ONION",
            "proto-ver": 5,
            "features": {},
            "nick": "J500000000000000",
            "network": "testnet",
        }
        frame = {"type": 793, "line": json.dumps(handshake, separators=(",", ":"))}
        sock.sendall(json.dumps(frame, separators=(",", ":")).encode("ascii") + b"\r\n")

        response_line = bytearray()
        while not response_line.endswith(b"\n"):
            response_line.extend(_recv_exact(sock, 1))
            if len(response_line) > 65_536:
                raise ConnectionError("Directory handshake response exceeded 64 KiB")

        outer = json.loads(response_line)
        if outer.get("type") != 795:
            raise ConnectionError(
                f"Unexpected directory response type: {outer.get('type')!r}"
            )
        inner = json.loads(outer.get("line", "{}"))
        if inner.get("accepted") is not True:
            raise ConnectionError(f"Directory rejected probe handshake: {inner!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("onion")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    check_directory_onion(args.onion, timeout=args.timeout)


if __name__ == "__main__":
    main()
