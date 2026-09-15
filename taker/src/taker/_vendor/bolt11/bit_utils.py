from typing import List

from bitstring import BitArray, Bits, ConstBitStream, pack


def int_to_scid(short_channel_id: int) -> str:
    blockheight = (short_channel_id >> 40) & 0xFFFFFF
    transactionindex = (short_channel_id >> 16) & 0xFFFFFF
    outputindex = short_channel_id & 0xFFFF
    return f"{blockheight}x{transactionindex}x{outputindex}"


def scid_to_int(short_channel_id: str) -> int:
    blockheight, transactionindex, outputindex = short_channel_id.split("x")
    scid = (int(blockheight) << 40) | (int(transactionindex) << 16) | int(outputindex)
    return scid


def bitarray_to_u5(barr):
    assert barr.len % 5 == 0
    ret = []
    s = ConstBitStream(barr)
    while s.pos != s.len:
        ret.append(s.read(5).uint)
    return ret


def u5_to_bitarray(arr: List[int]) -> Bits:
    """Bech32 spits out array of 5-bit values. Shim here."""
    ret = BitArray()
    for a in arr:
        ret += pack("uint:5", a)
    return ret


def trim_to_bytes(barr: Bits) -> bytes:
    """Adds a byte if necessary."""
    b = barr.tobytes()
    if barr.len % 8 != 0:
        return b[:-1]
    return b
