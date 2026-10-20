from . import lightning_pb2 as _lightning_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GenSeedRequest(_message.Message):
    __slots__ = ("aezeed_passphrase", "seed_entropy")
    AEZEED_PASSPHRASE_FIELD_NUMBER: _ClassVar[int]
    SEED_ENTROPY_FIELD_NUMBER: _ClassVar[int]
    aezeed_passphrase: bytes
    seed_entropy: bytes
    def __init__(self, aezeed_passphrase: _Optional[bytes] = ..., seed_entropy: _Optional[bytes] = ...) -> None: ...

class GenSeedResponse(_message.Message):
    __slots__ = ("cipher_seed_mnemonic", "enciphered_seed")
    CIPHER_SEED_MNEMONIC_FIELD_NUMBER: _ClassVar[int]
    ENCIPHERED_SEED_FIELD_NUMBER: _ClassVar[int]
    cipher_seed_mnemonic: _containers.RepeatedScalarFieldContainer[str]
    enciphered_seed: bytes
    def __init__(self, cipher_seed_mnemonic: _Optional[_Iterable[str]] = ..., enciphered_seed: _Optional[bytes] = ...) -> None: ...

class InitWalletRequest(_message.Message):
    __slots__ = ("wallet_password", "cipher_seed_mnemonic", "aezeed_passphrase", "recovery_window", "channel_backups", "stateless_init", "extended_master_key", "extended_master_key_birthday_timestamp", "watch_only", "macaroon_root_key")
    WALLET_PASSWORD_FIELD_NUMBER: _ClassVar[int]
    CIPHER_SEED_MNEMONIC_FIELD_NUMBER: _ClassVar[int]
    AEZEED_PASSPHRASE_FIELD_NUMBER: _ClassVar[int]
    RECOVERY_WINDOW_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_BACKUPS_FIELD_NUMBER: _ClassVar[int]
    STATELESS_INIT_FIELD_NUMBER: _ClassVar[int]
    EXTENDED_MASTER_KEY_FIELD_NUMBER: _ClassVar[int]
    EXTENDED_MASTER_KEY_BIRTHDAY_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    WATCH_ONLY_FIELD_NUMBER: _ClassVar[int]
    MACAROON_ROOT_KEY_FIELD_NUMBER: _ClassVar[int]
    wallet_password: bytes
    cipher_seed_mnemonic: _containers.RepeatedScalarFieldContainer[str]
    aezeed_passphrase: bytes
    recovery_window: int
    channel_backups: _lightning_pb2.ChanBackupSnapshot
    stateless_init: bool
    extended_master_key: str
    extended_master_key_birthday_timestamp: int
    watch_only: WatchOnly
    macaroon_root_key: bytes
    def __init__(self, wallet_password: _Optional[bytes] = ..., cipher_seed_mnemonic: _Optional[_Iterable[str]] = ..., aezeed_passphrase: _Optional[bytes] = ..., recovery_window: _Optional[int] = ..., channel_backups: _Optional[_Union[_lightning_pb2.ChanBackupSnapshot, _Mapping]] = ..., stateless_init: _Optional[bool] = ..., extended_master_key: _Optional[str] = ..., extended_master_key_birthday_timestamp: _Optional[int] = ..., watch_only: _Optional[_Union[WatchOnly, _Mapping]] = ..., macaroon_root_key: _Optional[bytes] = ...) -> None: ...

class InitWalletResponse(_message.Message):
    __slots__ = ("admin_macaroon",)
    ADMIN_MACAROON_FIELD_NUMBER: _ClassVar[int]
    admin_macaroon: bytes
    def __init__(self, admin_macaroon: _Optional[bytes] = ...) -> None: ...

class WatchOnly(_message.Message):
    __slots__ = ("master_key_birthday_timestamp", "master_key_fingerprint", "accounts")
    MASTER_KEY_BIRTHDAY_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    MASTER_KEY_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    ACCOUNTS_FIELD_NUMBER: _ClassVar[int]
    master_key_birthday_timestamp: int
    master_key_fingerprint: bytes
    accounts: _containers.RepeatedCompositeFieldContainer[WatchOnlyAccount]
    def __init__(self, master_key_birthday_timestamp: _Optional[int] = ..., master_key_fingerprint: _Optional[bytes] = ..., accounts: _Optional[_Iterable[_Union[WatchOnlyAccount, _Mapping]]] = ...) -> None: ...

class WatchOnlyAccount(_message.Message):
    __slots__ = ("purpose", "coin_type", "account", "xpub")
    PURPOSE_FIELD_NUMBER: _ClassVar[int]
    COIN_TYPE_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    XPUB_FIELD_NUMBER: _ClassVar[int]
    purpose: int
    coin_type: int
    account: int
    xpub: str
    def __init__(self, purpose: _Optional[int] = ..., coin_type: _Optional[int] = ..., account: _Optional[int] = ..., xpub: _Optional[str] = ...) -> None: ...

class UnlockWalletRequest(_message.Message):
    __slots__ = ("wallet_password", "recovery_window", "channel_backups", "stateless_init")
    WALLET_PASSWORD_FIELD_NUMBER: _ClassVar[int]
    RECOVERY_WINDOW_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_BACKUPS_FIELD_NUMBER: _ClassVar[int]
    STATELESS_INIT_FIELD_NUMBER: _ClassVar[int]
    wallet_password: bytes
    recovery_window: int
    channel_backups: _lightning_pb2.ChanBackupSnapshot
    stateless_init: bool
    def __init__(self, wallet_password: _Optional[bytes] = ..., recovery_window: _Optional[int] = ..., channel_backups: _Optional[_Union[_lightning_pb2.ChanBackupSnapshot, _Mapping]] = ..., stateless_init: _Optional[bool] = ...) -> None: ...

class UnlockWalletResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ChangePasswordRequest(_message.Message):
    __slots__ = ("current_password", "new_password", "stateless_init", "new_macaroon_root_key")
    CURRENT_PASSWORD_FIELD_NUMBER: _ClassVar[int]
    NEW_PASSWORD_FIELD_NUMBER: _ClassVar[int]
    STATELESS_INIT_FIELD_NUMBER: _ClassVar[int]
    NEW_MACAROON_ROOT_KEY_FIELD_NUMBER: _ClassVar[int]
    current_password: bytes
    new_password: bytes
    stateless_init: bool
    new_macaroon_root_key: bool
    def __init__(self, current_password: _Optional[bytes] = ..., new_password: _Optional[bytes] = ..., stateless_init: _Optional[bool] = ..., new_macaroon_root_key: _Optional[bool] = ...) -> None: ...

class ChangePasswordResponse(_message.Message):
    __slots__ = ("admin_macaroon",)
    ADMIN_MACAROON_FIELD_NUMBER: _ClassVar[int]
    admin_macaroon: bytes
    def __init__(self, admin_macaroon: _Optional[bytes] = ...) -> None: ...
