from typing import List, NamedTuple

from bitstring import BitArray, Bits, ConstBitStream, pack

from ..bit_utils import int_to_scid, scid_to_int


class Route(NamedTuple):
    public_key: str
    short_channel_id: str
    base_fee: int
    ppm_fee: int
    cltv_expiry_delta: int


class RouteHint(NamedTuple):
    routes: List[Route]

    @classmethod
    def from_bitstring(cls, data: Bits) -> "RouteHint":
        stream = ConstBitStream(data)
        route_hints = []
        while stream.pos + 264 + 64 + 32 + 32 + 16 < stream.len:
            route = Route(
                public_key=stream.read(264).tobytes().hex(),
                short_channel_id=int_to_scid(stream.read(64).intbe),
                base_fee=stream.read(32).intbe,
                ppm_fee=stream.read(32).intbe,
                cltv_expiry_delta=stream.read(16).intbe,
            )
            route_hints.append(route)
        return cls(routes=route_hints)

    @classmethod
    def from_list(cls, list_of_routes: List[dict]) -> "RouteHint":
        return cls(routes=[Route(**route) for route in list_of_routes])

    @property
    def data(self) -> Bits:
        route_hints = BitArray()
        for route in self.routes:
            route_hints.append(
                BitArray(hex=route.public_key)
                + pack("intbe:64", scid_to_int(route.short_channel_id))
                + pack("intbe:32", route.base_fee)
                + pack("intbe:32", route.ppm_fee)
                + pack("intbe:16", route.cltv_expiry_delta)
            )

        return route_hints
