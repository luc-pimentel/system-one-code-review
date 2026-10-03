"""Every file s1cr keeps and every service it calls, each named once with what it holds.

`python -m scripts.flow` reads this module to tell which command reads and writes what. A file or a
service named anywhere else shows up there as one nobody named.
"""

from pathlib import Path

DATA = Path("data")  # everything built from SWR-Bench, kept between commands
SOURCE = DATA / "swrbench" / "swr_datasets_d5c5.jsonl"  # SWR-Bench at its pinned commit, one record per line
COMPARE_CACHE = DATA / "compare"  # GitHub's commit comparisons, one JSON file per compared range
ROWS = DATA / "rows.jsonl"  # one Row per line: a pull request as the model reads it, with its labels
RUNS = Path("runs")  # benchmark runs, one directory each, named on the command line
REPORT = Path("reports/jev-swrbench.md")  # the benchmark report, written from the saved answers

JEV = "https://api.typesafe.ai/v1/systemone"  # TypeSafe's Jev: answers the questions with probabilities
GITHUB_API = "https://api.github.com"  # GitHub's REST API: the commits a benchmark pull request compares
GITHUB_RAW = "https://raw.githubusercontent.com"  # GitHub's raw files: SWR-Bench at its pinned commit
GH = "gh"  # the GitHub CLI: live pull requests, and the token for GitHub's API
GIT = "git"  # this checkout's commit and status, recorded with every run


def answers(run: Path) -> Path:
    """One Attempt per line: each pull request's answers, or the error that replaced them."""
    return run / "answers.jsonl"


def receipt(run: Path) -> Path:
    """The Receipt that ties a run to its commit, model and inputs."""
    return run / "run.json"
