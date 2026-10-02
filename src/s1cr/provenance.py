"""Link benchmark executions to Git commits and exact benchmark inputs.

Git resolution follows worktree-repos/tools/repo-checks/src/fleet_checks/source.py:
use git -C at the source root and resolve HEAD as a commit, including in worktrees.
"""

import hashlib
import json
import re
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from . import questions
from .swrbench import SOURCE_COMMIT, SOURCE_REPO, Row

SOURCE_ROOT = Path(__file__).resolve().parents[2]


def git_snapshot(root: Path = SOURCE_ROOT) -> dict:
    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
            ).stdout.strip()
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            raise ValueError("run from a Git checkout of system-one-code-review") from error

    if Path(git("rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise ValueError("the executed s1cr source must belong to its own Git checkout")
    commit = git("rev-parse", "--verify", "--end-of-options", "HEAD^{commit}")
    # Results and cached inputs can change as a run executes. Everything else must
    # be committed for the SHA to identify the implementation being measured.
    dirty = bool(
        git(
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude)runs",
            ":(exclude)reports",
            ":(exclude)data",
        )
    )
    return {"commit": commit, "dirty": dirty}


def rows_hash(rows: list[Row]) -> str:
    """Include inputs, labels, eligibility, and file ordering; ignore PR ordering."""
    payload = [asdict(row) for row in sorted(rows, key=lambda row: row.id)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def identity(receipt: dict) -> dict:
    return {key: value for key, value in receipt.items() if key != "created_at"}


def read(run_dir: Path) -> dict:
    path = run_dir / "run.json"
    if not path.exists():
        raise ValueError(
            f"{run_dir} has no run.json; start a new recorded run (legacy runs still support score)"
        )
    receipt = json.loads(path.read_text())
    required = {
        "git_commit",
        "model",
        "questions",
        "workers",
        "dataset",
        "case_ids",
        "eligible_ids",
        "created_at",
    }
    if not isinstance(receipt, dict) or not required <= receipt.keys():
        raise ValueError(f"invalid run receipt: {path}")
    ids, eligible = receipt["case_ids"], receipt["eligible_ids"]
    if len(set(ids)) != len(ids) or len(set(eligible)) != len(eligible) or not set(eligible) <= set(ids):
        raise ValueError(f"invalid case IDs in {path}")
    return receipt


def select_rows(receipt: dict, all_rows: list[Row]) -> list[Row]:
    indexed = {row.id: row for row in all_rows}
    if len(indexed) != len(all_rows):
        raise ValueError("benchmark rows contain duplicate IDs")
    missing = set(receipt["case_ids"]) - indexed.keys()
    if missing:
        raise ValueError(f"benchmark rows are missing {len(missing)} recorded cases")
    rows = [indexed[case_id] for case_id in receipt["case_ids"]]
    if rows_hash(rows) != receipt["dataset"]["rows_sha256"]:
        raise ValueError("benchmark inputs or labels differ from the recorded run")
    if {row.id for row in rows if row.excluded is None} != set(receipt["eligible_ids"]):
        raise ValueError("benchmark eligibility differs from the recorded run")
    return rows


def records(run_dir: Path, receipt: dict) -> list[dict]:
    """Validate all saved attempts before resuming or comparing a recorded run."""
    path = run_dir / "answers.jsonl"
    if not path.exists():
        return []
    eligible = set(receipt["eligible_ids"])
    entries = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line:
            continue
        entry = json.loads(line)
        if entry.get("id") not in eligible or not ("answers" in entry or "error" in entry):
            raise ValueError(f"unexpected result in {path}:{number}")
        if "answers" in entry and (
            entry.get("model") != receipt["model"] or entry.get("questions") != receipt["questions"]
        ):
            raise ValueError(f"result model/questions do not match {run_dir}/run.json")
        entries.append(entry)
    return entries


def prepare(run_dir: Path, rows: list[Row], model: str, workers: int) -> dict:
    """Create a receipt once, or require a matching receipt before appending answers."""
    if not re.fullmatch(r"jev-\d+\.\d+\.\d+", model):
        raise ValueError("benchmark runs require a pinned model, e.g. --model jev-1.13.0")
    if workers < 1:
        raise ValueError("workers must be at least 1")
    ids = [row.id for row in rows]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("a run needs a nonempty set of unique benchmark case IDs")
    snapshot = git_snapshot()
    if snapshot["dirty"]:
        raise ValueError("commit source changes before benchmarking, or use a clean Git worktree")
    expected = {
        "git_commit": snapshot["commit"],
        "model": model,
        "questions": questions.VERSION,
        "workers": workers,
        "dataset": {"repo": SOURCE_REPO, "commit": SOURCE_COMMIT, "rows_sha256": rows_hash(rows)},
        "case_ids": ids,
        "eligible_ids": [row.id for row in rows if row.excluded is None],
    }
    path = run_dir / "run.json"
    if path.exists():
        saved = read(run_dir)
        if identity(saved) != expected:
            raise ValueError("run commit, model, inputs, or execution settings changed; use a new run name")
        records(run_dir, saved)
        return saved
    answers = run_dir / "answers.jsonl"
    if answers.exists() and answers.stat().st_size:
        raise ValueError(f"{run_dir} has answers without provenance; use a new run name")
    receipt = {**expected, "created_at": datetime.now(UTC).isoformat()}
    run_dir.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    return receipt
