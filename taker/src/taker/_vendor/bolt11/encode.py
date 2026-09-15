from typing import Optional

from bech32 import bech32_encode
from bitstring import BitArray, Bits, pack

from .bit_utils import bitarray_to_u5
from .exceptions import (
    Bolt11InvalidDescriptionHashException,
    Bolt11NoSignatureException,
)
from .models.signature import Signature
from .models.tags import TagChar
from .types import Bolt11, MilliSatoshi
from .utils import msat_to_amount


# Tagged field containing BitArray
def _tagged(char: int, bits: Bits):
    # Tagged fields need to be zero-padded to 5 bits.
    while bits.len % 5 != 0:
        bits = bits + "0b0"
    return (
        pack(
            "uint:5, uint:5, uint:5",
            char,
            (bits.len / 5) / 32,
            (bits.len / 5) % 32,
        )
        + bits
    )


def _tagged_bytes(char: int, bits: bytes):
    return _tagged(char, BitArray(bits))


def _tagged_int(tag: bytes):
    """Get minimal length by trimming leading 5 bits at a time."""
    value = pack("intbe:64", tag)[4:64]
    while value.startswith("0b00000"):
        value = value[5:]
    return value


def _create_hrp(currency: str, amount_msat: Optional[MilliSatoshi]) -> str:
    hrp = "ln" + currency
    if amount_msat:
        hrp += msat_to_amount(amount_msat)
    return hrp


def encode(
    invoice: Bolt11,
    private_key: Optional[str] = None,
    ignore_exceptions: bool = False,
    strict: bool = False,
    keep_payee: bool = False,
) -> str:
    try:
        if invoice.description_hash:
            bytes.fromhex(invoice.description_hash)
    except Exception as exc:
        raise Bolt11InvalidDescriptionHashException() from exc

    timestamp = BitArray(uint=invoice.date, length=35)
    tags = BitArray()

    for tag in invoice.tags:
        if tag.char == TagChar.payment_hash:
            tags += _tagged_bytes(tag.bech32, bytes.fromhex(tag.data))
        elif tag.char == TagChar.payment_secret:
            tags += _tagged_bytes(tag.bech32, bytes.fromhex(tag.data))
        elif tag.char == TagChar.description:
            tags += _tagged_bytes(tag.bech32, tag.data.encode())
        elif tag.char == TagChar.description_hash:
            tags += _tagged_bytes(tag.bech32, bytes.fromhex(tag.data))
        elif tag.char == TagChar.metadata:
            tags += _tagged_bytes(tag.bech32, bytes.fromhex(tag.data))
        elif tag.char == TagChar.payee and keep_payee:
            tags += _tagged_bytes(tag.bech32, bytes.fromhex(tag.data))
        elif tag.char == TagChar.features:
            tags += _tagged_bytes(tag.bech32, tag.data.data)
        elif tag.char == TagChar.fallback:
            tags += _tagged_bytes(tag.bech32, tag.data.data)
        elif tag.char == TagChar.min_final_cltv_expiry:
            tags += _tagged(tag.bech32, _tagged_int(tag.data))
        elif tag.char == TagChar.expire_time:
            tags += _tagged(tag.bech32, _tagged_int(tag.data))
        elif tag.char == TagChar.route_hint:
            tags += _tagged(tag.bech32, tag.data.data)

    hrp = _create_hrp(invoice.currency, invoice.amount_msat)
    data_part = timestamp + tags

    if private_key:
        invoice.signature = Signature.from_private_key(
            hrp=hrp, private_key=private_key, signing_data=data_part.tobytes()
        )

    if not invoice.signature:
        raise Bolt11NoSignatureException()

    signature_part = BitArray(invoice.signature.signature_data)

    if not ignore_exceptions:
        invoice.validate(strict=strict)

    encoded = bech32_encode(hrp, bitarray_to_u5(data_part + signature_part))

    return encoded
