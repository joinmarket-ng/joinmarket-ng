from .decode import decode
from .utils import amount_to_btc, btc_to_amount


def lndecode(a):
    return decode(a)


def shorten_amount(amount):
    return btc_to_amount(amount)


def unshorten_amount(amount):
    return amount_to_btc(amount)
