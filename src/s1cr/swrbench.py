"""SWR-Bench: 1,000 GitHub pull requests from 12 Python projects. Reviewers asked for changes to 500 of
them and approved the other 500 as they were; for the first 500 the benchmark records each problem they
found, its category, the commit that introduced it and the commit that fixed it.

A model reads each pull request the way its first reviewer did: the title, the description and the diff
of the commits pushed before the first review. The review timeline and the later commits hold the
answers, so they never reach a model.

Source: https://github.com/ZZR0/SWRench (MIT), paper: https://arxiv.org/abs/2509.01494
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from .models import MAX_STATE_CHARS, ReviewInput
from .models import FileDiff as FileDiff
from .questions import CATEGORIES as CATEGORIES

SOURCE_REPO = "ZZR0/SWRench"
SOURCE_COMMIT = "67ae1d4395ac05f800d62b0e13678eadeb9fa5c7"
SOURCE_PATH = "data/swr_datasets_d5c5.jsonl"
SOURCE_SHA256 = "7048e7a92ea9f7a1"  # first 16 hex digits of the file's SHA-256


@dataclass
class Row:
    """One pull request as a model sees it, with what its reviewers found."""

    id: str
    repo: str
    created_at: str
    title: str
    description: str
    files: list[FileDiff]
    changes_requested: bool  # reviewers asked for at least one change
    categories: list[str] = field(default_factory=list)  # category codes of the problems they found
    fault_files: list[str] = field(default_factory=list)  # files those problems were in, where known
    excluded: str | None = None  # why the pull request is left out, if it is

    def review_input(self) -> ReviewInput:
        """Strip the labels and benchmark metadata before invoking the reviewer."""
        return ReviewInput(self.title, self.description, self.files)

    @property
    def functional(self) -> bool:
        """Reviewers found at least one functional problem."""
        return any(code.startswith("F") for code in self.categories)

    @property
    def era(self) -> str:
        return "2024–25" if self.created_at[:4] >= "2024" else "2011–23"

    @property
    def changed_lines(self) -> int:
        return sum(f.changed for f in self.files)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "Row":
        data = json.loads(line)
        data["files"] = [FileDiff(**f) for f in data["files"]]
        return cls(**data)


def download(target: Path) -> Path:
    """Fetch the benchmark file at the pinned commit and check it is the one this code was built on."""
    url = f"https://raw.githubusercontent.com/{SOURCE_REPO}/{SOURCE_COMMIT}/{SOURCE_PATH}"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        response = httpx.get(url, timeout=300, follow_redirects=True)
        response.raise_for_status()
        target.write_bytes(response.content)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if not digest.startswith(SOURCE_SHA256):
        raise ValueError(
            f"{target} has SHA-256 {digest[:16]}, expected {SOURCE_SHA256}; delete it and fetch again"
        )
    return target


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def changed_lines(patch: str) -> int:
    return sum(
        1
        for line in patch.split("\n")
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )


def commit_files(commit: dict) -> list[FileDiff]:
    """The files one commit changes, with its patches."""
    patches: dict[str, list[str]] = {}
    for part in commit["diff"]:
        patches.setdefault(part["file"], []).append(part["patch"])
    return [FileDiff(path, "\n".join(p), changed_lines("\n".join(p))) for path, p in patches.items()]


def fault_files(record: dict, files: list[FileDiff]) -> list[str]:
    """The files the reviewers' problems were in. Most change snippets start with their file's path; for the
    rest, the file is the only one whose diff holds one of the snippet's longer lines."""
    paths = [f.path for f in files]
    found: set[str] = set()
    for change in record["changes"]:
        snippet = change["change_introducing"]["code_snippet"]
        first = snippet.split("\n", 1)[0].strip()
        if first in paths:
            found.add(first)
            continue
        lines = {line[1:].strip() if line[:1] in "+- " else line.strip() for line in snippet.split("\n")}
        for line in sorted(lines, key=len, reverse=True)[:5]:
            if len(line) < 12:
                break
            holders = [f.path for f in files if line in f.patch]
            if len(holders) == 1:
                found.add(holders[0])
                break
    return sorted(found)


Compare = Callable[[str, str, str], tuple[list[FileDiff], int]]


def to_row(record: dict, compare: Compare) -> Row:
    """One benchmark record as a row. `compare(repo, base, head)` returns the files changed between two
    commits and how many commits lie between them, for pull requests with more than one commit."""
    commits = record["pr_commits"]
    row = Row(
        id=record["instance_id"],
        repo=record["repo"],
        created_at=record["created_at"],
        title=record["pr_title"],
        description=record["pr_statement"],
        files=[],
        changes_requested=bool(record["change_introduced"]),
        categories=[change["change_type"].split()[0] for change in record["changes"]],
    )
    if any(c["message"].lower().startswith("merge") for c in commits):
        row.excluded = "a merge commit before the first review mixes other work into the diff"
        return row
    if len(commits) == 1:
        row.files = commit_files(commits[0])
    else:
        try:
            row.files, ahead = compare(record["repo"], record["base_commit"], commits[-1]["sha"])
        except Exception as error:  # the commits are gone from GitHub, or the API refused
            row.excluded = f"GitHub could not compare the commits: {error}"
            return row
        if ahead != len(commits):
            row.excluded = f"the compared range holds {ahead} commits, not the pull request's {len(commits)}"
            return row
    if not row.files:
        row.excluded = "no file changes"
    elif sum(len(f.patch) + len(f.path) for f in row.files) + len(row.title) > MAX_STATE_CHARS:
        row.excluded = "too large for one Jev call"
    row.fault_files = fault_files(record, row.files)
    return row
