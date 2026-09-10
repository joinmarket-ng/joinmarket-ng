#!/usr/bin/env python3
"""Diagnose a maker's directory relay and direct orderbook responses."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import re
import struct
import sys
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

# Permit running this standalone script from a source checkout.
sys.path.insert(0, str(Path(__file__).parent.parent / "jmcore" / "src"))

from jmcore.crypto import (
    NickIdentity,
    verify_bitcoin_message_signature,
    verify_signed_privmsg,
)
from jmcore.directory_client import DirectoryClient
from jmcore.models import DIRECTORY_NODES_SIGNET, Offer, OfferType
from jmcore.network import ONION_HOSTID, OnionPeer
from jmcore.nick_auth import (
    NickAuthMode,
    directory_id_for_endpoint,
    validate_directory_endpoint,
)
from jmcore.protocol import MessageType, is_valid_nick

DEFAULT_MAINNET_DIRECTORY = (
    "nakamotourflxwjnjpnrk7yc2nhkf6r62ed4gdfxmmn5f4saw5q5qoyd.onion:5222"
)
MAX_ROUTE_MESSAGES = 1_000
MAX_ROUTE_OFFERS = 1_000
MAX_ROUTE_BONDS = 1_000
MAX_COMMANDS_PER_MESSAGE = 1_000
MAX_BOND_PROOF_BASE64_LENGTH = 336
MAX_REPORTED_ERRORS = 100


class RouteResult(BaseModel):
    """Responses collected through one maker communication route."""

    name: str
    connected: bool = False
    requested: bool = False
    offers: list[Offer] = Field(default_factory=list)
    bonds: dict[str, dict[str, Any]] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    messages_received: int = 0
    offer_lines: list[str] = Field(default_factory=list)

    @property
    def successful(self) -> bool:
        return (
            self.connected and self.requested and bool(self.offers) and not self.errors
        )


class DiagnosisResult(BaseModel):
    """The complete, serializable result of a maker diagnosis."""

    maker_nick: str
    taker_nick: str
    network: str
    directory: str
    maker_address: str | None = None
    discovery: str = ""
    directory_nick_authenticated: bool = False
    directory_result: RouteResult
    direct_result: RouteResult

    @property
    def successful(self) -> bool:
        routes = (self.directory_result, self.direct_result)
        return all(
            route.successful and _route_bonds_are_valid(route) for route in routes
        )


def _route_bonds_are_valid(result: RouteResult) -> bool:
    """Return whether every reported bond has both valid signatures."""
    for analysis in result.bonds.values():
        if (
            analysis.get("nick_signature", {}).get("valid") is not True
            or analysis.get("cert_signature", {}).get("valid") is not True
        ):
            return False
    return True


def normalize_onion_endpoint(value: str) -> str:
    """Return a canonical v3 onion host:port, adding port 5222 when omitted."""
    endpoint = value if ":" in value else f"{value}:5222"
    validate_directory_endpoint(endpoint)
    host, port_text = endpoint.rsplit(":", 1)
    port = int(port_text)
    # This validates the host is a Tor v3 onion, not merely a host:port string.
    directory_id_for_endpoint(host, port)
    return f"{host.lower()}:{port}"


def directory_endpoint(value: str) -> str:
    """Argparse adapter for an onion endpoint."""
    try:
        return normalize_onion_endpoint(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def maker_endpoint(value: str) -> str:
    """Argparse adapter for an explicit direct maker onion:port override."""
    if ":" not in value:
        raise argparse.ArgumentTypeError("maker address must use onion:port")
    return directory_endpoint(value)


def maker_nick_argument(value: str) -> str:
    """Validate the positional maker nick before any network operation."""
    if not is_valid_nick(value):
        raise argparse.ArgumentTypeError(
            f"invalid maker nick {value!r}: expected the canonical JoinMarket nick format"
        )
    return value


def positive_timeout(value: str) -> float:
    """Parse a finite timeout greater than zero."""
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number")
    return timeout


def _add_error(result: RouteResult, message: str) -> None:
    """Retain bounded, non-sensitive diagnostics for malformed peer input."""
    if len(result.errors) < MAX_REPORTED_ERRORS:
        result.errors.append(message)
        logger.warning("[{}] {}", result.name, message)
    elif len(result.errors) == MAX_REPORTED_ERRORS:
        result.errors.append("Too many route errors; later errors were suppressed")


def _record_bond(
    proof_b64: str,
    maker_nick: str,
    taker_nick: str,
    result: RouteResult,
) -> bool:
    """Analyze one bond proof, returning whether collection must stop."""
    if proof_b64 in result.bonds:
        return False
    if len(result.bonds) >= MAX_ROUTE_BONDS:
        _add_error(result, f"Bond collection limit reached ({MAX_ROUTE_BONDS})")
        return True
    if len(proof_b64) > MAX_BOND_PROOF_BASE64_LENGTH:
        _add_error(
            result,
            f"Bond proof exceeds the {MAX_BOND_PROOF_BASE64_LENGTH}-character wire limit",
        )
        result.bonds[f"oversized-proof-{len(proof_b64)}"] = {
            "error": "Bond proof was not decoded because it exceeds the wire limit",
            "proof_length": len(proof_b64),
        }
        return False

    # Keep the original proof as the key for report consumers. Parsing is intentionally
    # attempted even when either contained signature is invalid.
    result.bonds[proof_b64] = parse_bond_proof(proof_b64, maker_nick, taker_nick)
    return False


def record_response(
    message: dict[str, Any],
    maker_nick: str,
    taker_nick: str,
    result: RouteResult,
) -> bool:
    """Validate and collect one signed maker response; return whether to stop."""
    if message.get("type") != MessageType.PRIVMSG.value:
        return False

    line = message.get("line")
    if not isinstance(line, str):
        return False
    if not line.startswith(f"{maker_nick}!"):
        return False

    parts = line.split("!", 2)
    if len(parts) != 3:
        _add_error(result, "Maker PRIVMSG envelope has an invalid format")
        return False
    sender, recipient, rest = parts
    if sender != maker_nick or recipient != taker_nick:
        return False

    signature_valid, command, data = verify_signed_privmsg(
        maker_nick, rest, ONION_HOSTID
    )
    if not signature_valid:
        tokens = rest.split()
        for offer_type in OfferType:
            if (
                tokens
                and tokens[0].startswith(offer_type.value)
                and tokens[0] != offer_type.value
            ):
                _add_error(
                    result,
                    f"Malformed offer command {tokens[0]!r}: missing space after {offer_type.value}",
                )
                break
        if len(tokens) < 3 or re.fullmatch(r"0[23][0-9a-fA-F]{64}", tokens[-2]) is None:
            _add_error(
                result,
                "Maker response is unsigned or has a malformed public-key/signature suffix",
            )
        else:
            _add_error(result, "Maker response has an invalid message signature")
        logger.debug(
            "[{}] Rejected PRIVMSG: expected '<command> <data> <pubkey_hex> <signature>'; "
            "sender={!r}, recipient={!r}, content={!r}",
            result.name,
            sender,
            recipient,
            rest,
        )
        return False

    reconstructed = f"{command} {data}"
    commands = reconstructed.split("!", MAX_COMMANDS_PER_MESSAGE)
    if len(commands) == MAX_COMMANDS_PER_MESSAGE + 1:
        _add_error(
            result, f"Maker response command limit reached ({MAX_COMMANDS_PER_MESSAGE})"
        )
        commands = commands[:MAX_COMMANDS_PER_MESSAGE]

    for command_line in commands:
        fields = command_line.split()
        if not fields:
            continue

        if fields[0] == "tbond":
            if len(fields) < 2:
                _add_error(result, "Maker sent a fidelity bond command without a proof")
                continue
            if _record_bond(fields[1], maker_nick, taker_nick, result):
                return True
            continue

        try:
            offer_type = OfferType(fields[0])
        except ValueError:
            # Other valid JoinMarket commands may be bundled with the response.
            continue

        if len(fields) != 6:
            _add_error(result, "Maker offer has an invalid field count")
            continue
        if len(result.offers) >= MAX_ROUTE_OFFERS:
            _add_error(result, f"Offer collection limit reached ({MAX_ROUTE_OFFERS})")
            return True

        try:
            offer = Offer(
                counterparty=maker_nick,
                ordertype=offer_type,
                oid=int(fields[1]),
                minsize=int(fields[2]),
                maxsize=int(fields[3]),
                txfee=int(fields[4]),
                cjfee=fields[5],
            )
        except (ValidationError, ValueError, OverflowError) as exc:
            _add_error(result, "Maker offer failed shared Offer validation")
            logger.debug("[{}] Offer validation error: {}", result.name, exc)
            continue

        result.offers.append(offer)
        result.offer_lines.append(command_line)
        logger.info("[{}] Verified offer: {}", result.name, command_line)

    return False


def _accept_route_message(result: RouteResult) -> bool:
    """Count an incoming message and report whether its route limit was reached."""
    if result.messages_received >= MAX_ROUTE_MESSAGES:
        _add_error(result, f"Message collection limit reached ({MAX_ROUTE_MESSAGES})")
        return True
    result.messages_received += 1
    if result.messages_received == MAX_ROUTE_MESSAGES:
        _add_error(result, f"Message collection limit reached ({MAX_ROUTE_MESSAGES})")
    return result.messages_received >= MAX_ROUTE_MESSAGES


async def _discover_maker_endpoint(
    client: DirectoryClient, maker_nick: str, timeout: float
) -> tuple[str, str | None]:
    """Fetch a completed peerlist snapshot without making unknown data authoritative."""
    try:
        logger.debug(
            "[directory] SEND {}", {"type": MessageType.GETPEERLIST.value, "line": ""}
        )
        snapshot = await asyncio.wait_for(
            client.get_authoritative_peerlist_snapshot(), timeout=timeout + 5.0
        )
    except Exception as exc:
        logger.debug("Peerlist lookup failed: {!r}", exc)
        return f"unknown (peerlist lookup failed: {type(exc).__name__}: {exc})", None

    if snapshot is None:
        return "unknown (directory did not provide a complete peerlist snapshot)", None

    matches = [peer for peer in snapshot.peers if peer[0] == maker_nick]
    if not matches:
        return "complete peerlist snapshot does not contain the maker", None

    _nick, location, features = matches[0]
    feature_text = ", ".join(sorted(features.features)) or "none advertised"
    logger.info("Discovered maker endpoint: {}", location)
    logger.info("Discovered maker features: {}", feature_text)
    if location == "NOT-SERVING-ONION":
        return "maker is not serving an onion endpoint", None

    try:
        return f"discovered {location}", normalize_onion_endpoint(location)
    except ValueError:
        return "directory returned an invalid maker onion endpoint", None


async def _collect_directory_responses(
    client: DirectoryClient,
    maker_nick: str,
    taker_nick: str,
    timeout: float,
    result: RouteResult,
) -> None:
    """Request and collect the target maker's relayed orderbook response."""
    logger.debug(
        "[directory] SEND {}",
        {"type": MessageType.PUBMSG.value, "line": f"{taker_nick}!PUBLIC!orderbook"},
    )
    await client.send_public_message("orderbook")
    result.requested = True
    logger.info("[directory] Waiting up to {}s for relayed offers", timeout)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while (remaining := deadline - loop.time()) > 0:
        try:
            messages = await client.listen_for_messages(duration=min(5.0, remaining))
        except Exception as exc:
            _add_error(
                result,
                f"Directory response collection failed: {type(exc).__name__}: {exc}",
            )
            break

        stop = False
        for message in messages:
            logger.debug("[directory] RECV {!r}", message)
            reached_message_limit = _accept_route_message(result)
            stop = (
                record_response(message, maker_nick, taker_nick, result)
                or reached_message_limit
            )
            if stop:
                break
        if stop:
            break
    logger.info(
        "[directory] Collection complete: {} verified offers", len(result.offers)
    )


async def _run_directory_phase(
    maker_nick: str,
    identity: NickIdentity,
    directory: str,
    network: str,
    timeout: float,
    nick_auth_mode: NickAuthMode,
    result: RouteResult,
) -> tuple[str, str | None, bool]:
    """Connect, discover, request directory offers, and deterministically close."""
    host, port_text = directory.rsplit(":", 1)
    client: DirectoryClient | None = None
    discovery = "unknown (directory connection was not established)"
    discovered_endpoint: str | None = None
    authenticated = False

    try:
        logger.info("[directory] Connecting to {} on {}", directory, network)
        client = DirectoryClient(
            host,
            int(port_text),
            network,
            identity,
            timeout=timeout,
            peerlist_timeout=timeout,
            nick_auth_mode=nick_auth_mode,
        )
        await client.connect()
        result.connected = True
        authenticated = client.directory_nick_authenticated
        logger.info(
            "[directory] Handshake complete, nick authenticated: {}", authenticated
        )

        discovery, discovered_endpoint = await _discover_maker_endpoint(
            client, maker_nick, timeout
        )
        logger.info("Discovery: {}", discovery)

        # get_authoritative_peerlist_snapshot may buffer unsolicited peer messages. Discard
        # them before the request so only responses received after requested=True are counted.
        try:
            buffered = await client.listen_for_messages(duration=0.0)
            for message in buffered:
                logger.debug(
                    "[directory] RECV before request (not counted): {!r}", message
                )
        except Exception as exc:
            _add_error(
                result,
                f"Directory pre-request drain failed: {type(exc).__name__}: {exc}",
            )

        await _collect_directory_responses(
            client, maker_nick, identity.nick, timeout, result
        )
    except Exception as exc:
        _add_error(result, f"Directory route failed: {type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                _add_error(
                    result, f"Directory close failed: {type(exc).__name__}: {exc}"
                )

    return discovery, discovered_endpoint, authenticated


def _warn_unexpected_direct_nick(
    message: dict[str, Any],
    maker_nick: str,
    taker_nick: str,
    result: RouteResult,
) -> None:
    """Report a verified alternate identity without attributing its offers to the target."""
    if message.get("type") != MessageType.PRIVMSG.value:
        return
    line = message.get("line")
    if not isinstance(line, str):
        return
    fields = line.split("!", 2)
    if len(fields) != 3:
        return
    sender, recipient, content = fields
    if sender == maker_nick or recipient != taker_nick or not is_valid_nick(sender):
        return
    verified, _command, _data = verify_signed_privmsg(sender, content, ONION_HOSTID)
    if not verified:
        return
    warning = (
        f"Direct endpoint returned a valid signed response from {sender}, "
        f"but the requested maker nick is {maker_nick}. "
        f"The maker nick may have changed; retry with {sender}. "
        "This response is not counted as an offer from the requested maker."
    )
    if warning not in result.warnings:
        result.warnings.append(warning)
        logger.warning("[direct] {}", warning)


async def _run_direct_phase(
    maker_nick: str,
    identity: NickIdentity,
    maker_address: str | None,
    network: str,
    timeout: float,
    result: RouteResult,
) -> None:
    """Request the orderbook directly after the directory connection has closed."""
    if maker_address is None:
        _add_error(
            result, "No usable maker onion endpoint is available for the direct route"
        )
        return

    done = asyncio.Event()
    collecting = True

    async def on_message(_nick: str, raw: bytes) -> None:
        logger.debug("[direct] RECV {!r}", raw)
        if not result.requested or not collecting or done.is_set():
            logger.debug(
                "[direct] Ignoring message outside the response collection window"
            )
            return
        reached_message_limit = _accept_route_message(result)
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _add_error(result, "Direct maker response has an invalid JSON envelope")
            if reached_message_limit:
                done.set()
            return
        if isinstance(envelope, dict):
            _warn_unexpected_direct_nick(envelope, maker_nick, identity.nick, result)
            if record_response(envelope, maker_nick, identity.nick, result):
                done.set()
        else:
            _add_error(result, "Direct maker response envelope is not an object")
        if reached_message_limit:
            done.set()

    async def on_disconnect(_nick: str) -> None:
        if collecting:
            if result.requested and result.offers:
                logger.info(
                    "[direct] Maker disconnected after {} verified offers",
                    len(result.offers),
                )
            elif result.requested:
                _add_error(
                    result, "Direct maker disconnected before a valid offer response"
                )
            else:
                logger.warning(
                    "[direct] Maker disconnected before the orderbook request"
                )
        done.set()

    peer = OnionPeer(
        nick=maker_nick,
        location=maker_address,
        nick_identity=identity,
        timeout=timeout,
        on_message=on_message,
        on_disconnect=on_disconnect,
    )
    try:
        logger.info("[direct] Connecting to {} on {}", maker_address, network)
        if not await peer.connect(identity.nick, "NOT-SERVING-ONION", network):
            _add_error(result, "Direct maker connection or handshake failed")
            return
        result.connected = True
        feature_text = ", ".join(sorted(peer.peer_features)) or "none advertised"
        logger.info("[direct] Handshake complete, maker features: {}", feature_text)

        result.requested = True
        request = {
            "type": MessageType.PUBMSG.value,
            "line": f"{identity.nick}!PUBLIC!orderbook",
        }
        wire_request = json.dumps(request).encode("utf-8")
        logger.debug("[direct] SEND {!r}", wire_request)
        if not await peer.send(wire_request):
            _add_error(result, "Direct orderbook request could not be sent")
            return

        logger.info("[direct] Waiting up to {}s for offers", timeout)
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except TimeoutError:
            logger.debug("[direct] Response collection timeout reached")
        logger.info(
            "[direct] Collection complete: {} verified offers", len(result.offers)
        )
    except Exception as exc:
        _add_error(result, f"Direct route failed: {type(exc).__name__}: {exc}")
    finally:
        collecting = False
        try:
            await peer.disconnect()
        except Exception as exc:
            _add_error(result, f"Direct close failed: {type(exc).__name__}: {exc}")


async def diagnose_maker(
    maker_nick: str,
    directory: str | None = None,
    network: str = "mainnet",
    timeout: float = 60.0,
    nick_auth_mode: NickAuthMode = NickAuthMode.PREFER_VERIFIED,
    maker_address: str | None = None,
) -> DiagnosisResult:
    """Diagnose one maker through its directory relay and onion endpoint."""
    if not is_valid_nick(maker_nick):
        raise ValueError("maker_nick must use the canonical JoinMarket nick format")
    if network not in {"mainnet", "signet"}:
        raise ValueError("network must be mainnet or signet")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")

    selected_directory = normalize_onion_endpoint(
        directory
        if directory is not None
        else DIRECTORY_NODES_SIGNET[0]
        if network == "signet"
        else DEFAULT_MAINNET_DIRECTORY
    )
    if maker_address is not None and ":" not in maker_address:
        raise ValueError("maker_address must use onion:port")
    override = (
        normalize_onion_endpoint(maker_address) if maker_address is not None else None
    )
    auth_mode = NickAuthMode(nick_auth_mode)
    identity = NickIdentity()
    directory_result = RouteResult(name="directory")
    direct_result = RouteResult(name="direct")

    discovery, discovered_endpoint, authenticated = await _run_directory_phase(
        maker_nick,
        identity,
        selected_directory,
        network,
        timeout,
        auth_mode,
        directory_result,
    )
    chosen_endpoint = override or discovered_endpoint
    if override is not None:
        logger.info("Direct endpoint override: {}", override)
        if discovered_endpoint is not None and override != discovered_endpoint:
            logger.info(
                "Discovered endpoint differs from override: {}", discovered_endpoint
            )

    # The directory client has closed before this independent direct connection begins.
    await _run_direct_phase(
        maker_nick, identity, chosen_endpoint, network, timeout, direct_result
    )
    return DiagnosisResult(
        maker_nick=maker_nick,
        taker_nick=identity.nick,
        network=network,
        directory=selected_directory,
        maker_address=chosen_endpoint,
        discovery=discovery,
        directory_nick_authenticated=authenticated,
        directory_result=directory_result,
        direct_result=direct_result,
    )


def parse_bond_proof(
    proof_b64: str, maker_nick: str, taker_nick: str
) -> dict[str, Any]:
    """Parse a fidelity bond proof and retain its detailed signature analysis."""
    try:
        proof_bytes = base64.b64decode(proof_b64)
    except Exception as exc:
        return {"error": f"Failed to decode base64: {exc}"}

    if len(proof_bytes) != 252:
        return {"error": f"Invalid proof length: {len(proof_bytes)}, expected 252"}

    try:
        unpacked = struct.unpack("<72s72s33sH33s32sII", proof_bytes)
    except Exception as exc:
        return {"error": f"Failed to unpack: {exc}"}

    (
        nick_sig_padded,
        cert_sig_padded,
        cert_pub,
        cert_expiry_encoded,
        utxo_pub,
        txid_bytes,
        vout,
        locktime,
    ) = unpacked

    try:
        nick_sig_start = nick_sig_padded.index(b"\x30")
        nick_sig = nick_sig_padded[nick_sig_start:]
    except ValueError:
        nick_sig = None
        nick_sig_start = -1

    try:
        cert_sig_start = cert_sig_padded.index(b"\x30")
        cert_sig = cert_sig_padded[cert_sig_start:]
    except ValueError:
        cert_sig = None
        cert_sig_start = -1

    nick_msg = (taker_nick + "|" + maker_nick).encode("ascii")
    nick_sig_valid: bool | str | None = None
    if nick_sig and cert_pub:
        try:
            nick_sig_valid = verify_bitcoin_message_signature(
                nick_msg, nick_sig, cert_pub
            )
        except Exception as exc:
            nick_sig_valid = f"Error: {exc}"

    cert_msg_binary = (
        b"fidelity-bond-cert|"
        + cert_pub
        + b"|"
        + str(cert_expiry_encoded).encode("ascii")
    )
    cert_msg_ascii = (
        b"fidelity-bond-cert|"
        + cert_pub.hex().encode("ascii")
        + b"|"
        + str(cert_expiry_encoded).encode("ascii")
    )
    cert_sig_valid: bool | str | None = None
    cert_sig_format: str | None = None
    cert_msg_used = cert_msg_binary
    if cert_sig and utxo_pub:
        try:
            if verify_bitcoin_message_signature(cert_msg_binary, cert_sig, utxo_pub):
                cert_sig_valid = True
                cert_sig_format = "binary"
            elif verify_bitcoin_message_signature(cert_msg_ascii, cert_sig, utxo_pub):
                cert_sig_valid = True
                cert_sig_format = "ascii"
                cert_msg_used = cert_msg_ascii
            else:
                cert_sig_valid = False
        except Exception as exc:
            cert_sig_valid = f"Error: {exc}"

    txid_display = txid_bytes.hex()
    return {
        "proof_length": len(proof_bytes),
        "nick_signature": {
            "padded_hex": nick_sig_padded.hex(),
            "der_start_offset": nick_sig_start,
            "der_sig_hex": nick_sig.hex() if nick_sig else None,
            "message": nick_msg.decode("ascii"),
            "message_hex": nick_msg.hex(),
            "valid": nick_sig_valid,
        },
        "cert_signature": {
            "padded_hex": cert_sig_padded.hex(),
            "der_start_offset": cert_sig_start,
            "der_sig_hex": cert_sig.hex() if cert_sig else None,
            "message": cert_msg_used.decode("ascii", errors="replace"),
            "message_hex": cert_msg_used.hex(),
            "message_format": cert_sig_format,
            "valid": cert_sig_valid,
        },
        "cert_pubkey": {
            "hex": cert_pub.hex(),
            "compressed": len(cert_pub) == 33,
            "prefix": hex(cert_pub[0]) if cert_pub else None,
        },
        "cert_expiry": {
            "encoded": cert_expiry_encoded,
            "blocks": cert_expiry_encoded * 2016,
            "explanation": f"{cert_expiry_encoded} retarget periods = {cert_expiry_encoded * 2016} blocks",
        },
        "utxo_pubkey": {
            "hex": utxo_pub.hex(),
            "compressed": len(utxo_pub) == 33,
            "prefix": hex(utxo_pub[0]) if utxo_pub else None,
            "matches_cert_pub": utxo_pub == cert_pub,
        },
        "utxo": {
            "txid": txid_display,
            "vout": vout,
            "outpoint": f"{txid_display}:{vout}",
        },
        "locktime": {
            "value": locktime,
            "unix_timestamp": locktime,
        },
    }


def print_analysis(data: dict[str, Any], indent: int = 0) -> None:
    """Pretty-print an existing fidelity bond analysis using plain boolean labels."""
    prefix = "  " * indent
    if "error" in data:
        print(f"{prefix}ERROR: {data['error']}")
        return
    for key, value in data.items():
        if isinstance(value, dict):
            print(f"{prefix}{key}:")
            print_analysis(value, indent + 1)
        elif isinstance(value, bool):
            print(f"{prefix}{key}: {str(value).lower()}")
        elif isinstance(value, str) and key.endswith("_hex") and len(value) > 80:
            print(f"{prefix}{key}: {value[:60]}...")
        else:
            print(f"{prefix}{key}: {value}")


def _print_route_result(result: RouteResult) -> None:
    """Render one route without treating a missing bond as a connection failure."""
    print(f"{result.name.title()} route result:")
    print(f"  connected: {str(result.connected).lower()}")
    print(f"  orderbook requested: {str(result.requested).lower()}")
    print(f"  messages received: {result.messages_received}")
    if result.offers:
        print(f"  offers: {len(result.offers)}")
        for index, offer in enumerate(result.offers):
            raw_line = result.offer_lines[index]
            print(f"    raw offer: {raw_line}")
            print(
                "    validated: "
                f"oid={offer.oid}, type={offer.ordertype.value}, "
                f"size={offer.minsize}-{offer.maxsize}, txfee={offer.txfee}, cjfee={offer.cjfee}"
            )
            print("    message signature: valid")
    else:
        print("  offers: none")
    if result.bonds:
        print(f"  fidelity bonds: {len(result.bonds)}")
        for index, (proof, analysis) in enumerate(result.bonds.items(), start=1):
            print(f"    bond {index} (base64 length: {len(proof)}):")
            print_analysis(analysis, 3)
    else:
        print("  fidelity bonds: none")
    if result.errors:
        print("  errors:")
        for error in result.errors:
            print(f"    {error}")
    if result.warnings:
        print("  warnings:")
        for warning in result.warnings:
            print(f"    {warning}")
    print(
        f"  status: {'successful' if result.successful and _route_bonds_are_valid(result) else 'incomplete'}"
    )


def print_diagnosis(result: DiagnosisResult) -> None:
    """Render the complete diagnostic report."""
    print("Maker diagnosis")
    print(f"Maker nick: {result.maker_nick}")
    print(f"Taker nick: {result.taker_nick}")
    print(f"Network: {result.network}")
    print(f"Directory: {result.directory}")
    print(
        f"Directory nick authenticated: {str(result.directory_nick_authenticated).lower()}"
    )
    print(f"Discovery: {result.discovery}")
    print(f"Direct endpoint: {result.maker_address or 'unavailable'}")
    _print_route_result(result.directory_result)
    _print_route_result(result.direct_result)
    print(f"Overall status: {'successful' if result.successful else 'incomplete'}")


def _configure_logging(level: str) -> None:
    """Set the shared Loguru threshold for this standalone command."""
    logger.remove()
    logger.add(sys.stderr, level=level)


def build_parser() -> argparse.ArgumentParser:
    """Build the single-command maker diagnostic CLI."""
    parser = argparse.ArgumentParser(description="Diagnose a JoinMarket maker")
    parser.add_argument(
        "maker_nick", type=maker_nick_argument, help="Maker JoinMarket nick"
    )
    parser.add_argument(
        "--directory",
        type=directory_endpoint,
        help="Directory onion:port (bare onion defaults to port 5222)",
    )
    parser.add_argument("--network", choices=("mainnet", "signet"), default="mainnet")
    parser.add_argument(
        "--nick-auth-mode",
        choices=[mode.value for mode in NickAuthMode],
        default=NickAuthMode.PREFER_VERIFIED.value,
    )
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=60.0,
        help="Per-phase timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--maker-address",
        type=maker_endpoint,
        help="Explicit maker onion:port to use when discovery is unavailable",
    )
    parser.add_argument(
        "--output", help="Write the JSON diagnostic report to this path"
    )
    parser.add_argument(
        "--log-level",
        choices=("TRACE", "DEBUG", "INFO", "WARNING", "ERROR"),
        default="DEBUG",
        help="Loguru logging level (default: DEBUG, includes sent and received orderbook messages)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the diagnostic command, returning shell-compatible status codes."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    _configure_logging(args.log_level)
    try:
        result = asyncio.run(
            diagnose_maker(
                args.maker_nick,
                directory=args.directory,
                network=args.network,
                timeout=args.timeout,
                nick_auth_mode=NickAuthMode(args.nick_auth_mode),
                maker_address=args.maker_address,
            )
        )
    except KeyboardInterrupt:
        print("Diagnosis interrupted", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Diagnosis failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    print_diagnosis(result)
    if args.output:
        try:
            Path(args.output).write_text(
                result.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            print(
                f"Could not write diagnostic report: {type(exc).__name__}",
                file=sys.stderr,
            )
            return 1
    return 0 if result.successful else 1


if __name__ == "__main__":
    sys.exit(main())
