"""Endpoint-local signing for channel-input escrow timeout spends."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, TypeVar, runtime_checkable

from jmcore.bitcoin import get_txid, parse_transaction
from jmcore.musig2 import SecNonce, nonce_agg, partial_sig_verify, sign_partial
from jmcore.taproot import taproot_output_script
from jmswap.bitcoin_escrow import (
    ChannelInputEscrowTree,
    UnsignedTimeoutSpend,
    build_timeout_spend,
    finalize_timeout_spend,
    timeout_nonce,
    timeout_session,
    verify_timeout,
)
from jmswap.channel_escrow import (
    ChannelInputEscrowTerms,
    EscrowOutpoint,
    EscrowTimeoutEvidence,
)
from jmswap.errors import ChannelInputEscrowError
from jmswap.lnd import LndEscrowSigningAttempt, LndFinalizedEscrowParent
from jmswap.peer_transport import (
    EscrowProtocolRole,
    EscrowPublicKeysPayload,
    ParentMuSig2NoncePayload,
    ParentMuSig2PartialSignaturePayload,
    ParentPreparationPayload,
    TimeoutMuSig2NoncePayload,
    TimeoutMuSig2PartialSignaturePayload,
)
from jmswap.spend import SwapOutpoint

from maker.escrow_keys import (
    EscrowEndpointRole,
    EscrowKeyHandle,
    EscrowKeyPurpose,
    WalletEscrowKeyDeriver,
    derive_timeout_nonce_randomness,
)

_PROTOCOL_ROLES = {
    EscrowEndpointRole.MAKER: EscrowProtocolRole.MAKER,
    EscrowEndpointRole.PEER: EscrowProtocolRole.PEER,
}
_PARENT_SIGNING_ROUND_DOMAIN = b"joinmarket-ng/channel-input-escrow/v1/parent-signing-round\x00"
T = TypeVar("T")


@runtime_checkable
class ChannelInputParentSigningBackend(Protocol):
    """The LND operations needed for one prepared channel-input parent."""

    async def prepare_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: list[tuple[int, bytes]],
        channel_input_index: int,
    ) -> str: ...

    async def begin_channel_input_signing(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        *,
        restart_attempt: bool = False,
    ) -> LndEscrowSigningAttempt: ...

    async def sign_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_public_nonce: bytes,
    ) -> bytes: ...

    async def finalize_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent: ...

    async def recover_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent: ...


def _protocol_role(role: EscrowEndpointRole) -> EscrowProtocolRole:
    if not isinstance(role, EscrowEndpointRole):
        raise ChannelInputEscrowError("invalid escrow endpoint role")
    return _PROTOCOL_ROLES[role]


def _normalize_edge_id(edge_id: str) -> str:
    if not isinstance(edge_id, str) or len(edge_id) != 64:
        raise ChannelInputEscrowError("edge ID must be a 32-byte hexadecimal string")
    try:
        value = bytes.fromhex(edge_id)
    except ValueError as exc:
        raise ChannelInputEscrowError("edge ID must be a 32-byte hexadecimal string") from exc
    if len(value) != 32:
        raise ChannelInputEscrowError("edge ID must be a 32-byte hexadecimal string")
    return value.hex()


def _timeout_txid(spend: UnsignedTimeoutSpend) -> str:
    return spend.tx.txid()


def _normalize_error(message: str, operation: Callable[[], T]) -> T:
    try:
        return operation()
    except ChannelInputEscrowError:
        raise
    except Exception as exc:  # noqa: BLE001 - endpoint boundaries normalize primitive failures
        raise ChannelInputEscrowError(message) from exc


async def _normalize_async_error(message: str, operation: Callable[[], Awaitable[T]]) -> T:
    try:
        return await operation()
    except ChannelInputEscrowError:
        raise
    except Exception as exc:  # noqa: BLE001 - endpoint boundaries normalize backend failures
        raise ChannelInputEscrowError(message) from exc


@dataclass(frozen=True, slots=True)
class EndpointEscrowKeys:
    """One endpoint's in-memory escrow handles and public exchange payload."""

    edge_id: str
    role: EscrowEndpointRole
    escrow: EscrowKeyHandle
    timeout_destination: EscrowKeyHandle
    nonce_seed: EscrowKeyHandle
    claim: EscrowKeyHandle | None = None

    def __post_init__(self) -> None:
        edge_id = _normalize_edge_id(self.edge_id)
        object.__setattr__(self, "edge_id", edge_id)
        if not isinstance(self.role, EscrowEndpointRole):
            raise ChannelInputEscrowError("invalid escrow endpoint role")
        expected = (
            (self.escrow, EscrowKeyPurpose.ESCROW),
            (self.timeout_destination, EscrowKeyPurpose.TIMEOUT_DESTINATION),
            (self.nonce_seed, EscrowKeyPurpose.NONCE_SEED),
        )
        for handle, purpose in expected:
            self._validate_handle(handle, purpose)
        if self.role is EscrowEndpointRole.MAKER:
            if self.claim is None:
                raise ChannelInputEscrowError("maker escrow keys require a claim key")
            self._validate_handle(self.claim, EscrowKeyPurpose.CLAIM)
        elif self.claim is not None:
            raise ChannelInputEscrowError("peer escrow keys cannot include a claim key")

    def _validate_handle(self, handle: EscrowKeyHandle, purpose: EscrowKeyPurpose) -> None:
        if not isinstance(handle, EscrowKeyHandle):
            raise ChannelInputEscrowError("invalid escrow key handle")
        metadata = handle.metadata
        if (
            metadata.session_id != self.edge_id
            or metadata.role is not self.role
            or metadata.purpose is not purpose
        ):
            raise ChannelInputEscrowError("escrow key metadata does not match endpoint")

    def public_payload(self) -> EscrowPublicKeysPayload:
        """Return the endpoint's wire-safe public keys and timeout destination."""
        return _normalize_error(
            "unable to construct escrow public-key payload",
            lambda: EscrowPublicKeysPayload(
                role=_protocol_role(self.role),
                escrow_pubkey=self.escrow.public_key_bytes().hex(),
                timeout_destination_scriptpubkey=taproot_output_script(
                    self.timeout_destination.public_key_bytes()[1:]
                ).hex(),
                maker_claim_pubkey=(
                    self.claim.public_key_bytes().hex() if self.claim is not None else None
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class EndpointTimeoutSigningContext:
    """In-memory deterministic timeout signing state for one endpoint."""

    round_id: str
    tree: ChannelInputEscrowTree
    unsigned_spend: UnsignedTimeoutSpend
    local_keys: EndpointEscrowKeys
    maker_keys: EscrowPublicKeysPayload
    peer_keys: EscrowPublicKeysPayload
    peer_claim_sats: int
    timeout_compensation_sats: int
    timeout_fee_sats: int
    timeout_height: int
    local_secnonce: SecNonce = field(repr=False)
    public_nonce: bytes


@dataclass(frozen=True, slots=True)
class EndpointParentSigningContext:
    """Endpoint-local LND state for signing one exact prepared parent."""

    edge_id: str
    role: EscrowEndpointRole
    funding_outpoint: EscrowOutpoint
    parent: ParentPreparationPayload
    _attempt: LndEscrowSigningAttempt = field(repr=False)
    round_id: str
    public_nonce: bytes

    def __post_init__(self) -> None:
        edge_id = _normalize_edge_id(self.edge_id)
        object.__setattr__(self, "edge_id", edge_id)
        if not isinstance(self.role, EscrowEndpointRole):
            raise ChannelInputEscrowError("invalid escrow endpoint role")
        if not isinstance(self.funding_outpoint, EscrowOutpoint):
            raise ChannelInputEscrowError("invalid channel funding outpoint")
        if not isinstance(self.parent, ParentPreparationPayload):
            raise ChannelInputEscrowError("invalid parent preparation")
        if not isinstance(self._attempt, LndEscrowSigningAttempt):
            raise ChannelInputEscrowError("invalid local parent signing attempt")
        if len(self._attempt.attempt_id) != 32 or len(self._attempt.public_nonce) != 66:
            raise ChannelInputEscrowError("invalid local parent signing attempt")
        if not isinstance(self.public_nonce, bytes) or len(self.public_nonce) != 66:
            raise ChannelInputEscrowError("invalid local parent public nonce")
        if self.public_nonce != self._attempt.public_nonce:
            raise ChannelInputEscrowError(
                "local parent public nonce does not match signing attempt"
            )
        round_id = _normalize_edge_id(self.round_id)
        object.__setattr__(self, "round_id", round_id)


class EndpointParentSigner:
    """Coordinate endpoint-local LND signing for a prepared escrow parent."""

    def __init__(self, backend: ChannelInputParentSigningBackend) -> None:
        if not isinstance(backend, ChannelInputParentSigningBackend):
            raise ChannelInputEscrowError("invalid channel-input signing backend")
        self._backend = backend

    async def prepare_parent(
        self,
        edge_id: str,
        funding_outpoint: EscrowOutpoint,
        parent: ParentPreparationPayload,
    ) -> str:
        """Bind LND to the exact parent bytes and ordered prevouts."""

        async def prepare() -> str:
            normalized_edge_id = _normalize_edge_id(edge_id)
            self._validate_parent_and_funding(funding_outpoint, parent)
            prepared_txid = await self._backend.prepare_channel_input_parent(
                normalized_edge_id,
                funding_outpoint,
                parent.parent_tx_hex,
                [
                    (prevout.value_sats, bytes.fromhex(prevout.scriptpubkey))
                    for prevout in parent.ordered_prevouts
                ],
                parent.channel_input_index,
            )
            if not isinstance(prepared_txid, str) or prepared_txid != parent.parent_txid:
                raise ChannelInputEscrowError("LND prepared an unexpected parent")
            return prepared_txid

        return await _normalize_async_error("unable to prepare channel-input parent", prepare)

    async def begin_signing(
        self,
        *,
        edge_id: str,
        role: EscrowEndpointRole,
        funding_outpoint: EscrowOutpoint,
        parent: ParentPreparationPayload,
        timeout_evidence: EscrowTimeoutEvidence,
        peer_round_id: str | None = None,
        restart_attempt: bool = False,
    ) -> EndpointParentSigningContext:
        """Start a local LND MuSig2 attempt after timeout recovery is secured."""

        async def begin() -> EndpointParentSigningContext:
            normalized_edge_id = _normalize_edge_id(edge_id)
            self._validate_parent_and_funding(funding_outpoint, parent)
            self._validate_timeout_evidence(parent, timeout_evidence)
            if not isinstance(role, EscrowEndpointRole):
                raise ChannelInputEscrowError("invalid escrow endpoint role")
            if not isinstance(restart_attempt, bool):
                raise ChannelInputEscrowError("invalid parent signing restart flag")
            if role is EscrowEndpointRole.MAKER:
                if peer_round_id is not None:
                    raise ChannelInputEscrowError("maker must not supply a parent signing round")
                round_id: str | None = None
            else:
                if peer_round_id is None:
                    raise ChannelInputEscrowError("peer must supply a parent signing round")
                round_id = _normalize_edge_id(peer_round_id)

            attempt = await self._backend.begin_channel_input_signing(
                normalized_edge_id, funding_outpoint, restart_attempt=restart_attempt
            )
            self._validate_attempt(attempt)
            round_id = (
                self.maker_round_id(normalized_edge_id, parent.parent_txid, attempt.public_nonce)
                if role is EscrowEndpointRole.MAKER
                else round_id
            )
            if round_id is None:
                raise ChannelInputEscrowError("parent signing round is unavailable")
            return EndpointParentSigningContext(
                edge_id=normalized_edge_id,
                role=role,
                funding_outpoint=funding_outpoint,
                parent=parent,
                _attempt=attempt,
                round_id=round_id,
                public_nonce=attempt.public_nonce,
            )

        return await _normalize_async_error("unable to begin parent signing", begin)

    def nonce_payload(self, context: EndpointParentSigningContext) -> ParentMuSig2NoncePayload:
        """Return the wire-safe nonce bound to the local parent signing round."""
        return _normalize_error(
            "unable to construct parent nonce payload",
            lambda: ParentMuSig2NoncePayload(
                round_id=self._validate_context(context).round_id,
                parent_txid=context.parent.parent_txid,
                public_nonce=context.public_nonce.hex(),
            ),
        )

    async def sign_parent(
        self,
        context: EndpointParentSigningContext,
        remote_nonce_payload: ParentMuSig2NoncePayload,
    ) -> ParentMuSig2PartialSignaturePayload:
        """Ask LND for the local parent partial after transcript validation."""

        async def sign() -> ParentMuSig2PartialSignaturePayload:
            validated_context = self._validate_context(context)
            remote_nonce = self._validate_nonce_payload(validated_context, remote_nonce_payload)
            partial = await self._backend.sign_channel_input_parent(
                validated_context.edge_id,
                validated_context.funding_outpoint,
                validated_context._attempt,
                remote_nonce,
            )
            if not isinstance(partial, bytes) or len(partial) != 32:
                raise ChannelInputEscrowError("LND returned an invalid parent partial signature")
            return ParentMuSig2PartialSignaturePayload(
                round_id=validated_context.round_id,
                parent_txid=validated_context.parent.parent_txid,
                partial_signature=partial.hex(),
            )

        return await _normalize_async_error("unable to sign parent transaction", sign)

    async def finalize_parent(
        self,
        context: EndpointParentSigningContext,
        remote_partial_payload: ParentMuSig2PartialSignaturePayload,
    ) -> LndFinalizedEscrowParent:
        """Finalize and verify the LND witness artifact without broadcasting it."""

        async def finalize() -> LndFinalizedEscrowParent:
            validated_context = self._validate_context(context)
            remote_partial = self._validate_partial_payload(
                validated_context, remote_partial_payload
            )
            artifact = await self._backend.finalize_channel_input_parent(
                validated_context.edge_id,
                validated_context.funding_outpoint,
                validated_context._attempt,
                remote_partial,
            )
            self._validate_finalized_artifact(validated_context.parent, artifact)
            return artifact

        return await _normalize_async_error("unable to finalize parent transaction", finalize)

    async def recover_parent(
        self,
        *,
        edge_id: str,
        funding_outpoint: EscrowOutpoint,
        parent: ParentPreparationPayload,
        timeout_evidence: EscrowTimeoutEvidence,
        remote_partial_payload: ParentMuSig2PartialSignaturePayload,
        round_id: str,
    ) -> LndFinalizedEscrowParent:
        """Finalize from LND's durable partial after its in-memory attempt was lost."""

        async def recover() -> LndFinalizedEscrowParent:
            normalized_edge_id = _normalize_edge_id(edge_id)
            normalized_round_id = _normalize_edge_id(round_id)
            self._validate_parent_and_funding(funding_outpoint, parent)
            self._validate_timeout_evidence(parent, timeout_evidence)
            if (
                remote_partial_payload.round_id != normalized_round_id
                or remote_partial_payload.parent_txid != parent.parent_txid
            ):
                raise ChannelInputEscrowError(
                    "parent partial signature does not match recovery transcript"
                )
            remote_partial = bytes.fromhex(remote_partial_payload.partial_signature)
            artifact = await self._backend.recover_channel_input_parent(
                normalized_edge_id, funding_outpoint, remote_partial
            )
            self._validate_finalized_artifact(parent, artifact)
            return artifact

        return await _normalize_async_error("unable to recover parent transaction", recover)

    @staticmethod
    def _validate_parent_and_funding(
        funding_outpoint: EscrowOutpoint, parent: ParentPreparationPayload
    ) -> None:
        if not isinstance(funding_outpoint, EscrowOutpoint):
            raise ChannelInputEscrowError("invalid channel funding outpoint")
        if not isinstance(parent, ParentPreparationPayload):
            raise ChannelInputEscrowError("invalid parent preparation")
        validated = ParentPreparationPayload.model_validate(parent.model_dump())
        channel_input = parse_transaction(validated.parent_tx_hex).inputs[
            validated.channel_input_index
        ]
        if (
            channel_input.txid != funding_outpoint.txid
            or channel_input.vout != funding_outpoint.vout
        ):
            raise ChannelInputEscrowError(
                "parent channel input does not match the expected funding outpoint"
            )

    @staticmethod
    def _validate_timeout_evidence(
        parent: ParentPreparationPayload, timeout_evidence: EscrowTimeoutEvidence
    ) -> None:
        if not isinstance(timeout_evidence, EscrowTimeoutEvidence):
            raise ChannelInputEscrowError("invalid timeout evidence")
        expected_outpoint = EscrowOutpoint(
            txid=parent.parent_txid,
            vout=parent.escrow_output_index,
        )
        if (
            timeout_evidence.fully_signed is not True
            or timeout_evidence.signature_verified is not True
            or timeout_evidence.spent_escrow_outpoint != expected_outpoint
        ):
            raise ChannelInputEscrowError(
                "timeout recovery does not secure the exact parent escrow"
            )

    @staticmethod
    def _validate_attempt(attempt: LndEscrowSigningAttempt) -> None:
        if (
            not isinstance(attempt, LndEscrowSigningAttempt)
            or len(attempt.attempt_id) != 32
            or len(attempt.public_nonce) != 66
        ):
            raise ChannelInputEscrowError("LND returned an invalid parent signing attempt")

    @staticmethod
    def maker_round_id(edge_id: str, parent_txid: str, public_nonce: bytes) -> str:
        """Derive the maker-selected round identifier for an LND public nonce."""
        return hashlib.sha256(
            _PARENT_SIGNING_ROUND_DOMAIN
            + bytes.fromhex(edge_id)
            + bytes.fromhex(parent_txid)
            + public_nonce
        ).hexdigest()

    @staticmethod
    def _validate_context(context: EndpointParentSigningContext) -> EndpointParentSigningContext:
        if not isinstance(context, EndpointParentSigningContext):
            raise ChannelInputEscrowError("invalid parent signing context")
        EndpointParentSigner._validate_parent_and_funding(context.funding_outpoint, context.parent)
        EndpointParentSigner._validate_attempt(context._attempt)
        if context.public_nonce != context._attempt.public_nonce:
            raise ChannelInputEscrowError(
                "local parent public nonce does not match signing attempt"
            )
        return context

    @staticmethod
    def _validate_nonce_payload(
        context: EndpointParentSigningContext, payload: ParentMuSig2NoncePayload
    ) -> bytes:
        if not isinstance(payload, ParentMuSig2NoncePayload):
            raise ChannelInputEscrowError("invalid parent nonce payload")
        if (
            payload.round_id != context.round_id
            or payload.parent_txid != context.parent.parent_txid
        ):
            raise ChannelInputEscrowError("parent nonce payload does not match signing transcript")
        remote_nonce = bytes.fromhex(payload.public_nonce)
        if len(remote_nonce) != 66 or remote_nonce == context.public_nonce:
            raise ChannelInputEscrowError("parent nonce payload is invalid")
        return remote_nonce

    @staticmethod
    def _validate_partial_payload(
        context: EndpointParentSigningContext, payload: ParentMuSig2PartialSignaturePayload
    ) -> bytes:
        if not isinstance(payload, ParentMuSig2PartialSignaturePayload):
            raise ChannelInputEscrowError("invalid parent partial signature payload")
        if (
            payload.round_id != context.round_id
            or payload.parent_txid != context.parent.parent_txid
        ):
            raise ChannelInputEscrowError(
                "parent partial signature does not match signing transcript"
            )
        partial = bytes.fromhex(payload.partial_signature)
        if len(partial) != 32:
            raise ChannelInputEscrowError("parent partial signature is invalid")
        return partial

    @staticmethod
    def _validate_finalized_artifact(
        parent: ParentPreparationPayload, artifact: LndFinalizedEscrowParent
    ) -> None:
        try:
            parsed = parse_transaction(artifact.tx_hex)
            witness = tuple(parsed.witnesses[parent.channel_input_index])
            other_witnesses = [
                stack
                for index, stack in enumerate(parsed.witnesses)
                if index != parent.channel_input_index
            ]
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            raise ChannelInputEscrowError("LND returned an invalid finalized parent") from exc
        if (
            not isinstance(artifact, LndFinalizedEscrowParent)
            or artifact.txid != parent.parent_txid
            or artifact.channel_input_index != parent.channel_input_index
            or len(artifact.channel_input_witness) != 1
            or len(artifact.channel_input_witness[0]) != 64
            or witness != artifact.channel_input_witness
            or any(other_witnesses)
            or get_txid(artifact.tx_hex) != parent.parent_txid
        ):
            raise ChannelInputEscrowError("LND returned an invalid finalized parent")


class EndpointEscrowSigner:
    """Derive endpoint keys and produce role-bound timeout MuSig2 artifacts."""

    def __init__(self, deriver: WalletEscrowKeyDeriver) -> None:
        if not isinstance(deriver, WalletEscrowKeyDeriver):
            raise ChannelInputEscrowError("invalid escrow key deriver")
        self._deriver = deriver

    def derive_keys(self, edge_id: str, role: EscrowEndpointRole) -> EndpointEscrowKeys:
        """Derive all endpoint-local keys for one exact normalized ring edge."""
        normalized_edge_id = _normalize_edge_id(edge_id)
        if not isinstance(role, EscrowEndpointRole):
            raise ChannelInputEscrowError("invalid escrow endpoint role")

        def derive() -> EndpointEscrowKeys:
            claim = (
                self._deriver.derive(normalized_edge_id, role, EscrowKeyPurpose.CLAIM)
                if role is EscrowEndpointRole.MAKER
                else None
            )
            return EndpointEscrowKeys(
                edge_id=normalized_edge_id,
                role=role,
                escrow=self._deriver.derive(normalized_edge_id, role, EscrowKeyPurpose.ESCROW),
                timeout_destination=self._deriver.derive(
                    normalized_edge_id, role, EscrowKeyPurpose.TIMEOUT_DESTINATION
                ),
                nonce_seed=self._deriver.derive(
                    normalized_edge_id, role, EscrowKeyPurpose.NONCE_SEED
                ),
                claim=claim,
            )

        return _normalize_error("unable to derive endpoint escrow keys", derive)

    def build_timeout_context(
        self,
        *,
        local_keys: EndpointEscrowKeys,
        maker_keys: EscrowPublicKeysPayload,
        peer_keys: EscrowPublicKeysPayload,
        terms: ChannelInputEscrowTerms,
        parent: ParentPreparationPayload,
    ) -> EndpointTimeoutSigningContext:
        """Validate a parent commitment and prepare deterministic timeout signing."""

        def build() -> EndpointTimeoutSigningContext:
            if not isinstance(local_keys, EndpointEscrowKeys):
                raise ChannelInputEscrowError("invalid local escrow keys")
            if not isinstance(terms, ChannelInputEscrowTerms):
                raise ChannelInputEscrowError("invalid escrow terms")
            if not isinstance(parent, ParentPreparationPayload):
                raise ChannelInputEscrowError("invalid parent preparation")
            self._validate_public_keys(local_keys, maker_keys, peer_keys)
            if terms.snapshot.session_id != local_keys.edge_id:
                raise ChannelInputEscrowError("terms session does not match escrow edge")
            tree = ChannelInputEscrowTree(
                maker_escrow_pubkey=bytes.fromhex(maker_keys.escrow_pubkey),
                peer_escrow_pubkey=bytes.fromhex(peer_keys.escrow_pubkey),
                maker_claim_pubkey=bytes.fromhex(maker_keys.maker_claim_pubkey or ""),
                payment_hash=bytes.fromhex(terms.payment_hash),
            )
            outpoint = self._validate_parent(parent, terms, tree)
            spend = build_timeout_spend(
                tree,
                outpoint,
                bytes.fromhex(maker_keys.timeout_destination_scriptpubkey),
                bytes.fromhex(peer_keys.timeout_destination_scriptpubkey),
                terms.snapshot.peer_claim_sats,
                terms.timeout_compensation_sats,
                terms.timeout_fee_sats,
                terms.timeout_height,
            )
            randomness = derive_timeout_nonce_randomness(
                local_keys.nonce_seed,
                edge_id=local_keys.edge_id,
                timeout_txid=_timeout_txid(spend),
                timeout_sighash=spend.sighash,
                role=local_keys.role,
            )
            secnonce, public_nonce = timeout_nonce(local_keys.escrow.public_key_bytes(), randomness)
            return EndpointTimeoutSigningContext(
                round_id=parent.round_id,
                tree=tree,
                unsigned_spend=spend,
                local_keys=local_keys,
                maker_keys=maker_keys,
                peer_keys=peer_keys,
                peer_claim_sats=terms.snapshot.peer_claim_sats,
                timeout_compensation_sats=terms.timeout_compensation_sats,
                timeout_fee_sats=terms.timeout_fee_sats,
                timeout_height=terms.timeout_height,
                local_secnonce=secnonce,
                public_nonce=public_nonce,
            )

        return _normalize_error("unable to build timeout signing context", build)

    def nonce_payload(self, context: EndpointTimeoutSigningContext) -> TimeoutMuSig2NoncePayload:
        """Return the local public nonce bound to the exact timeout transcript."""
        return _normalize_error(
            "unable to construct timeout nonce payload",
            lambda: TimeoutMuSig2NoncePayload(
                round_id=context.round_id,
                timeout_txid=_timeout_txid(context.unsigned_spend),
                timeout_sighash=context.unsigned_spend.sighash.hex(),
                public_nonce=context.public_nonce.hex(),
            ),
        )

    def sign_timeout(
        self,
        context: EndpointTimeoutSigningContext,
        remote_nonce_payload: TimeoutMuSig2NoncePayload,
    ) -> TimeoutMuSig2PartialSignaturePayload:
        """Sign the timeout transcript after receiving the peer's public nonce."""

        def sign() -> TimeoutMuSig2PartialSignaturePayload:
            remote_nonce = self._validated_nonce(context, remote_nonce_payload)
            local_nonce, peer_nonce = self._role_ordered_nonces(context, remote_nonce)
            session = timeout_session(
                context.tree, nonce_agg([local_nonce, peer_nonce]), context.unsigned_spend.sighash
            )
            partial = sign_partial(
                context.local_secnonce, context.local_keys.escrow.private_key_bytes(), session
            )
            if not partial_sig_verify(
                partial,
                context.public_nonce,
                context.local_keys.escrow.public_key_bytes(),
                session,
            ):
                raise ChannelInputEscrowError("local timeout partial signature is invalid")
            return TimeoutMuSig2PartialSignaturePayload(
                round_id=context.round_id,
                timeout_txid=_timeout_txid(context.unsigned_spend),
                timeout_sighash=context.unsigned_spend.sighash.hex(),
                partial_signature=partial.hex(),
            )

        return _normalize_error("unable to sign timeout transaction", sign)

    def finalize_timeout(
        self,
        context: EndpointTimeoutSigningContext,
        remote_nonce_payload: TimeoutMuSig2NoncePayload,
        local_partial_payload: TimeoutMuSig2PartialSignaturePayload,
        remote_partial_payload: TimeoutMuSig2PartialSignaturePayload,
    ) -> EscrowTimeoutEvidence:
        """Verify both partials and return fully verified deterministic timeout evidence."""

        def finalize() -> EscrowTimeoutEvidence:
            remote_nonce = self._validated_nonce(context, remote_nonce_payload)
            self._validated_partial(context, local_partial_payload)
            self._validated_partial(context, remote_partial_payload)
            maker_nonce, peer_nonce = self._role_ordered_nonces(context, remote_nonce)
            maker_partial, peer_partial = self._role_ordered_partials(
                context,
                bytes.fromhex(local_partial_payload.partial_signature),
                bytes.fromhex(remote_partial_payload.partial_signature),
            )
            tx = finalize_timeout_spend(
                context.tree,
                context.unsigned_spend,
                maker_nonce,
                peer_nonce,
                maker_partial,
                peer_partial,
            )
            verified = verify_timeout(
                context.tree,
                context.unsigned_spend.outpoint,
                bytes.fromhex(context.maker_keys.timeout_destination_scriptpubkey),
                bytes.fromhex(context.peer_keys.timeout_destination_scriptpubkey),
                context.peer_claim_sats,
                context.timeout_compensation_sats,
                context.timeout_fee_sats,
                context.timeout_height,
                tx,
            )
            return EscrowTimeoutEvidence(
                timeout_tx_hex=tx.serialize().hex(),
                timeout_txid=tx.txid(),
                spent_escrow_outpoint=EscrowOutpoint(
                    txid=context.unsigned_spend.outpoint.txid,
                    vout=context.unsigned_spend.outpoint.vout,
                ),
                locktime=tx.locktime,
                maker_output_sats=tx.outputs[0].value,
                peer_output_sats=tx.outputs[1].value,
                fee_sats=context.unsigned_spend.outpoint.value
                - tx.outputs[0].value
                - tx.outputs[1].value,
                fully_signed=True,
                signature_verified=verified,
            )

        return _normalize_error("unable to finalize timeout transaction", finalize)

    @staticmethod
    def _validate_public_keys(
        local_keys: EndpointEscrowKeys,
        maker_keys: EscrowPublicKeysPayload,
        peer_keys: EscrowPublicKeysPayload,
    ) -> None:
        if (
            not isinstance(maker_keys, EscrowPublicKeysPayload)
            or not isinstance(peer_keys, EscrowPublicKeysPayload)
            or maker_keys.role is not EscrowProtocolRole.MAKER
            or peer_keys.role is not EscrowProtocolRole.PEER
        ):
            raise ChannelInputEscrowError("escrow public-key roles are invalid")
        expected_local = local_keys.public_payload()
        supplied_local = maker_keys if local_keys.role is EscrowEndpointRole.MAKER else peer_keys
        if supplied_local != expected_local:
            raise ChannelInputEscrowError("local escrow public keys do not match derived keys")

    @staticmethod
    def _validate_parent(
        parent: ParentPreparationPayload,
        terms: ChannelInputEscrowTerms,
        tree: ChannelInputEscrowTree,
    ) -> SwapOutpoint:
        parsed = parse_transaction(parent.parent_tx_hex)
        channel_input = parsed.inputs[parent.channel_input_index]
        snapshot_outpoint = terms.snapshot.funding_outpoint
        prevout = parent.ordered_prevouts[parent.channel_input_index]
        if (
            channel_input.txid != snapshot_outpoint.txid
            or channel_input.vout != snapshot_outpoint.vout
            or prevout.value_sats != terms.snapshot.capacity_sats
            or parent.escrow_value_sats != terms.escrow_value_sats
            or parent.escrow_scriptpubkey != tree.funding_output_script().hex()
        ):
            raise ChannelInputEscrowError("parent preparation does not match escrow terms")
        return SwapOutpoint(
            txid=parent.parent_txid,
            vout=parent.escrow_output_index,
            value=parent.escrow_value_sats,
            scriptpubkey=bytes.fromhex(parent.escrow_scriptpubkey),
        )

    @staticmethod
    def _validated_nonce(
        context: EndpointTimeoutSigningContext,
        payload: TimeoutMuSig2NoncePayload,
    ) -> bytes:
        if not isinstance(context, EndpointTimeoutSigningContext):
            raise ChannelInputEscrowError("invalid timeout signing context")
        if not isinstance(payload, TimeoutMuSig2NoncePayload):
            raise ChannelInputEscrowError("invalid timeout nonce payload")
        if (
            payload.round_id != context.round_id
            or payload.timeout_txid != _timeout_txid(context.unsigned_spend)
            or payload.timeout_sighash != context.unsigned_spend.sighash.hex()
        ):
            raise ChannelInputEscrowError("timeout nonce payload does not match signing transcript")
        return bytes.fromhex(payload.public_nonce)

    @staticmethod
    def _validated_partial(
        context: EndpointTimeoutSigningContext,
        payload: TimeoutMuSig2PartialSignaturePayload,
    ) -> None:
        if not isinstance(payload, TimeoutMuSig2PartialSignaturePayload):
            raise ChannelInputEscrowError("invalid timeout partial signature payload")
        if (
            payload.round_id != context.round_id
            or payload.timeout_txid != _timeout_txid(context.unsigned_spend)
            or payload.timeout_sighash != context.unsigned_spend.sighash.hex()
        ):
            raise ChannelInputEscrowError(
                "timeout partial signature does not match signing transcript"
            )

    @staticmethod
    def _role_ordered_nonces(
        context: EndpointTimeoutSigningContext, remote_nonce: bytes
    ) -> tuple[bytes, bytes]:
        if context.local_keys.role is EscrowEndpointRole.MAKER:
            return context.public_nonce, remote_nonce
        return remote_nonce, context.public_nonce

    @staticmethod
    def _role_ordered_partials(
        context: EndpointTimeoutSigningContext, local_partial: bytes, remote_partial: bytes
    ) -> tuple[bytes, bytes]:
        if context.local_keys.role is EscrowEndpointRole.MAKER:
            return local_partial, remote_partial
        return remote_partial, local_partial
