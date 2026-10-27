import json
from enum import Enum
from math import floor
from typing import Dict, NamedTuple, Optional, Union

from bitstring import BitArray, Bits

from ..exceptions import Bolt11FeatureException


class FeatureState(Enum):
    required = 0
    supported = 1


class Feature(Enum):
    option_data_loss_protect = 0
    initial_routing_sync = 1
    option_upfront_shutdown_script = 2
    gossip_queries = 3
    var_onion_optin = 4
    gossip_queries_ex = 5
    option_static_remotekey = 6
    payment_secret = 7
    basic_mpp = 8
    option_support_large_channel = 9
    option_anchor_outputs = 10
    option_anchors_zero_fee_htlc_tx = 11
    option_route_blinding = 12
    option_shutdown_anysegwit = 13
    option_channel_type = 14
    option_scid_alias = 15
    option_payment_metadata = 16
    option_zeroconf = 17


class FeatureExtra:
    def __init__(self, index: int):
        if index < len(Feature):
            raise Bolt11FeatureException(f"FeatureExtra must be greater than {len(Feature) - 1}")
        self.feature_index = index

    @property
    def value(self) -> int:
        return self.feature_index

    @property
    def name(self) -> str:
        return f"extra_{self.feature_index}"


class Features(NamedTuple):
    data: Bits
    feature_list: Dict[Union[Feature, FeatureExtra], FeatureState]

    @classmethod
    def from_bitstring(cls, data: Bits) -> "Features":
        while data.len % 5 != 0:
            data = data + "0b0"
        length = data.length
        feature_list: Dict[Union[Feature, FeatureExtra], FeatureState] = {}
        for i in range(0, length):
            feature_index = floor(i / 2)
            si = i + 1
            cut = data[-si : -si + 1] if i > 0 else data[-si:]
            if cut == "0b1":
                feature: Union[Feature, FeatureExtra] = (
                    Feature(feature_index)
                    if feature_index < len(Feature)
                    else FeatureExtra(feature_index)
                )
                feature_list[feature] = FeatureState.supported if i % 2 else FeatureState.required
        return cls(data, feature_list)

    @classmethod
    def from_feature_list(
        cls, feature_list: Dict[Union[Feature, FeatureExtra], FeatureState]
    ) -> "Features":
        length = max([feature.value + 1 for feature in feature_list]) * 2
        data = BitArray(length=length)
        for feature, feature_state in feature_list.items():
            if feature_state == FeatureState.required:
                data.invert(feature.value * 2)
            elif feature_state == FeatureState.supported:
                data.invert(feature.value * 2 + 1)
            else:
                raise ValueError("Unknown feature state")
        # Remove trailing zeroes
        while data[-1:] == "0b0":
            data = BitArray(data[:-1])
        # add zeroes
        while data.len % 5 != 0:
            data = data + "0b0"
        data.reverse()
        return cls(data, feature_list)

    @property
    def readable(self) -> Dict[str, str]:
        json: Dict[str, str] = {}
        for feature, feature_state in self.feature_list.items():
            json[feature.name] = feature_state.name
        return json

    @property
    def json(self) -> str:
        return json.dumps(self.readable)

    @classmethod
    def from_dict(cls, data: dict) -> "Features":
        features: Dict[Union[Feature, FeatureExtra], FeatureState] = {}
        for char, value in data.items():
            if char in Feature._member_map_:
                features[Feature(Feature._member_map_.get(char))] = FeatureState(
                    FeatureState._member_map_.get(value)
                )
            else:
                if not char.startswith("extra_"):
                    raise ValueError(f"invalid feature char: {char}")
                extra_bit = int(char.replace("extra_", ""))
                features[FeatureExtra(extra_bit)] = FeatureState(
                    FeatureState._member_map_.get(value)
                )
        return cls.from_feature_list(features)

    def has_feature(self, feature_string: str) -> Optional[str]:
        for feature, feature_state in self.feature_list.items():
            if feature.name == feature_string:
                return feature_state.name
        return None
