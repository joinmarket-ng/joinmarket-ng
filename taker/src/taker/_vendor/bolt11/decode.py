"""bolt11 decoder,
based on https://github.com/rustyrussell/lightning-payencode/blob/master/lnaddr.py
"""

from bech32 import CHARSET, bech32_decode
from bitstring import ConstBitStream

from .bit_utils import trim_to_bytes, u5_to_bitarray
from .exceptions import (
    Bolt11Bech32InvalidException,
    Bolt11SignatureTooShortException,
    Bolt11SignatureVerifyException,
)
from .models.fallback import Fallback
from .models.features import Features
from .models.routehint import RouteHint
from .models.signature import Signature
from .models.tags import TagChar, Tags
from .types import Bolt11
from .utils import verify_hrp


def _pull_tagged(stream):
    tag = stream.read(5).uint
    length = stream.read(5).uint * 32 + stream.read(5).uint
    return (CHARSET[tag], stream.read(length * 5), stream)


def decode(
    pr: str,
    ignore_exceptions: bool = False,
    strict: bool = False,
) -> Bolt11:
    pr = pr.lower()

    hrp, bech32_data = bech32_decode(pr)
    if hrp is None or bech32_data is None:
        raise Bolt11Bech32InvalidException()

    currency, amount_msat = verify_hrp(hrp)
    data = u5_to_bitarray(bech32_data)

    # final signature 65 bytes, split it off.
    if len(data) < 65 * 8:
        raise Bolt11SignatureTooShortException()

    # extract the signature
    signature_data = data[-65 * 8 :].tobytes()

    # the tagged fields as a bitstream
    data_part = ConstBitStream(data[: -65 * 8])

    timestamp = data_part.read(35).uint

    tags = Tags()
    payee = None

    while data_part.pos != data_part.len:
        tag, tagdata, data_part = _pull_tagged(data_part)
        data_length = int(len(tagdata or []) / 5)

        # MUST skip over unknown fields, OR an f field with unknown version, OR p, h,
        # s or n fields that do NOT have data_lengths of 52, 52, 52 or 53, respectively.

        if (
            tag == TagChar.payment_hash.value
            and data_length == 52
            and not tags.has(TagChar.payment_hash)
        ):
            tags.add(
                TagChar.payment_hash,
                trim_to_bytes(tagdata).hex(),
            )
        elif (
            tag == TagChar.description_hash.value
            and data_length == 52
            and not tags.has(TagChar.description_hash)
            and not tags.has(TagChar.description)
        ):
            tags.add(
                TagChar.description_hash,
                trim_to_bytes(tagdata).hex(),
            )
        elif (
            tag == TagChar.payment_secret.value
            and data_length == 52
            and not tags.has(TagChar.payment_secret)
        ):
            tags.add(
                TagChar.payment_secret,
                trim_to_bytes(tagdata).hex(),
            )
        elif tag == TagChar.payee.value and data_length == 53 and not tags.has(TagChar.payee):
            payee = trim_to_bytes(tagdata).hex()
            tags.add(
                TagChar.payee,
                payee,
            )
        elif (
            tag == TagChar.description.value
            and not tags.has(TagChar.description)
            and not tags.has(TagChar.description_hash)
        ):
            tags.add(
                TagChar.description,
                trim_to_bytes(tagdata).decode(),
            )
        elif tag == TagChar.metadata.value and not tags.has(TagChar.metadata):
            tags.add(
                TagChar.metadata,
                trim_to_bytes(tagdata).hex(),
            )
        elif tag == TagChar.expire_time.value and not tags.has(TagChar.expire_time):
            tags.add(
                TagChar.expire_time,
                tagdata.uint,
            )
        elif tag == TagChar.min_final_cltv_expiry.value and not tags.has(
            TagChar.min_final_cltv_expiry
        ):
            tags.add(
                TagChar.min_final_cltv_expiry,
                tagdata.uint,
            )
        elif tag == TagChar.fallback.value and not tags.has(TagChar.fallback):
            tags.add(
                TagChar.fallback,
                Fallback.from_bitstring(tagdata, currency),
            )
        elif tag == TagChar.features.value and not tags.has(TagChar.features):
            tags.add(TagChar.features, Features.from_bitstring(tagdata))
        elif tag == TagChar.route_hint.value:
            tags.add(TagChar.route_hint, RouteHint.from_bitstring(tagdata))

        else:
            # skip unknown fields
            pass

    signature = Signature(
        signature_data=signature_data,
        signing_data=data_part.tobytes(),
        hrp=hrp,
    )

    # A reader MUST check that the `signature` is valid (see the `n` tagged field
    # specified below). A reader MUST use the `n` field to validate the signature
    # instead of performing signature recovery if a valid `n` field is provided.
    if payee:
        try:
            signature.verify(payee)
        except Exception as exc:
            raise Bolt11SignatureVerifyException() from exc
    else:
        tags.add(
            TagChar.payee,
            signature.recover_public_key(),
        )

    bolt11 = Bolt11(
        currency=currency,
        amount_msat=amount_msat,
        date=timestamp,
        signature=signature,
        tags=tags,
    )

    if not ignore_exceptions:
        bolt11.validate(strict=strict)

    return bolt11
