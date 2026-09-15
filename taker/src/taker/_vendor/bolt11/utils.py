from decimal import Decimal
from re import fullmatch, match
from typing import Optional, Tuple, Union

from .exceptions import Bolt11AmountInvalidException, Bolt11HrpInvalidException
from .types import MilliSatoshi


def verify_hrp(hrp: str) -> Tuple[str, Optional[MilliSatoshi]]:
    matches = match(r"ln(bcrt|bc|tbs|tb)(\w+)?", hrp)
    if matches is None:
        raise Bolt11HrpInvalidException()
    currency, amount_str = matches.groups()
    return currency, amount_to_msat(amount_str) if amount_str else None


def amount_to_msat(amount: str) -> MilliSatoshi:
    """Given a shortened amount, convert it into millisatoshis."""
    # BOLT #11:
    # A reader SHOULD fail if `amount` contains a non-digit, or is followed by
    # anything except a `multiplier` in the table above.
    if not fullmatch(r"\d+[pnum]?", amount):
        raise Bolt11AmountInvalidException()

    try:
        num = {"p": 10**12, "n": 10**9, "u": 10**6, "m": 10**3}[amount[-1]]
        # BOLT #11:
        # The following `multiplier` letters are defined:
        #
        # * `m` (milli): multiply by 0.001
        # * `u` (micro): multiply by 0.000001
        # * `n` (nano): multiply by 0.000000001
        # * `p` (pico): multiply by 0.000000000001
        return MilliSatoshi.from_btc(Decimal(amount[:-1]) / num)
    except KeyError:
        return MilliSatoshi.from_btc(Decimal(amount))


def amount_to_sat(amount: str) -> int:
    """Given a shortened amount, convert it into satoshis."""
    return amount_to_msat(amount).sat


def amount_to_btc(amount: str) -> Decimal:
    """Given a shortened amount, convert it into bitcoin."""
    return amount_to_msat(amount).btc


def msat_to_amount(msat: int) -> str:
    """Given an amount in millisatoshis, shorten it."""
    if not isinstance(msat, int):
        raise ValueError("`msat` should be an integer.")

    value = msat * 10

    unit = ""
    for unit in ["p", "n", "u", "m", ""]:
        if value % 1000 == 0:
            value //= 1000
        else:
            break

    return f"{value}{unit}"


def sat_to_amount(sat: int) -> str:
    """Given an amount in satoshis, shorten it."""
    return msat_to_amount(sat * 1000)


def btc_to_amount(btc: Union[int, Decimal]) -> str:
    """Given an amount in bitcoin, shorten it."""
    return msat_to_amount(MilliSatoshi.from_btc(Decimal(btc)))
