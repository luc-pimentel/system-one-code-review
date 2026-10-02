import json

import pytest

from s1cr import provenance
from s1cr.report import write_report
from s1cr.score import score
from s1cr.swrbench import FileDiff, Row


def make_rows() -> list[Row]:
    one = [FileDiff("a.py", "+x", 1)]
    two = [FileDiff("a.py", "+x", 3), FileDiff("b.py", "+y", 1)]
    rows = [
        Row("r-logic", "o/r", "2020", "t", "d", two, True, ["F.2"], ["b.py"]),
        Row("r-check", "o/r", "2024", "t", "d", one, True, ["F.4"], ["a.py"]),
        Row("r-text", "o/r", "2021", "t", "d", two, True, ["E.1.1"], ["a.py"]),
        Row("r-org", "o/r", "2025", "t", "d", one, True, ["E.3.1", "E.1.1"], ["a.py"]),
    ]
    rows += [Row(f"clean-{i}", "o/r", f"20{18 + i}", "t", "d", one, False) for i in range(4)]
    rows.append(Row("huge", "o/r", "2020", "t", "d", one, False))
    rows.append(Row("merge", "o/r", "2020", "t", "d", [], True, excluded="a merge commit: mixes work"))
    return rows


def answer(row: Row, shift: float) -> dict:
    functional = any(c.startswith("F") for c in row.categories)
    answers = {
        "changes_requested": {
            "type": "noul",
            "noul": min(1.0, (0.8 if row.changes_requested else 0.2) + shift),
        },
        "functional_defect": {"type": "noul", "noul": min(1.0, (0.7 if functional else 0.3) + shift)},
        "problem_type": {"type": "choice", "choice": "F2" if functional else "E11", "confidence": 0.5},
    }
    if len(row.files) > 1:
        answers["fault_file"] = {"type": "choice", "choice": "f1", "confidence": 0.6}
    return {
        "id": row.id,
        "model": "jev-1.13.0",
        "answers": answers,
        "usage": {"input_tokens": 1000, "output_tokens": 50},
        "ms": 400,
    }


@pytest.fixture
def runs(tmp_path):
    rows = [r for r in make_rows() if r.excluded is None]
    for name, shift in (("r1", 0.0), ("r2", 0.25)):
        lines = [answer(r, shift) for r in rows if r.id != "huge"]
        lines.append({"id": "huge", "error": 'Jev HTTP 400: {"detail":{"error_type":"max_tokens_exceeded"}}'})
        (tmp_path / name).mkdir()
        (tmp_path / name / "answers.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    return tmp_path


def test_scoring_counts_what_was_asked_and_leaves_out_what_could_not_be(runs, tmp_path):
    results = score(make_rows(), runs, "r1")
    assert (results.total, results.kept) == (10, 8)
    assert results.excluded == {"a merge commit": 1, "over Jev's token limit": 1}
    assert results.model == "jev-1.13.0"
    assert results.changes.auroc == 1.0 and results.changes.accuracy == 1.0
    assert results.functional.positives == 2 and results.functional.auroc == 1.0
    assert results.functional_vs_clean.n == 6  # the two evolvability-only pull requests are left out
    # r-check is F.4 but gets F2, r-text is E.1.1 and gets E11; r-org has two problems, so it is not scored
    assert results.problem_type.n == 3 and results.problem_type.accuracy == pytest.approx(2 / 3)
    # f1 is b.py: right for r-logic, wrong for r-text
    assert results.fault_file.n == 2 and results.fault_file.accuracy == 0.5
    stable = results.stability
    assert stable.runs == ["r1", "r2"]
    assert stable.mean_change["changes_requested"] == pytest.approx(
        (4 * 0.2 + 4 * 0.25) / 8
    )  # 0.8 stops at 1.0
    assert stable.flips["changes_requested"] == 0  # 0.2 + 0.25 is still no
    assert stable.flips["functional_defect"] == pytest.approx(6 / 8)  # 0.3 + 0.25 turns no into yes
    assert results.cost.calls == 8 and results.cost.input_tokens == 8000


def test_the_report_is_written(runs, tmp_path):
    output = tmp_path / "report.md"
    write_report(score(make_rows(), runs, "r1"), output)
    text = output.read_text()
    for heading in (
        "## Summary",
        "## 2. Would a reviewer ask for changes?",
        "## 6. Asking again",
        "## Reproduce",
    ):
        assert heading in text


def test_an_unfinished_run_is_refused(runs):
    lines = (runs / "r1" / "answers.jsonl").read_text().splitlines()
    (runs / "r1" / "answers.jsonl").write_text("\n".join(lines[1:]) + "\n")
    with pytest.raises(ValueError, match="rerun it to finish"):
        score(make_rows(), runs, "r1")


def test_stability_only_groups_repetitions_of_the_same_commit_and_configuration(tmp_path, monkeypatch):
    rows = make_rows()
    for name, commit in (("baseline", "a"), ("repeat", "a"), ("candidate", "b")):
        monkeypatch.setattr(
            provenance, "git_snapshot", lambda commit=commit: {"commit": commit * 40, "dirty": False}
        )
        path = tmp_path / name
        provenance.prepare(path, rows, "jev-1.13.0", 4)
        entries = [answer(row, 0) | {"questions": "v1"} for row in rows if row.excluded is None]
        (path / "answers.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    results = score(rows, tmp_path, "baseline")
    assert results.stability.runs == ["baseline", "repeat"]
    assert score(rows, tmp_path, "candidate").stability is None
