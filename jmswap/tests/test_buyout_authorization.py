"""Escrow authorization in the buyout signers (patch 0003, policy 1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import grpc
import pytest
from bitcointx.core.key import XOnlyPubKey
from jmcore.constants import GENESIS_BLOCK_HASHES
from jmcore.escrow_authorizer import authorization_digest
from test_buyout_signing import BUYER_AUTHORIZER, BUYER_KEY, POINT, SCRIPT

from jmswap.buyout_messages import Prevout
from jmswap.buyout_signing import _freeze, _funding_partial, _require_hardened
from jmswap.buyout_terms import ProtocolError
from jmswap.lnd_escrow import LndEscrowClient, LndEscrowRpcError, SigningAttempt

SESSION = b"\x44" * 32
ATTEMPT = SigningAttempt(b"\x22" * 32, BUYER_KEY * 2)
REMOTE_NONCE = BUYER_KEY * 2
PARENT = SimpleNamespace(raw=b"exact parent bytes")
PREVOUTS = [
    Prevout(value=1_000_000, script_pubkey=SCRIPT.hex()),
    Prevout(value=200_000, script_pubkey=SCRIPT.hex()),
]


def _backend(**status: Any) -> Any:
    backend = create_autospec(LndEscrowClient, instance=True)
    backend.status.return_value = SimpleNamespace(
        durable_local_partial=status.get("partial"),
        durable_local_nonce=ATTEMPT.public_nonce,
        durable_remote_nonce=REMOTE_NONCE,
        active_attempt=status.get("active", ATTEMPT),
    )
    backend.sign.return_value = b"\x99" * 32
    backend.policy_versions.return_value = frozenset({1})
    return backend


async def _partial(backend: Any, authorizer: Any = BUYER_AUTHORIZER) -> bytes:
    return await _funding_partial(
        backend, authorizer, POINT, SESSION, ATTEMPT, REMOTE_NONCE, PARENT, PREVOUTS, 0
    )  # type: ignore[arg-type]


async def test_fresh_partial_carries_an_authorization_of_the_local_parent() -> None:
    backend = _backend()
    assert await _partial(backend) == b"\x99" * 32
    authorization = backend.sign.await_args.args[4]
    digest = authorization_digest(
        chain_hash=bytes.fromhex(GENESIS_BLOCK_HASHES["regtest"])[::-1],
        node_pubkey=BUYER_KEY,
        funding_txid=bytes.fromhex(POINT.txid)[::-1],
        output_index=POINT.vout,
        session_id=SESSION,
        parent_tx=PARENT.raw,
        prevouts=[(item.value, bytes.fromhex(item.script_pubkey)) for item in PREVOUTS],
        channel_input_index=0,
    )
    public = XOnlyPubKey(BUYER_AUTHORIZER.authorizer.public_key)
    assert public.verify_schnorr(digest, authorization)


async def test_fresh_partial_without_authorizer_is_refused() -> None:
    backend = _backend()
    with pytest.raises(ProtocolError, match="authorizer unavailable"):
        await _partial(backend, None)
    backend.sign.assert_not_awaited()


async def test_durable_partial_is_recovered_without_authorizer() -> None:
    backend = _backend(partial=b"\x77" * 32, active=None)
    backend.policy_versions.side_effect = LndEscrowRpcError(
        "PolicyInfo", grpc.StatusCode.UNIMPLEMENTED
    )
    assert await _partial(backend, None) == b"\x77" * 32
    backend.sign.assert_not_awaited()
    backend.policy_versions.assert_not_awaited()


async def test_fresh_partial_of_a_pre_upgrade_session_needs_a_hardened_node() -> None:
    """A session frozen before upgrading never reaches an unhardened SignParent."""
    backend = _backend()
    backend.policy_versions.side_effect = LndEscrowRpcError(
        "PolicyInfo", grpc.StatusCode.UNIMPLEMENTED
    )
    with pytest.raises(ProtocolError, match="upgrade"):
        await _partial(backend)
    backend.sign.assert_not_awaited()


def _policy_backend(result: Any) -> Any:
    backend = create_autospec(LndEscrowClient, instance=True)
    if isinstance(result, BaseException):
        backend.policy_versions.side_effect = result
    else:
        backend.policy_versions.return_value = result
    return backend


async def test_hardened_backend_is_accepted() -> None:
    await _require_hardened(_policy_backend(frozenset({1, 2})), BUYER_AUTHORIZER)


@pytest.mark.parametrize(
    ("result", "match"),
    [
        (LndEscrowRpcError("PolicyInfo", grpc.StatusCode.UNIMPLEMENTED), "upgrade"),
        (frozenset(), "required authorization policy"),
        (frozenset({2}), "required authorization policy"),
    ],
)
async def test_unhardened_backend_is_refused_before_freezing(result: Any, match: str) -> None:
    backend = _policy_backend(result)
    with pytest.raises(ProtocolError, match=match):
        await _freeze(backend, SimpleNamespace(), BUYER_AUTHORIZER)  # type: ignore[arg-type]
    backend.freeze.assert_not_awaited()


@pytest.mark.parametrize(
    "code",
    [grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.UNAVAILABLE, None],
)
async def test_other_policy_failures_propagate_unchanged(code: Any) -> None:
    error = LndEscrowRpcError("PolicyInfo", code)
    with pytest.raises(LndEscrowRpcError) as raised:
        await _require_hardened(_policy_backend(error), BUYER_AUTHORIZER)
    assert raised.value is error


async def test_freeze_without_authorizer_never_contacts_the_backend() -> None:
    backend = _policy_backend(frozenset({1}))
    with pytest.raises(ProtocolError, match="authorizer unavailable"):
        await _freeze(backend, SimpleNamespace(), None)  # type: ignore[arg-type]
    backend.policy_versions.assert_not_awaited()
    backend.freeze.assert_not_awaited()
