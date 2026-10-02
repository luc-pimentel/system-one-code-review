from pathlib import Path

import pytest

from scripts import pr_comment

ENV = {
    "GITHUB_REPOSITORY": "o/r",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_RUN_ID": "7",
    "HEAD_SHA": "c738d3a" + "0" * 33,
    "STEP_RESULTS": "ruff=success format=success tests=success",
}
LIMITS = {"decisions": 10, "branches": 12, "statements": 50}
PREVIOUS = (
    "🟢 **Checks:** pass. 0 ruff findings, 81 passed on `abc1234` ([run](https://github.com/o/r/actions/runs/6)).\n"
    "First run.\n<!-- checks -->"
)


def checks(
    tmp_path: Path,
    ruff="All checks passed!\n",
    fmt="22 files already formatted\n",
    tests="81 passed in 0.87s\n",
):
    (tmp_path / "ruff.txt").write_text(ruff)
    (tmp_path / "format.txt").write_text(fmt)
    (tmp_path / "pytest.txt").write_text(tests)
    return tmp_path


def test_body_summarises_the_checks_and_carries_the_marker(tmp_path):
    body = pr_comment.render(
        ENV,
        checks(tmp_path),
        None,
        changed="No function changed.",
        top=(8, "github.pull_request", 0),
        limits=LIMITS,
    )
    assert body.startswith(
        "🟢 **Checks:** pass. 0 ruff findings, 81 passed on `c738d3a` ([run](https://github.com/o/r/actions/runs/7)).\nFirst run.\n"
    )
    assert "| ruff check | ⚪ pass | 0 findings |" in body
    assert "| ruff format | ⚪ pass | 22 files already formatted |" in body
    assert "| pytest | ⚪ pass | 81 passed in 0.87s |" in body
    assert "| complexity | 🟡 watch | highest 8 of 10 (`github.pull_request`) |" in body
    assert "**Functions this PR touched**" in body
    assert "No function changed." in body
    assert "Limits: pyproject.toml (mccabe 10, 12 branches, 50 statements). Updated " in body
    assert body.endswith(pr_comment.MARKER)


def test_failures_count_findings_and_compare_with_the_previous_run(tmp_path):
    env = {**ENV, "STEP_RESULTS": "ruff=failure format=success tests=failure"}
    ruff = "src/a.py:1:1: F401 unused\nsrc/a.py:2:1: E501 long\nFound 2 errors.\n"
    tests = "FAILED tests/test_a.py::t - boom\n1 failed, 80 passed in 0.90s\n"
    body = pr_comment.render(
        env, checks(tmp_path, ruff=ruff, tests=tests), PREVIOUS, changed="x", top=(3, "a.f", 2), limits=LIMITS
    )
    assert body.startswith("🔴 **Checks:** fail. 2 ruff findings, 1 failed, 80 passed on `c738d3a`")
    assert (
        "Since last run: ruff 0 → 2 findings, tests 81 passed → 1 failed, 80 passed. "
        "Previous: pass ([run](https://github.com/o/r/actions/runs/6))." in body
    )
    assert "| ruff check | 🔴 fail | 2 findings |" in body
    assert "| pytest | 🔴 fail | 1 failed, 80 passed in 0.90s |" in body
    assert "| complexity | 🟡 watch | highest 3 of 10 (`a.f`), 2 decisions without a phrase |" in body


def test_steps_that_did_not_run_are_shown_as_such(tmp_path):
    env = {**ENV, "STEP_RESULTS": "ruff=skipped format=skipped tests=skipped"}
    body = pr_comment.render(env, tmp_path, None, changed="x", top=None, limits=LIMITS)
    assert body.startswith("🔴 **Checks:** fail. 0 ruff findings, no test result on `c738d3a`")
    assert "| ruff check | ⚫ not run | 0 findings |" in body
    assert "| complexity | ⚫ not run | |" in body


def test_comment_is_created_once_and_then_updated(monkeypatch):
    calls = []

    def fake_api(endpoint, *args, payload=None):
        calls.append((endpoint, args, payload))
        return {"html_url": "https://github.com/o/r/pull/4#issuecomment-1"}

    monkeypatch.setattr(pr_comment, "gh_api", fake_api)
    pr_comment.upsert_comment("o/r", "4", None, "body")
    pr_comment.upsert_comment("o/r", "4", {"id": 9}, "body")
    assert calls == [
        ("repos/o/r/issues/4/comments", ("--method", "POST"), {"body": "body"}),
        ("repos/o/r/issues/comments/9", ("--method", "PATCH"), {"body": "body"}),
    ]


def test_the_comment_is_found_by_its_marker(monkeypatch):
    pages = [[{"id": 1, "body": "hello"}], [{"id": 2, "body": f"old\n{pr_comment.MARKER}"}]]
    monkeypatch.setattr(pr_comment, "gh_api", lambda endpoint, *args, payload=None: pages)
    assert pr_comment.find_comment("o/r", "4")["id"] == 2


def test_outside_a_pull_request_only_the_summary_is_written(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    env = {**ENV, "GITHUB_STEP_SUMMARY": str(summary), "CHECKS_DIR": str(checks(tmp_path))}
    monkeypatch.setattr(pr_comment.os, "environ", env)
    monkeypatch.setattr(
        pr_comment, "gh_api", lambda *a, **k: pytest.fail("no API call outside a pull request")
    )
    monkeypatch.setattr(
        pr_comment, "changed_section", lambda env, limits, link_base: "No base commit to compare with."
    )
    assert pr_comment.main() == 0
    assert "No pull request in this event; summary only." in capsys.readouterr().out
    text = summary.read_text()
    assert text.startswith("🟢 **Checks:** pass.")
    assert "| complexity | " in text
    assert text.rstrip().endswith(pr_comment.MARKER)


def test_many_changed_functions_are_folded_and_a_huge_list_is_cut():
    block = "⚪ **`m.f`** — 2 of 10 decisions\n- **if** x → ↩ returns `1`  L2"
    assert pr_comment.fold("No function changed.") == "No function changed."
    two = pr_comment.fold("\n\n".join([block] * 2))
    assert two.startswith("<details open><summary>2 functions changed</summary>\n\n⚪")
    six = pr_comment.fold("\n\n".join([block] * 6))
    assert six.startswith("<details><summary>6 functions changed</summary>")
    cut = pr_comment.fold("\n\n".join([block] * 6), limit=len(block) * 3)
    assert cut.count("**`m.f`**") < 6
    assert "… cut here; the job summary has the full list." in cut
