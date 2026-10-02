import json
import subprocess
from dataclasses import replace

import pytest

from s1cr import provenance
from s1cr.swrbench import FileDiff, Row


@pytest.fixture
def rows():
    return [
        Row("a", "o/r", "2020", "Title", "Body", [FileDiff("a.py", "+x", 1)], True, ["F.2"]),
        Row("b", "o/r", "2020", "Other", "Body", [], False, excluded="no file changes"),
    ]


@pytest.fixture
def snapshot(monkeypatch):
    current = {"commit": "a" * 40, "dirty": False}
    monkeypatch.setattr(provenance, "git_snapshot", lambda: dict(current))
    return current


def test_receipt_identifies_exact_inputs_and_resumes_without_rewriting(tmp_path, rows, snapshot):
    receipt = provenance.prepare(tmp_path, rows, "jev-1.13.0", 4)
    original = (tmp_path / "run.json").read_bytes()
    assert receipt["git_commit"] == "a" * 40
    assert receipt["case_ids"] == ["a", "b"]
    assert receipt["eligible_ids"] == ["a"]
    assert receipt["dataset"]["rows_sha256"] == provenance.rows_hash(rows)
    assert provenance.prepare(tmp_path, rows, "jev-1.13.0", 4) == receipt
    assert (tmp_path / "run.json").read_bytes() == original
    assert provenance.select_rows(receipt, list(reversed(rows))) == rows


@pytest.mark.parametrize("change", ["commit", "model", "workers", "input", "label", "selection"])
def test_resume_rejects_changed_execution_before_appending(tmp_path, rows, snapshot, change):
    provenance.prepare(tmp_path, rows, "jev-1.13.0", 4)
    original = (tmp_path / "run.json").read_bytes()
    model, workers = "jev-1.13.0", 4
    if change == "commit":
        snapshot["commit"] = "b" * 40
    elif change == "model":
        model = "jev-1.14.0"
    elif change == "workers":
        workers = 1
    elif change == "input":
        rows[0] = replace(rows[0], description="Changed")
    elif change == "label":
        rows[0] = replace(rows[0], categories=["E.1.1"])
    else:
        rows = rows[:1]
    with pytest.raises(ValueError, match="use a new run name"):
        provenance.prepare(tmp_path, rows, model, workers)
    assert (tmp_path / "run.json").read_bytes() == original


def test_dirty_code_and_unpinned_models_cannot_claim_a_committed_run(tmp_path, rows, snapshot):
    snapshot["dirty"] = True
    with pytest.raises(ValueError, match="commit source changes"):
        provenance.prepare(tmp_path, rows, "jev-1.13.0", 4)
    with pytest.raises(ValueError, match="pinned model"):
        provenance.prepare(tmp_path, rows, "jev-latest", 4)
    assert not (tmp_path / "run.json").exists()


def test_existing_answers_are_not_given_invented_provenance(tmp_path, rows, snapshot):
    (tmp_path / "answers.jsonl").write_text('{"id":"a","answers":{}}\n')
    with pytest.raises(ValueError, match="answers without provenance"):
        provenance.prepare(tmp_path, rows, "jev-1.13.0", 4)
    assert not (tmp_path / "run.json").exists()


def test_tampered_answers_and_benchmark_rows_are_rejected(tmp_path, rows, snapshot):
    receipt = provenance.prepare(tmp_path, rows, "jev-1.13.0", 4)
    (tmp_path / "answers.jsonl").write_text(
        json.dumps(
            {
                "id": "a",
                "answers": {},
                "model": "jev-1.14.0",
                "questions": "v1",
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="model/questions"):
        provenance.prepare(tmp_path, rows, "jev-1.13.0", 4)
    with pytest.raises(ValueError, match="inputs or labels differ"):
        provenance.select_rows(receipt, [replace(rows[0], title="Different"), rows[1]])


def test_git_snapshot_uses_the_worktree_commit_and_ignores_run_artifacts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "--initial-branch=main")
    (repo / "code.py").write_text("print('baseline')\n")
    git("add", "code.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")
    worktree = tmp_path / "baseline"
    git("worktree", "add", "--detach", str(worktree), baseline)
    (repo / "code.py").write_text("print('candidate')\n")
    git("add", "code.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "candidate")
    assert provenance.git_snapshot(repo)["commit"] != baseline
    assert provenance.git_snapshot(worktree) == {"commit": baseline, "dirty": False}
    (worktree / "runs").mkdir()
    (worktree / "runs" / "result.json").write_text("{}")
    assert provenance.git_snapshot(worktree)["dirty"] is False
    (worktree / "new_code.py").write_text("x = 1\n")
    assert provenance.git_snapshot(worktree)["dirty"] is True
    (repo / "code.py").write_text("print('uncommitted')\n")
    git("add", "code.py")
    assert provenance.git_snapshot(repo)["dirty"] is True
