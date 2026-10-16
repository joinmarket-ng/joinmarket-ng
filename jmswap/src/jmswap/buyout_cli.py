"""Operator command line of the standalone buyout service.

An operator drives a buyout endpoint with four verbs: run it (``serve``),
propose one buyout (``prepare``), abandon one that was never signed (``cancel``)
and look at the journal without touching anything (``status``). This module is
that surface and nothing else. It owns no protocol rule, no economic decision
and no recovery action: every such rule already lives in
:mod:`jmswap.buyout_config` and :mod:`jmswap.buyout_runtime`, and this module
only decides which of them an operator asked for.

Explicit input only
-------------------

``--config`` is required and is the only configuration this tool ever reads:
there is no search path, no environment variable, no default file and no
fallback. An operator who mistypes the path gets an error, never a different
node's configuration.

``serve`` monitors as a buyer only. Answering a peer's proposals, which is what
makes this node sign as a counterparty, requires the explicit
``--counterparty`` flag, so a plain ``serve`` can never be talked into signing
for someone else.

``serve``, ``prepare`` and ``cancel`` act, so they require a configuration that
says ``enabled = true``; a disabled file is reported and nothing is opened,
connected or created. Nothing here adopts, resumes, migrates or force-closes
anything: a session the configured runtime did not create stays untouched, and
:class:`~jmswap.buyout_runtime.BuyoutRuntime` is what refuses it.

``status`` is offline
---------------------

``status`` opens the configured journal read-only and prints one JSON summary
per session. It never connects to a node and it never creates a journal: a
missing file is reported as missing, because an absent journal is not evidence
that this deployment has no sessions, and creating one would turn a typo in
``--config`` into a brand new, empty and entirely believable history.

A summary carries only ``session_id``, ``role``, ``state``,
``parent_signing_started``, ``settlement_authorized`` and
``runtime_binding_present``. Peers, channel points, transactions, scripts,
nonces and credentials stay in the journal: the operator already knows them, and
a terminal, a scrollback or a pasted support log does not need them.

Errors
------

A failure prints one fixed sentence naming what did not happen, plus the
exception class name, and exits non-zero. The underlying message is deliberately
dropped and the chain is suppressed: an RPC failure, an OS error or a rejected
payload can quote a macaroon path, an RPC URL or the private terms of an
attempt, and a traceback would carry them into whatever the operator pastes.
Invalid arguments are rejected by :mod:`argparse` itself, before a file is
opened or a socket is created.

The runtime runs concurrent work in a :class:`asyncio.TaskGroup`, so an expected
failure usually arrives wrapped in an exception group. A group whose leaves are
all expected is sanitized exactly like a single expected failure, naming the
leaf classes. A group that also carries a programming error or a cancellation is
re-raised stripped of its expected leaves: the bug (or the cancellation) must
stay visible with its traceback, and the payloads that travelled with it must
still not be printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import signal
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Annotated, Any, Self

from jmcore import experimental
from jmcore.experimental import experimental_warnings
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from jmswap.buyout_chain import ChainError
from jmswap.buyout_config import (
    BuyoutConfigError,
    BuyoutNetwork,
    BuyoutSettings,
    WalletFingerprint,
    load_buyout_settings,
)
from jmswap.buyout_messages import BuyoutMessageError, CompressedPubKey, Outpoint
from jmswap.buyout_runtime import BuyoutRuntime, BuyoutRuntimeError
from jmswap.buyout_store import BuyoutStore, BuyoutStoreError, StoredSession
from jmswap.buyout_terms import ProtocolError
from jmswap.buyout_transport import PrivateTransportError
from jmswap.lnd_escrow import LndEscrowError
from jmswap.lnd_peer import LndPeerError

PROGRAM = "jm-buyout"

EXIT_OK = 0
EXIT_ERROR = 1

CONFIG_CONTEXT = "the buyout configuration could not be loaded"
SERVE_CONTEXT = "the buyout service could not start or could not keep running"
PREPARE_CONTEXT = "the buyout preparation did not complete"
CANCEL_CONTEXT = "the buyout cancellation did not complete"
FORCE_CLOSE_CONTEXT = "the buyout force-close request did not complete"
STATUS_CONTEXT = "the buyout journal could not be read"
DISABLED_CONTEXT = "the buyout service is disabled in this configuration; nothing was started"
NO_JOURNAL_CONTEXT = "no journal is configured; status requires an existing journal"
MISSING_JOURNAL_CONTEXT = "the configured journal does not exist; status never creates one"

_OUTPOINT_RE = re.compile(r"\A(?P<txid>[0-9a-f]{64}):(?P<vout>0|[1-9][0-9]{0,9})\Z")
_PUBKEY_RE = re.compile(r"\A0[23][0-9a-f]{64}\Z")
_SESSION_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_MAX_VOUT = 2**32 - 1

_EXPECTED_ERRORS: tuple[type[Exception], ...] = (
    BuyoutConfigError,
    BuyoutRuntimeError,
    BuyoutStoreError,
    BuyoutMessageError,
    ProtocolError,
    ChainError,
    PrivateTransportError,
    LndPeerError,
    LndEscrowError,
    ValidationError,
    OSError,
)
"""Failures an operator can cause; anything else is a bug and is not sanitized."""


class CommandError(Exception):
    """One command did not happen, described without quoting anything private."""

    def __init__(self, context: str, cause: str | None = None) -> None:
        super().__init__(context if cause is None else f"{context} ({cause})")


@contextmanager
def _sanitized(context: str) -> Iterator[None]:
    """Reduce an expected failure to ``context`` plus the exception class names.

    An expected failure raised inside the runtime's task group arrives as an
    exception group, so groups are handled too: an entirely expected one is
    sanitized, and one that also carries a bug or a cancellation is re-raised
    with only those leaves, which keeps the bug debuggable without printing the
    message of the expected failure that accompanied it.
    """
    try:
        yield
    except _EXPECTED_ERRORS as exc:
        # `from None` keeps the original message out of any traceback too.
        raise CommandError(context, type(exc).__name__) from None
    except BaseExceptionGroup as group:
        _, unexpected = group.split(_EXPECTED_ERRORS)
        if unexpected is not None:
            raise unexpected from None
        raise CommandError(context, _leaf_names(group)) from None


def _leaf_names(group: BaseExceptionGroup[Any]) -> str:
    """The distinct exception classes of ``group``, and nothing else from it."""
    return ", ".join(sorted({type(exc).__name__ for exc in _leaves(group)}))


def _leaves(group: BaseExceptionGroup[Any]) -> Iterator[BaseException]:
    for exception in group.exceptions:
        if isinstance(exception, BaseExceptionGroup):
            yield from _leaves(exception)
        else:
            yield exception


def _outpoint(value: str) -> Outpoint:
    """Parse ``txid:vout`` before anything is loaded, opened or connected."""
    match = _OUTPOINT_RE.fullmatch(value)
    if match is None or int(match["vout"]) > _MAX_VOUT:
        raise argparse.ArgumentTypeError("a channel must be lowercase txid:vout")
    return Outpoint(txid=match["txid"], vout=int(match["vout"]))


def _pubkey(value: str) -> str:
    if _PUBKEY_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("a peer must be a compressed pubkey in lowercase hex")
    return value


def _session_id(value: str) -> str:
    if _SESSION_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("a session id must be 64 lowercase hex characters")
    return value


def build_parser() -> argparse.ArgumentParser:
    """The whole argument surface; invalid input exits non-zero from here."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Operate a standalone JoinMarket channel-buyout endpoint.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="PATH",
        help="explicit TOML configuration file; no location is ever guessed",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="monitor sessions until SIGINT or SIGTERM")
    serve.add_argument(
        "--counterparty",
        action="store_true",
        help="also answer incoming proposals and sign as a counterparty",
    )

    prepare = commands.add_parser("prepare", help="propose one buyout and print its session id")
    prepare.add_argument("--peer", type=_pubkey, required=True, metavar="PUBKEY")
    prepare.add_argument(
        "--channel",
        type=_outpoint,
        action="append",
        required=True,
        metavar="TXID:VOUT",
        help="channel to buy out; repeat for several channels",
    )

    cancel = commands.add_parser("cancel", help="cancel one never-signed buyer session")
    cancel.add_argument("--session", type=_session_id, required=True, metavar="SID")

    force_close = commands.add_parser(
        "force-close",
        help="explicitly force-close an unpaid signed session (incurs LND chain fees)",
    )
    force_close.add_argument("--session", type=_session_id, required=True, metavar="SID")

    commands.add_parser("status", help="print the configured journal offline, as JSON")
    return parser


class _RuntimeBindingShape(BaseModel):
    """The shape :class:`~jmswap.buyout_runtime.BuyoutRuntime` writes, and only it.

    The field types are the configuration's own, so "this record carries a
    binding" means the same thing here as it does where the binding is written
    and compared. The model is strict and closed: a stray key, a network this
    build does not know, a non-hex identity or a boolean mixdepth is not a
    binding, and an extra or unexpected key means the record was written by
    something else. Whether the binding is *this* node's is deliberately not
    decided here; that needs a runtime, and status is offline.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)

    network: BuyoutNetwork
    lnd_identity: CompressedPubKey
    mixdepth: Annotated[int, Field(ge=0)]
    wallet_fingerprint: WalletFingerprint | None = None

    @model_validator(mode="after")
    def _fingerprint_is_absent_or_valid(self) -> Self:
        if "wallet_fingerprint" in self.model_fields_set and self.wallet_fingerprint is None:
            raise ValueError("wallet_fingerprint must be absent or a fingerprint")
        return self


def _binding_is_present(binding: object) -> bool:
    """Whether ``binding`` is a well-formed runtime binding, not merely a value."""
    if not isinstance(binding, dict):
        return False
    try:
        _RuntimeBindingShape.model_validate(binding)
    except ValidationError:
        return False
    return True


def summarize(record: StoredSession) -> dict[str, object]:
    """The only view of a session this tool prints.

    ``settlement_authorized`` and ``runtime_binding_present`` are affirmative
    facts: an absent or non-boolean marker, and an absent, empty, partial or
    otherwise malformed binding, are reported as false rather than as unknown.
    """
    return {
        "session_id": record.session_id,
        "role": record.role,
        "state": record.state,
        "parent_signing_started": record.parent_signing_started,
        "settlement_authorized": record.data.get("settlement_authorized") is True,
        "runtime_binding_present": _binding_is_present(record.data.get("runtime_binding")),
    }


@contextmanager
def _stop_on_signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> Iterator[None]:
    """Turn SIGINT and SIGTERM into ``stop``, and put the handlers back after.

    Whatever the process had installed is restored on the way out, so a caller
    that embeds this command keeps its own shutdown handling.
    """
    installed: list[tuple[signal.Signals, Any]] = []
    for number in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(number)
        try:
            loop.add_signal_handler(number, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed.append((number, previous))
    try:
        yield
    finally:
        for number, previous in reversed(installed):
            with suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(number)
            if previous is not None:
                with suppress(OSError, RuntimeError, TypeError, ValueError):
                    signal.signal(number, previous)


async def _serve(settings: BuyoutSettings, *, counterparty: bool) -> None:
    """Poll until a signal arrives, then leave the runtime context.

    The handlers are installed before the runtime is entered so that a signal
    during startup is still observed, and no task is created here: shutdown and
    cancellation both unwind through this single ``await``.
    """
    stop = asyncio.Event()
    with _stop_on_signals(asyncio.get_running_loop(), stop):
        async with BuyoutRuntime(settings, counterparty=counterparty) as runtime:
            await runtime.run(stop)


async def _prepare(settings: BuyoutSettings, peer: str, channels: Sequence[Outpoint]) -> str:
    async with BuyoutRuntime(settings) as runtime:
        return await runtime.prepare(peer, channels)


async def _cancel(settings: BuyoutSettings, session_id: str) -> None:
    async with BuyoutRuntime(settings) as runtime:
        await runtime.cancel(session_id)


async def _force_close(settings: BuyoutSettings, session_id: str) -> None:
    async with BuyoutRuntime(settings) as runtime:
        await runtime.force_close(session_id)


def _status(settings: BuyoutSettings) -> list[dict[str, object]]:
    """Read the configured journal without creating it and without connecting."""
    journal = settings.journal
    if journal is None:
        raise CommandError(NO_JOURNAL_CONTEXT)
    if not journal.exists():
        raise CommandError(MISSING_JOURNAL_CONTEXT)
    with _sanitized(STATUS_CONTEXT), BuyoutStore(journal) as store:
        return [summarize(record) for record in store.list()]


def _load(path: Path) -> BuyoutSettings:
    with _sanitized(CONFIG_CONTEXT):
        return load_buyout_settings(path)


def _dispatch(arguments: argparse.Namespace) -> None:
    settings = _load(arguments.config)
    if arguments.command == "status":
        print(json.dumps(_status(settings), indent=2, sort_keys=True))
        return
    if not settings.enabled:
        # Acting requires an affirmative `enabled`; nothing is opened here.
        raise CommandError(DISABLED_CONTEXT)
    for line in experimental_warnings([experimental.CHANNEL_BUYOUT], str(settings.network)):
        print(line, file=sys.stderr)
    if arguments.command == "serve":
        with _sanitized(SERVE_CONTEXT):
            asyncio.run(_serve(settings, counterparty=arguments.counterparty))
        return
    if arguments.command == "prepare":
        with _sanitized(PREPARE_CONTEXT):
            session_id = asyncio.run(_prepare(settings, arguments.peer, tuple(arguments.channel)))
        print(session_id)
        return
    if arguments.command == "force-close":
        with _sanitized(FORCE_CLOSE_CONTEXT):
            asyncio.run(_force_close(settings, arguments.session))
    else:
        with _sanitized(CANCEL_CONTEXT):
            asyncio.run(_cancel(settings, arguments.session))


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. Returns the process exit status.

    Cancellation is never converted into an exit status: it propagates, so a
    caller that cancels this command sees its own cancellation.
    """
    arguments = build_parser().parse_args(argv)
    try:
        _dispatch(arguments)
    except CommandError as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


__all__ = (
    "CANCEL_CONTEXT",
    "CONFIG_CONTEXT",
    "DISABLED_CONTEXT",
    "MISSING_JOURNAL_CONTEXT",
    "NO_JOURNAL_CONTEXT",
    "PREPARE_CONTEXT",
    "PROGRAM",
    "SERVE_CONTEXT",
    "STATUS_CONTEXT",
    "CommandError",
    "build_parser",
    "main",
    "summarize",
)


if __name__ == "__main__":
    sys.exit(main())
