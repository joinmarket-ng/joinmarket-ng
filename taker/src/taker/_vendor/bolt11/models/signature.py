from dataclasses import dataclass
from hashlib import sha256
from typing import Final

from bitcointx.core.key import CKey, CPubKey

SECP256K1_ORDER: Final = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _signature_hash(hrp: str, signing_data: bytes) -> bytes:
    return sha256(message(hrp, signing_data)).digest()


def _validated_signature_data(signature_data: bytes) -> tuple[bytes, int]:
    if len(signature_data) != 65:
        raise ValueError("Invalid signature data")

    recovery_flag = signature_data[64]
    if recovery_flag not in range(4):
        raise ValueError("Invalid signature data")

    return signature_data[:64], recovery_flag


def _der_integer(scalar: int) -> bytes:
    encoded = scalar.to_bytes(32, byteorder="big").lstrip(b"\x00")
    if encoded[0] & 0x80:
        encoded = b"\x00" + encoded
    return b"\x02" + bytes([len(encoded)]) + encoded


def _der_signature(raw_signature: bytes) -> bytes:
    r = int.from_bytes(raw_signature[:32], byteorder="big")
    s = int.from_bytes(raw_signature[32:], byteorder="big")
    if not 0 < r < SECP256K1_ORDER or not 0 < s < SECP256K1_ORDER:
        raise ValueError("Invalid signature")
    # BOLT11 requires low-S with an explicit payee; CPubKey.verify normalizes S.
    if s > SECP256K1_ORDER // 2:
        raise ValueError("Invalid signature")

    encoded = _der_integer(r) + _der_integer(s)
    return b"\x30" + bytes([len(encoded)]) + encoded


def message(hrp: str, signing_data: bytes) -> bytes:
    return bytes([ord(c) for c in hrp]) + signing_data


@dataclass
class Signature:
    """An invoice signature."""

    hrp: str
    signing_data: bytes
    signature_data: bytes

    @classmethod
    def from_signature_data(
        cls, hrp: str, signature_data: bytes, signing_data: bytes
    ) -> "Signature":
        return cls(hrp=hrp, signature_data=signature_data, signing_data=signing_data)

    @classmethod
    def from_private_key(cls, hrp: str, private_key: str, signing_data: bytes) -> "Signature":
        key = CKey(bytes.fromhex(private_key))
        raw_signature, recovery_flag = key.sign_compact(_signature_hash(hrp, signing_data))
        signature_data = raw_signature + bytes([recovery_flag])
        return cls(hrp=hrp, signing_data=signing_data, signature_data=signature_data)

    def verify(self, payee: str) -> bool:
        if not self.signature_data:
            raise ValueError("No signature data")
        if not self.signing_data:
            raise ValueError("No signing data")
        raw_signature, _ = _validated_signature_data(self.signature_data)
        if not CPubKey(bytes.fromhex(payee)).verify(
            _signature_hash(self.hrp, self.signing_data), _der_signature(raw_signature)
        ):
            raise ValueError("Invalid signature")
        return True

    def recover_public_key(self) -> str:
        if not self.signature_data:
            raise ValueError("No signature data")
        if not self.signing_data:
            raise ValueError("No signing data")

        raw_signature, recovery_flag = _validated_signature_data(self.signature_data)
        key = CPubKey.recover_compact(
            _signature_hash(self.hrp, self.signing_data),
            bytes([31 + recovery_flag]) + raw_signature,
        )
        if key is None:
            raise ValueError("Invalid signature")
        return bytes(key).hex()

    @property
    def r(self) -> str:
        if not self.signature_data:
            raise ValueError("No signature data")
        return self.signature_data[:32].hex()

    @property
    def s(self) -> str:
        if not self.signature_data:
            raise ValueError("No signature data")
        return self.signature_data[32:64].hex()

    @property
    def sig(self) -> bytes:
        if not self.signature_data:
            raise ValueError("No signature data")
        return self.signature_data[0:64]

    @property
    def recovery_flag(self) -> int:
        if not self.signature_data:
            raise ValueError("No signature data")
        return int(self.signature_data[64])

    @property
    def preimage(self) -> bytes:
        if not self.signature_data:
            raise ValueError("No signature data")
        return sha256(self.signature_data).digest()

    @property
    def hex(self) -> str:
        if not self.signature_data:
            raise ValueError("No signature data")
        return self.signature_data[:64].hex()
