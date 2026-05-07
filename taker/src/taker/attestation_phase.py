"""Taker-side coordination for tx-extension !attestreq/!attest exchange.

Two layers, both transport-agnostic:

* Decoder layer (``decode_attest_reply``, ``log_decode_failures``):
  pure functions that turn a maker's encrypted directory-server
  response into an :class:`AttestPayload` or a structured error.
* Orchestrator layer (``build_attestreq_for_maker``,
  ``run_attestation_round``): fan-out a single ring across all
  selected makers (each gets the same ring but a different
  ``signer_idx``), await replies, and collect decoded payloads.

The orchestrator deliberately uses the same all-at-once
send-then-wait pattern as ``Taker._phase_auth`` rather than the
wave-based scheduler in :mod:`jmcore.attestation_collector`; the
collector is shaped for unicast HTTPS-style transports, whereas the
attestation phase here runs over the directory-relayed PRIVMSG
channel where one ``wait_for_responses`` covers the whole fan-out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from jmcore.attestation_wire import (
    AttestPayload,
    AttestReqPayload,
    AttestWireError,
    decode_attest,
    encode_attestreq,
)
from jmcore.clsag_attestation import RingMember
from loguru import logger


class _DecryptingCrypto(Protocol):
    """Minimal interface the decoder needs from a maker session crypto."""

    def decrypt(self, ciphertext: str) -> str: ...


class _EncryptingCrypto(Protocol):
    """Minimal interface the orchestrator needs from a maker session crypto."""

    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str: ...


class _DirectoryClient(Protocol):
    """Subset of MultiDirectoryClient used by run_attestation_round."""

    async def send_privmsg(
        self,
        nick: str,
        command: str,
        message: str,
        *,
        log_routing: bool = ...,
        force_channel: str = ...,
    ) -> None: ...

    async def wait_for_responses(
        self,
        expected_nicks: list[str],
        expected_command: str,
        timeout: float = ...,
    ) -> dict[str, dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class MakerAttestParticipant:
    """Per-maker state needed to issue and decode one round of attestations.

    The taker keeps these in lock-step with ``MakerSession``s but we
    pass a narrowed view so this module doesn't depend on the full
    session class — the round can be exercised in tests with simple
    namespaces.
    """

    nick: str
    signer_idx: int
    crypto: _EncryptingCrypto
    comm_channel: str


@dataclass(frozen=True, slots=True)
class AttestDecodeResult:
    """Outcome of decoding one !attest reply.

    Either ``payload`` is populated and ``error`` is ``None``, or vice
    versa. The ``nick`` is preserved so callers can surface per-maker
    rejection reasons without rebuilding maps.
    """

    nick: str
    payload: AttestPayload | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.payload is not None


def decode_attest_reply(
    *,
    nick: str,
    response_data: str,
    crypto: _DecryptingCrypto,
    expected_set_size: int,
) -> AttestDecodeResult:
    """Decrypt and decode a single maker's !attest reply.

    The directory wire format is ``<encrypted_token> <signing_pk> <sig>``;
    only the first whitespace-separated token is the encrypted payload
    (the rest is per-message signing metadata applied by the directory
    client). Mirrors the !ioauth decode path in :mod:`taker.taker`.
    """
    parts = response_data.strip().split()
    if not parts:
        return AttestDecodeResult(nick=nick, payload=None, error="empty response")
    encrypted = parts[0]
    try:
        plaintext = crypto.decrypt(encrypted)
    except Exception as e:  # pragma: no cover - defensive: NaCl internals
        return AttestDecodeResult(nick=nick, payload=None, error=f"decrypt failed: {e}")
    try:
        payload = decode_attest(plaintext, expected_set_size=expected_set_size)
    except AttestWireError as e:
        return AttestDecodeResult(nick=nick, payload=None, error=f"wire decode failed: {e}")
    return AttestDecodeResult(nick=nick, payload=payload, error=None)


def index_ring_by_nick(ring: list[RingMember], nick_to_idx: dict[str, int]) -> dict[str, int]:
    """Return a copy of ``nick_to_idx`` with bounds-checked indices.

    Just a guardrail used at the boundary between the round assembler
    and this module: out-of-range or non-unique indices indicate a
    programming error in the round-builder layer rather than a hostile
    maker, so we surface them eagerly.
    """
    n = len(ring)
    seen: dict[int, str] = {}
    out: dict[str, int] = {}
    for nick, idx in nick_to_idx.items():
        if not 0 <= idx < n:
            raise ValueError(f"signer_idx {idx} for {nick} outside ring of size {n}")
        if idx in seen:
            raise ValueError(
                f"duplicate signer_idx {idx} for {nick} (also assigned to {seen[idx]})"
            )
        seen[idx] = nick
        out[nick] = idx
    return out


def log_decode_failures(results: list[AttestDecodeResult]) -> list[str]:
    """Side-effect logger; returns the list of failed nicks."""
    failed: list[str] = []
    for r in results:
        if r.ok:
            continue
        failed.append(r.nick)
        logger.warning(f"!attest from {r.nick}: {r.error}")
    return failed


def build_attestreq_for_maker(
    *,
    run_id: bytes,
    round_no: int,
    signer_idx: int,
    ring: list[RingMember],
) -> str:
    """Encode the !attestreq plaintext destined for a single maker.

    Same ring is sent to every maker in the round (JMP-0006 variant
    (a) — full ring up front); only ``signer_idx`` varies. The output
    is plaintext: the caller is responsible for encrypting with the
    per-maker session crypto before passing it to ``send_privmsg``.
    """
    payload = AttestReqPayload(run_id=run_id, round_no=round_no, signer_idx=signer_idx, ring=ring)
    return encode_attestreq(payload)


@dataclass(frozen=True, slots=True)
class AttestationRoundResult:
    """Outcome of one attestation round.

    ``decoded`` is keyed by nick and contains only payloads that
    survived decryption + wire decoding. ``failed_makers`` is the
    union of (a) makers we never heard from before the timeout and
    (b) makers whose reply failed to decode; this is the nick set the
    taker hands to its replacement / abort logic.
    """

    decoded: dict[str, AttestPayload]
    failed_makers: list[str]


async def run_attestation_round(
    *,
    directory_client: _DirectoryClient,
    run_id: bytes,
    round_no: int,
    ring: list[RingMember],
    participants: list[MakerAttestParticipant],
    timeout: float,
) -> AttestationRoundResult:
    """Issue !attestreq to every participant and collect !attest replies.

    Failures are *per-maker*: encryption errors, send errors, missing
    responses, decryption errors and wire-format errors all funnel
    into ``failed_makers`` rather than raising, so the caller can run
    the round to completion and only abort once it knows whether the
    survivors meet the minimum-makers threshold.
    """
    # Phase 1: fan out !attestreq. We do this serially because
    # send_privmsg already hides any per-call latency by routing
    # through the directory client's queues; serialising keeps the
    # error-attribution simple and matches the pattern in
    # Taker._phase_auth.
    plaintext = None
    sent_nicks: list[str] = []
    failed: list[str] = []
    for p in participants:
        try:
            plaintext = build_attestreq_for_maker(
                run_id=run_id, round_no=round_no, signer_idx=p.signer_idx, ring=ring
            )
            ciphertext = p.crypto.encrypt(plaintext)
        except Exception as e:
            logger.warning(f"!attestreq encode/encrypt for {p.nick} failed: {e}")
            failed.append(p.nick)
            continue
        try:
            await directory_client.send_privmsg(
                p.nick,
                "attestreq",
                ciphertext,
                log_routing=True,
                force_channel=p.comm_channel,
            )
        except Exception as e:
            logger.warning(f"!attestreq send to {p.nick} failed: {e}")
            failed.append(p.nick)
            continue
        sent_nicks.append(p.nick)

    if not sent_nicks:
        return AttestationRoundResult(decoded={}, failed_makers=failed)

    # Phase 2: wait for !attest replies. Single wait_for_responses
    # covers the whole fan-out; makers that never reply just don't
    # appear in the dict.
    responses = await directory_client.wait_for_responses(
        expected_nicks=sent_nicks,
        expected_command="!attest",
        timeout=timeout,
    )

    # Phase 3: decrypt + decode per maker.
    expected_set_size = len(ring)
    by_nick = {p.nick: p for p in participants}
    decoded: dict[str, AttestPayload] = {}
    decode_results: list[AttestDecodeResult] = []
    for nick in sent_nicks:
        resp = responses.get(nick)
        if resp is None:
            decode_results.append(
                AttestDecodeResult(nick=nick, payload=None, error="no response before timeout")
            )
            continue
        if resp.get("error"):
            decode_results.append(
                AttestDecodeResult(
                    nick=nick, payload=None, error=f"maker error: {resp.get('data', '')}"
                )
            )
            continue
        result = decode_attest_reply(
            nick=nick,
            response_data=resp.get("data", ""),
            crypto=by_nick[nick].crypto,
            expected_set_size=expected_set_size,
        )
        decode_results.append(result)
        if result.ok:
            assert result.payload is not None  # for mypy
            # Cross-check run_id / round_no — a misaddressed reply is
            # a protocol violation, not a decode error, so we surface
            # it the same way as decryption failure.
            if result.payload.run_id != run_id or result.payload.round_no != round_no:
                logger.warning(
                    f"!attest from {nick}: run_id/round_no mismatch "
                    f"(expected ({run_id.hex()[:8]}.., {round_no}), "
                    f"got ({result.payload.run_id.hex()[:8]}.., {result.payload.round_no}))"
                )
                continue
            decoded[nick] = result.payload

    failed.extend(log_decode_failures(decode_results))
    # Drop nicks that decoded successfully but were also in the
    # earlier send-failure list (shouldn't happen by construction,
    # but cheap insurance against future refactors).
    failed = [n for n in failed if n not in decoded]
    return AttestationRoundResult(decoded=decoded, failed_makers=failed)
