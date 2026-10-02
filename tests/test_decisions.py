import json
import re
import subprocess
import sys
from pathlib import Path

from scripts import decisions

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "fixtures" / "decisions_sample.py"


def sample() -> dict[str, decisions.Function]:
    return {fn.name: fn for fn in decisions.functions(SAMPLE, SAMPLE.read_text())}


def test_every_function_counts_what_ruff_counts():
    paths = ["src", "scripts", "tests"]
    command = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--select",
        "C901",
        "--exit-zero",
        "--output-format",
        "json",
    ]
    command += ["--config", "lint.mccabe.max-complexity=1", *paths]
    findings = json.loads(
        subprocess.run(command, capture_output=True, text=True, cwd=ROOT, check=True).stdout
    )
    ruff = {
        (Path(f["filename"]).resolve(), f["location"]["row"]): int(
            re.search(r"\((\d+) > 1\)", f["message"])[1]
        )
        for f in findings
    }
    checked = 0
    for path in decisions.python_files([str(ROOT / p) for p in paths]):
        for fn in decisions.functions(path, path.read_text(), nested=True):
            assert fn.count == ruff.get((path.resolve(), fn.line), 1), f"{path}:{fn.line} {fn.name}"
            checked += 1
    assert checked > 150


def test_phrases_come_from_the_comment_and_outcomes_from_the_code():
    text = decisions.render(sample()["guarded"], 10)
    assert text.startswith("⚪ **`decisions_sample.guarded`** — 4 of 10 decisions · *Create a receipt once.*")
    assert "\n- **if** fewer than one worker was asked for → ✋ workers must be at least 1  L8\n" in text
    assert "\n- **if** a receipt already exists → ↩ reuse it  L10\n" in text
    assert "\n  - **if** the saved receipt is empty → ✋ {path} is empty  L12" in text


def test_loops_errors_and_helpers_read_without_a_comment():
    fns = sample()
    looping = decisions.render(fns["looping"], 10)
    assert "- **for each** file  L" in looping
    assert "  - **if** a blank entry → ↷ skips it  L" in looping
    assert "- **while** the rows run out before the files do  L" in looping
    attempts = decisions.render(fns["attempts"], 10)
    assert "- **on error** the call failed → ✋ exits 1  L" in attempts
    assert "- **if** that succeeded  L" in attempts
    nesting = decisions.render(fns["nesting"], 10)
    assert "- **helper** `helper(item)`  L" in nesting
    assert "- **for each** item  L" in nesting


def test_a_decision_without_a_phrase_shows_its_code_and_marks_the_function():
    fns = sample()
    branches = decisions.render(fns["branches"], 10)
    assert branches.startswith("🟡 **`decisions_sample.branches`** — 4 of 10 decisions, 1 without a phrase")
    assert '- **or if** `kind == "b"`  L' in branches
    assert "\n- otherwise  L" in branches
    assert "\n  - **if** anything else that is set  L" in branches
    matching = decisions.render(fns["matching"], 10)
    assert matching.startswith("⚪ **`decisions_sample.matching`** — 2 of 10 decisions")
    assert "- **case** a run was asked for → ↩ returns `1`  L" in matching
    assert "- otherwise → ↩ returns `0`  L" in matching
    method = decisions.render(fns["Holder.method"], 10)
    assert method.startswith(
        "🟡 **`decisions_sample.Holder.method`** — 2 of 10 decisions, 1 without a phrase"
    )


def test_links_and_the_limit_marks():
    fn = sample()["guarded"]
    text = decisions.render(fn, 4, "https://example.test/blob/abc/")
    assert "🟡 **`decisions_sample.guarded`** — 4 of 4 decisions" in text
    assert "[L8](https://example.test/blob/abc/tests/fixtures/decisions_sample.py#L8)" in text
    assert decisions.render(fn, 3).startswith("🔴")


def test_comparison_marks_added_and_removed_decisions(tmp_path):
    path = tmp_path / "m.py"
    old = decisions.functions(path, "def f(x):\n    if x < 0:  # negative\n        return 0\n    return x\n")[
        0
    ]
    new_text = "def f(x):\n    if x < 0:  # negative\n        return 0\n    if x > 9:  # too big\n        return 9\n    return x\n"
    new = decisions.functions(path, new_text)[0]
    text = decisions.render(new, 10, before=old)
    assert text.startswith("⚪ **`m.f`** — 2 → 3 of 10 decisions")
    assert "\n- **if** negative → ↩ returns `0`  L2\n- + **if** too big → ↩ returns `9`  L4" in text
    back = decisions.render(old, 10, before=new)
    assert back.endswith("- − **if** too big → ↩ returns `9`  (was L4)")
    assert decisions.render(new, 10, new=True).startswith("⚪ **`m.f`** — 3 of 10 decisions, new")


def test_limits_come_from_pyproject(tmp_path):
    assert decisions.limits(tmp_path / "none.toml")["decisions"] == 10
    (tmp_path / "pyproject.toml").write_text("[tool.ruff.lint.mccabe]\nmax-complexity = 7\n")
    assert decisions.limits(tmp_path / "pyproject.toml") == {"decisions": 7, "branches": 12, "statements": 50}


def test_cli_lists_a_file(capsys):
    assert decisions.main([str(SAMPLE), "--limit", "10"]) == 0
    out = capsys.readouterr().out
    assert "**`decisions_sample.guarded`**" in out
    assert "**`decisions_sample.Holder.method`**" in out
