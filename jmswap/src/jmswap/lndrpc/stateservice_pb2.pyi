from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class WalletState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NON_EXISTING: _ClassVar[WalletState]
    LOCKED: _ClassVar[WalletState]
    UNLOCKED: _ClassVar[WalletState]
    RPC_ACTIVE: _ClassVar[WalletState]
    SERVER_ACTIVE: _ClassVar[WalletState]
    WAITING_TO_START: _ClassVar[WalletState]
NON_EXISTING: WalletState
LOCKED: WalletState
UNLOCKED: WalletState
RPC_ACTIVE: WalletState
SERVER_ACTIVE: WalletState
WAITING_TO_START: WalletState

class SubscribeStateRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SubscribeStateResponse(_message.Message):
    __slots__ = ("state",)
    STATE_FIELD_NUMBER: _ClassVar[int]
    state: WalletState
    def __init__(self, state: _Optional[_Union[WalletState, str]] = ...) -> None: ...

class GetStateRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetStateResponse(_message.Message):
    __slots__ = ("state",)
    STATE_FIELD_NUMBER: _ClassVar[int]
    state: WalletState
    def __init__(self, state: _Optional[_Union[WalletState, str]] = ...) -> None: ...
