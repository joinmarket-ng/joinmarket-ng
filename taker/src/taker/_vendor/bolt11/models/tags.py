from enum import Enum
from typing import Any, List, Optional

from bech32 import CHARSET

from .features import Features
from .routehint import RouteHint


class TagChar(Enum):
    description = "d"
    description_hash = "h"
    payment_hash = "p"
    payment_secret = "s"
    payee = "n"
    fallback = "f"
    route_hint = "r"
    expire_time = "x"
    min_final_cltv_expiry = "c"
    metadata = "m"
    features = "9"


class Tag:
    char: TagChar
    data: Any

    def __init__(self, char: TagChar, data: Any) -> None:
        self.char = char
        self.data = data

    @property
    def bech32(self) -> int:
        char = self.char.value
        return CHARSET.find(char)


class Tags:
    tags: List[Tag]

    def __init__(self, tags: Optional[List[Tag]] = None) -> None:
        self.tags = tags or []

    def __iter__(self):
        for tag in self.tags:
            yield tag

    def add(self, char: TagChar, data: Any) -> None:
        self.tags.append(Tag(char, data))

    def has(self, char: TagChar) -> bool:
        for tag in self.tags:
            if tag.char == char:
                return True
        return False

    def get(self, char: TagChar) -> Optional[Tag]:
        for tag in self.tags:
            if tag.char == char:
                return tag
        return None

    def get_route_hints(self) -> List[RouteHint]:
        return [tag.data for tag in self.tags if tag.char == TagChar.route_hint]

    @classmethod
    def from_dict(cls, data: dict) -> "Tags":
        tags = []
        for char, value in data.items():
            if char in ["currency", "amount_msat", "date", "signature"]:
                continue
            elif char == TagChar.features.name or char == TagChar.features.value:
                tags.append(Tag(TagChar.features, Features.from_dict(value)))
            elif char in TagChar._member_map_:
                member_char = TagChar._member_map_.get(char)
                assert member_char, "member char should not be None after check"
                tags.append(Tag(TagChar(member_char), value))
            elif char in TagChar._value2member_map_:
                tags.append(Tag(TagChar(char), value))
            else:
                raise ValueError(f"invalid tag char: {char}")
        return cls(tags)
