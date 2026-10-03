"""Inputs and results shared by live reviews and benchmark runs, and the JSON they are saved as."""

from dataclasses import asdict, dataclass
from typing import Literal, NotRequired, TypedDict

# The v1 benchmark's approximation of Jev's 32k-token state limit.
MAX_STATE_CHARS = 90_000
DEFAULT_MODEL = "jev-latest"

type NoulKey = Literal["changes_requested", "functional_defect"]
type ChoiceKey = Literal["problem_type", "fault_file"]
type UsageKey = Literal["input_tokens", "output_tokens"]


@dataclass
class FileDiff:
    """One changed file of a pull request."""

    path: str  # the file's path in the repository
    patch: str  # unified diff hunks, as GitHub shows them
    changed: int  # added plus removed lines


@dataclass
class ReviewInput:
    """Only information available to a reviewer; never benchmark labels."""

    title: str  # the pull request's title
    description: str  # the pull request's description, empty when it has none
    files: list[FileDiff]  # every changed file, in diff order


@dataclass(frozen=True)
class ReviewConfig:
    """How to ask Jev."""

    model: str = DEFAULT_MODEL  # the Jev version to ask, pinned or jev-latest


class Question(TypedDict):
    """One question as Jev reads it: its kind, what to decide, and what each answer means."""

    type: Literal["noul", "choice"]  # a yes/no question, or a choice between options
    instructions: str  # what Jev decides
    criteria: dict[str, str]  # what each answer means: "true" and "false", or each option's meaning


class FileState(TypedDict):
    """One changed file as Jev reads it."""

    path: str  # the file's path in the repository
    diff: str  # its unified diff hunks


class State(TypedDict):
    """What Jev reads about one pull request: never anything from its review."""

    pr_title: str  # the title
    pr_description: str  # the description, cut to the questions' limit
    files: dict[str, FileState]  # each changed file under an option name: f0, f1, ...


class JevRequest(TypedDict):
    """One call to Jev: the model, what it reads, and the questions it answers."""

    model: str  # the Jev version asked for
    state: State  # the pull request
    questions: dict[str, Question]  # each question under the key its answer comes back with


class NoulAnswer(TypedDict):
    """Jev's answer to a yes/no question."""

    type: Literal["noul"]  # a yes/no answer
    noul: float  # the probability of yes


class ChoiceAnswer(TypedDict):
    """Jev's answer to a Choice question."""

    type: Literal["choice"]  # a choice between options
    choice: str  # the option ranked first
    confidence: float  # the probability of that option
    probabilities: dict[str, float]  # every option's probability, by option name


class Answers(TypedDict):
    """Jev's answers to one pull request's questions."""

    changes_requested: NoulAnswer  # would a reviewer ask for at least one change
    functional_defect: NoulAnswer  # does the diff introduce a functional defect
    problem_type: ChoiceAnswer  # the kind of change a reviewer would most likely ask for
    fault_file: NotRequired[ChoiceAnswer]  # the file most likely to need it; asked only of several files


class Usage(TypedDict):
    """The tokens one Jev call used."""

    input_tokens: int  # tokens Jev read
    output_tokens: int  # tokens Jev wrote back


class JevResponse(TypedDict):
    """What Jev sends back for one call."""

    model: NotRequired[str]  # the Jev version that answered
    answers: Answers  # one answer per question asked
    usage: NotRequired[Usage]  # the tokens the call used


@dataclass
class ReviewResult:
    """Raw Jev judgments and execution metadata, in the existing run format.

    `answers` holds the two yes/no scores and the category/file Choice distributions.
    A single-file input has no file Choice answer: there was no choice to ask about.
    """

    questions: str  # the question set's version, e.g. v1
    model: str | None  # the Jev version that answered, when Jev said
    answers: Answers  # Jev's answers
    usage: Usage | None  # the tokens the call used, when Jev said
    ms: int  # how long the Jev call took, in milliseconds

    def to_dict(self) -> dict:
        return asdict(self)


class Attempt(TypedDict):
    """One line of a run's answers.jsonl: a pull request's answers, or the error that replaced them."""

    id: str  # the benchmark pull request
    questions: NotRequired[str]  # the question set's version, when Jev answered
    model: NotRequired[str | None]  # the Jev version that answered
    answers: NotRequired[Answers]  # present when the call succeeded
    usage: NotRequired[Usage | None]  # the tokens the call used
    ms: NotRequired[int]  # how long the call took, in milliseconds
    error: NotRequired[str]  # present when the call failed, or Jev answered with another model


class Dataset(TypedDict):
    """The benchmark inputs a run was measured on."""

    repo: str  # the SWR-Bench repository
    commit: str  # the SWR-Bench commit the rows were built from
    rows_sha256: str  # a hash of the selected rows: inputs, labels and eligibility


class Receipt(TypedDict):
    """What a benchmark run was measured on, saved before its first answer."""

    git_commit: str  # the s1cr commit that ran
    model: str  # the pinned Jev version
    questions: str  # the question set's version
    workers: int  # how many calls ran at once
    dataset: Dataset  # the benchmark inputs
    case_ids: list[str]  # every pull request the run selected
    eligible_ids: list[str]  # the selected pull requests that were not left out
    created_at: NotRequired[str]  # when the receipt was written; two runs may differ only here
