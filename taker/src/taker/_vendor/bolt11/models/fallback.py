from typing import NamedTuple, Optional

from base58 import b58decode_check, b58encode_check
from bech32 import bech32_decode, bech32_encode
from bitstring import Bits, pack

from ..bit_utils import bitarray_to_u5, u5_to_bitarray

base58_prefix_map = {"bc": (0, 5), "tb": (111, 196)}


def is_p2pkh(currency, prefix):
    return prefix == base58_prefix_map[currency][0]


def is_p2sh(currency, prefix):
    return prefix == base58_prefix_map[currency][1]


class Fallback(NamedTuple):
    """Fallback onchain address"""

    data: Bits
    currency: str

    @classmethod
    def from_bitstring(cls, data: Bits, currency) -> Optional["Fallback"]:
        # fallback address type 19 are ignored
        wver = data[0:5].uint
        if wver == 19:
            return None
        return cls(data, currency)

    @classmethod
    def from_address(cls, address: str, currency: str) -> "Fallback":
        if currency == "bc" or currency == "tb":
            fbhrp, witness = bech32_decode(address)
            if fbhrp:
                assert witness
                if fbhrp != currency:
                    raise ValueError("Not a bech32 address for this currency")
                wver = witness[0]
                if wver > 16:
                    raise ValueError("Invalid witness version {}".format(witness[0]))
                wprog = u5_to_bitarray(witness[1:])
            else:
                addr = b58decode_check(address)
                if is_p2pkh(currency, addr[0]):
                    wver = 17
                elif is_p2sh(currency, addr[0]):
                    wver = 18
                else:
                    raise ValueError("Unknown address type for {}".format(currency))
                wprog = Bits(addr[1:])
            return cls(data=pack("uint:5", wver) + wprog, currency=currency)
        else:
            raise NotImplementedError("Support for currency {} not implemented".format(currency))

    @property
    def address(self) -> str:
        if self.currency in ["bc", "tb"]:
            wver = self.witness_version
            prefix = self.b58prefix
            if wver == 17:
                return self.b58encode_check(prefix[0])
            elif wver == 18:
                return self.b58encode_check(prefix[1])
            elif wver <= 16:
                return bech32_encode(self.currency, bitarray_to_u5(self.data))
            else:
                raise ValueError("Unknown witness version")
        else:
            raise ValueError("Unknown currency")

    def b58encode_check(self, prefix) -> str:
        return b58encode_check(bytes([prefix]) + self.data[5:].tobytes()).decode()

    @property
    def b58prefix(self) -> tuple:
        return base58_prefix_map[self.currency]

    @property
    def witness_version(self) -> int:
        return self.data[0:5].uint
