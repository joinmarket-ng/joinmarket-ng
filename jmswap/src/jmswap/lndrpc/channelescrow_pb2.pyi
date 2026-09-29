from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ChannelPoint(_message.Message):
    __slots__ = ("funding_txid", "output_index")
    FUNDING_TXID_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_INDEX_FIELD_NUMBER: _ClassVar[int]
    funding_txid: bytes
    output_index: int
    def __init__(self, funding_txid: _Optional[bytes] = ..., output_index: _Optional[int] = ...) -> None: ...

class PrevOut(_message.Message):
    __slots__ = ("value_sat", "pk_script")
    VALUE_SAT_FIELD_NUMBER: _ClassVar[int]
    PK_SCRIPT_FIELD_NUMBER: _ClassVar[int]
    value_sat: int
    pk_script: bytes
    def __init__(self, value_sat: _Optional[int] = ..., pk_script: _Optional[bytes] = ...) -> None: ...

class FreezeChannelRequest(_message.Message):
    __slots__ = ("channel_point", "session_id")
    CHANNEL_POINT_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    channel_point: ChannelPoint
    session_id: bytes
    def __init__(self, channel_point: _Optional[_Union[ChannelPoint, _Mapping]] = ..., session_id: _Optional[bytes] = ...) -> None: ...

class FreezeChannelResponse(_message.Message):
    __slots__ = ("capacity_sat", "local_claim_sat", "remote_claim_sat", "peer_pubkey", "funding_outpoint", "funding_script", "local_funding_pubkey", "remote_funding_pubkey")
    CAPACITY_SAT_FIELD_NUMBER: _ClassVar[int]
    LOCAL_CLAIM_SAT_FIELD_NUMBER: _ClassVar[int]
    REMOTE_CLAIM_SAT_FIELD_NUMBER: _ClassVar[int]
    PEER_PUBKEY_FIELD_NUMBER: _ClassVar[int]
    FUNDING_OUTPOINT_FIELD_NUMBER: _ClassVar[int]
    FUNDING_SCRIPT_FIELD_NUMBER: _ClassVar[int]
    LOCAL_FUNDING_PUBKEY_FIELD_NUMBER: _ClassVar[int]
    REMOTE_FUNDING_PUBKEY_FIELD_NUMBER: _ClassVar[int]
    capacity_sat: int
    local_claim_sat: int
    remote_claim_sat: int
    peer_pubkey: bytes
    funding_outpoint: ChannelPoint
    funding_script: bytes
    local_funding_pubkey: bytes
    remote_funding_pubkey: bytes
    def __init__(self, capacity_sat: _Optional[int] = ..., local_claim_sat: _Optional[int] = ..., remote_claim_sat: _Optional[int] = ..., peer_pubkey: _Optional[bytes] = ..., funding_outpoint: _Optional[_Union[ChannelPoint, _Mapping]] = ..., funding_script: _Optional[bytes] = ..., local_funding_pubkey: _Optional[bytes] = ..., remote_funding_pubkey: _Optional[bytes] = ...) -> None: ...

class PrepareParentRequest(_message.Message):
    __slots__ = ("channel_point", "session_id", "raw_parent_tx", "prev_outs", "channel_input_index")
    CHANNEL_POINT_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    RAW_PARENT_TX_FIELD_NUMBER: _ClassVar[int]
    PREV_OUTS_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_INPUT_INDEX_FIELD_NUMBER: _ClassVar[int]
    channel_point: ChannelPoint
    session_id: bytes
    raw_parent_tx: bytes
    prev_outs: _containers.RepeatedCompositeFieldContainer[PrevOut]
    channel_input_index: int
    def __init__(self, channel_point: _Optional[_Union[ChannelPoint, _Mapping]] = ..., session_id: _Optional[bytes] = ..., raw_parent_tx: _Optional[bytes] = ..., prev_outs: _Optional[_Iterable[_Union[PrevOut, _Mapping]]] = ..., channel_input_index: _Optional[int] = ...) -> None: ...

class PrepareParentResponse(_message.Message):
    __slots__ = ("txid",)
    TXID_FIELD_NUMBER: _ClassVar[int]
    txid: bytes
    def __init__(self, txid: _Optional[bytes] = ...) -> None: ...

class BeginSigningRequest(_message.Message):
    __slots__ = ("channel_point", "session_id", "restart_attempt")
    CHANNEL_POINT_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    RESTART_ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    channel_point: ChannelPoint
    session_id: bytes
    restart_attempt: bool
    def __init__(self, channel_point: _Optional[_Union[ChannelPoint, _Mapping]] = ..., session_id: _Optional[bytes] = ..., restart_attempt: _Optional[bool] = ...) -> None: ...

class BeginSigningResponse(_message.Message):
    __slots__ = ("attempt_id", "local_public_nonce")
    ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    LOCAL_PUBLIC_NONCE_FIELD_NUMBER: _ClassVar[int]
    attempt_id: bytes
    local_public_nonce: bytes
    def __init__(self, attempt_id: _Optional[bytes] = ..., local_public_nonce: _Optional[bytes] = ...) -> None: ...

class SignParentRequest(_message.Message):
    __slots__ = ("channel_point", "session_id", "attempt_id", "remote_public_nonce")
    CHANNEL_POINT_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    REMOTE_PUBLIC_NONCE_FIELD_NUMBER: _ClassVar[int]
    channel_point: ChannelPoint
    session_id: bytes
    attempt_id: bytes
    remote_public_nonce: bytes
    def __init__(self, channel_point: _Optional[_Union[ChannelPoint, _Mapping]] = ..., session_id: _Optional[bytes] = ..., attempt_id: _Optional[bytes] = ..., remote_public_nonce: _Optional[bytes] = ...) -> None: ...

class SignParentResponse(_message.Message):
    __slots__ = ("local_partial_signature",)
    LOCAL_PARTIAL_SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    local_partial_signature: bytes
    def __init__(self, local_partial_signature: _Optional[bytes] = ...) -> None: ...

class FinalizeParentRequest(_message.Message):
    __slots__ = ("channel_point", "session_id", "attempt_id", "remote_partial_signature")
    CHANNEL_POINT_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    REMOTE_PARTIAL_SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    channel_point: ChannelPoint
    session_id: bytes
    attempt_id: bytes
    remote_partial_signature: bytes
    def __init__(self, channel_point: _Optional[_Union[ChannelPoint, _Mapping]] = ..., session_id: _Optional[bytes] = ..., attempt_id: _Optional[bytes] = ..., remote_partial_signature: _Optional[bytes] = ...) -> None: ...

class FinalizeParentResponse(_message.Message):
    __slots__ = ("raw_tx", "txid")
    RAW_TX_FIELD_NUMBER: _ClassVar[int]
    TXID_FIELD_NUMBER: _ClassVar[int]
    raw_tx: bytes
    txid: bytes
    def __init__(self, raw_tx: _Optional[bytes] = ..., txid: _Optional[bytes] = ...) -> None: ...

class StatusRequest(_message.Message):
    __slots__ = ("channel_point", "session_id")
    CHANNEL_POINT_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    channel_point: ChannelPoint
    session_id: bytes
    def __init__(self, channel_point: _Optional[_Union[ChannelPoint, _Mapping]] = ..., session_id: _Optional[bytes] = ...) -> None: ...

class StatusResponse(_message.Message):
    __slots__ = ("stage", "parent_txid", "finalized", "durable_local_public_nonce", "durable_remote_public_nonce", "durable_local_partial_signature", "finalized_parent_tx", "active_attempt_id", "active_local_public_nonce")
    STAGE_FIELD_NUMBER: _ClassVar[int]
    PARENT_TXID_FIELD_NUMBER: _ClassVar[int]
    FINALIZED_FIELD_NUMBER: _ClassVar[int]
    DURABLE_LOCAL_PUBLIC_NONCE_FIELD_NUMBER: _ClassVar[int]
    DURABLE_REMOTE_PUBLIC_NONCE_FIELD_NUMBER: _ClassVar[int]
    DURABLE_LOCAL_PARTIAL_SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    FINALIZED_PARENT_TX_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_LOCAL_PUBLIC_NONCE_FIELD_NUMBER: _ClassVar[int]
    stage: int
    parent_txid: bytes
    finalized: bool
    durable_local_public_nonce: bytes
    durable_remote_public_nonce: bytes
    durable_local_partial_signature: bytes
    finalized_parent_tx: bytes
    active_attempt_id: bytes
    active_local_public_nonce: bytes
    def __init__(self, stage: _Optional[int] = ..., parent_txid: _Optional[bytes] = ..., finalized: _Optional[bool] = ..., durable_local_public_nonce: _Optional[bytes] = ..., durable_remote_public_nonce: _Optional[bytes] = ..., durable_local_partial_signature: _Optional[bytes] = ..., finalized_parent_tx: _Optional[bytes] = ..., active_attempt_id: _Optional[bytes] = ..., active_local_public_nonce: _Optional[bytes] = ...) -> None: ...

class CancelRequest(_message.Message):
    __slots__ = ("channel_point", "session_id")
    CHANNEL_POINT_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    channel_point: ChannelPoint
    session_id: bytes
    def __init__(self, channel_point: _Optional[_Union[ChannelPoint, _Mapping]] = ..., session_id: _Optional[bytes] = ...) -> None: ...

class CancelResponse(_message.Message):
    __slots__ = ("link_resumed",)
    LINK_RESUMED_FIELD_NUMBER: _ClassVar[int]
    link_resumed: bool
    def __init__(self, link_resumed: _Optional[bool] = ...) -> None: ...
