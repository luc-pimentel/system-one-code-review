import json
from dataclasses import replace

import pytest
from test_score import answer, make_rows

from s1cr import cli, provenance
from s1cr.comparison import compare, readable


@pytest.fixture
def recorded_runs(tmp_path, monkeypatch):
    rows = make_rows()
    paths = [tmp_path / "baseline", tmp_path / "candidate"]
    for path, commit, shift, multiplier in zip(paths, ("a", "b"), (0.0, 0.25), (1, 2), strict=True):
        monkeypatch.setattr(
            provenance, "git_snapshot", lambda commit=commit: {"commit": commit * 40, "dirty": False}
        )
        provenance.prepare(path, rows, "jev-1.13.0", 4)
        entries = []
        for row in rows:
            if row.excluded is not None:
                continue
            if row.id == "huge":
                entries.append({"id": row.id, "error": "max_tokens_exceeded"})
            else:
                entry = answer(row, shift) | {"questions": "v1"}
                entry["ms"] *= multiplier
                entry["usage"] = {key: value * multiplier for key, value in entry["usage"].items()}
                entries.append(entry)
        (path / "answers.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return rows, *paths


def test_comparison_scores_both_commits_with_the_same_evaluator(recorded_runs):
    rows, baseline, candidate = recorded_runs
    result = compare(rows, baseline, candidate)
    assert result["baseline"]["git_commit"] == "a" * 40
    assert result["candidate"]["git_commit"] == "b" * 40
    assert result["cases"]["selected"] == 10
    assert result["cases"]["eligible"] == 9
    assert result["cases"]["matched"] == 8
    scores = result["metrics"]
    assert scores["functional_auroc"]["baseline"] == scores["functional_auroc"]["candidate"] == 1.0
    assert scores["functional_accuracy"] == {"n": 8, "baseline": 1.0, "candidate": 0.25, "delta": -0.75}
    assert scores["functional_brier"]["delta"] == pytest.approx(0.1375)
    assert scores["category_accuracy"]["n"] == 3
    assert scores["category_accuracy"]["baseline"] == pytest.approx(2 / 3)
    assert scores["file_accuracy"]["n"] == 2
    assert scores["file_accuracy"]["baseline"] == 0.5
    assert scores["median_ms"]["delta"] == 400
    assert scores["input_tokens"]["delta"] == 8000
    assert scores["estimated_input_usd"]["delta"] == pytest.approx(0.000336)
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    text = readable(result)
    assert "Completed: 8/9 baseline, 8/9 candidate" in text
    assert "Failed/pending: 1/0 baseline, 1/0 candidate" in text


def test_failed_and_unattempted_cases_are_visible_and_never_scored_as_success(recorded_runs):
    rows, baseline, candidate = recorded_runs
    path = candidate / "answers.jsonl"
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    entries = [entry for entry in entries if entry["id"] not in {"clean-0", "clean-1"}]
    entries.append({"id": "clean-0", "error": "HTTP 503"})
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    result = compare(rows, baseline, candidate)
    assert result["candidate"]["completed"] == 6
    assert result["candidate"]["failed"] == 2
    assert result["candidate"]["pending"] == 1
    assert result["cases"]["baseline_only"] == ["clean-0", "clean-1"]
    assert result["cases"]["matched"] == result["metrics"]["functional_accuracy"]["n"] == 6
    assert result["metrics"]["input_tokens"]["baseline"] == 6000
    assert "6 PRs answered by both" in readable(result)


def test_missing_usage_is_not_reported_as_free(recorded_runs):
    rows, baseline, candidate = recorded_runs
    path = candidate / "answers.jsonl"
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    entries[0]["usage"] = None
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    result = compare(rows, baseline, candidate)
    assert result["metrics"]["estimated_input_usd"]["candidate"] is None
    assert result["metrics"]["estimated_input_usd"]["delta"] is None


def test_empty_overlap_is_reported_without_nan_or_crashing(recorded_runs):
    rows, baseline, candidate = recorded_runs
    (candidate / "answers.jsonl").write_text("")
    result = compare(rows, baseline, candidate)
    assert result["cases"]["matched"] == 0
    assert result["candidate"]["pending"] == 9
    assert all(metric["candidate"] is None for metric in result["metrics"].values())
    assert "n/a" in readable(result)
    json.dumps(result, allow_nan=False)


def test_single_class_overlap_has_no_auroc_but_still_has_accuracy(recorded_runs):
    rows, baseline, candidate = recorded_runs
    path = candidate / "answers.jsonl"
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries if entry["id"].startswith("clean-"))
    )
    result = compare(rows, baseline, candidate)
    assert result["cases"]["matched"] == 4
    assert result["metrics"]["functional_auroc"]["baseline"] is None
    assert result["metrics"]["functional_accuracy"]["baseline"] == 1.0
    assert result["metrics"]["category_accuracy"]["n"] == 0
    json.dumps(result, allow_nan=False)


def test_different_selection_and_changed_rows_cannot_be_compared(recorded_runs):
    rows, baseline, candidate = recorded_runs
    changed = [replace(rows[0], description="Different diff context"), *rows[1:]]
    with pytest.raises(ValueError, match="inputs or labels differ"):
        compare(changed, baseline, candidate)
    path = candidate / "run.json"
    receipt = json.loads(path.read_text())
    receipt["case_ids"].remove("clean-0")
    receipt["eligible_ids"].remove("clean-0")
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="same benchmark inputs"):
        compare(rows, baseline, candidate)


def test_compare_accepts_worktree_paths_and_outputs_json_without_an_api_key(
    recorded_runs, monkeypatch, capsys
):
    rows, baseline, candidate = recorded_runs
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    rows_path = baseline.parent / "rows.jsonl"
    rows_path.write_text("".join(row.to_json() + "\n" for row in rows))
    cli.main(["compare", str(baseline), str(candidate), "--rows", str(rows_path), "--json"])
    result = json.loads(capsys.readouterr().out)
    assert result["cases"]["matched"] == 8


def test_legacy_runs_are_not_assigned_a_git_commit(recorded_runs):
    rows, baseline, _ = recorded_runs
    legacy = baseline.parent / "legacy"
    legacy.mkdir()
    with pytest.raises(ValueError, match=r"no run\.json"):
        compare(rows, baseline, legacy)
