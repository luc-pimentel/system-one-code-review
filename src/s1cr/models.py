"""Inputs and results shared by live reviews and benchmark runs."""

from dataclasses import asdict, dataclass

# The v1 benchmark's approximation of Jev's 32k-token state limit.
MAX_STATE_CHARS = 90_000
DEFAULT_MODEL = "jev-latest"


@dataclass
class FileDiff:
    path: str
    patch: str  # unified diff hunks, as GitHub shows them
    changed: int  # added plus removed lines


@dataclass
class ReviewInput:
    """Only information available to a reviewer; never benchmark labels."""

    title: str
    description: str
    files: list[FileDiff]


@dataclass(frozen=True)
class ReviewConfig:
    model: str = DEFAULT_MODEL


@dataclass
class ReviewResult:
    """Raw Jev judgments and execution metadata, in the existing run format.

    `answers` holds the two yes/no scores and the category/file Choice distributions.
    A single-file input has no file Choice answer: there was no choice to ask about.
    """

    questions: str
    model: str | None
    answers: dict
    usage: dict | None
    ms: int

    def to_dict(self) -> dict:
        return asdict(self)
