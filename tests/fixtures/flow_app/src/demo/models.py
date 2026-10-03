"""What demo passes around."""

from dataclasses import dataclass
from typing import TypedDict


@dataclass
class Item:
    """One thing demo scores."""

    name: str  # what the item is called
    size: int


class Score(TypedDict):
    """One item's score."""

    item: str  # the item's name
    value: float  # how good it is
