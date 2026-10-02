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
            assert fn.complexity == ruff.get((path.resolve(), fn.line), 1), f"{path}:{fn.line} {fn.name}"
            checked += 1
    assert checked > 150


def test_phrases_come_from_the_comment_and_outcomes_from_the_code():
    text = decisions.render(sample()["guarded"], 9)
    assert text.startswith("⚪ **`decisions_sample.guarded`** — 3 of 9 decisions · *Create a receipt once.*")
    assert "\n- **if** fewer than one worker was asked for → ✋ workers must be at least 1  L8\n" in text
    assert "\n- **if** a receipt already exists → ↩ reuse it  L10\n" in text
    assert "\n  - **if** the saved receipt is empty → ✋ {path} is empty  L12" in text


def test_loops_errors_and_helpers_read_without_a_comment():
    fns = sample()
    looping = decisions.render(fns["looping"], 9)
    assert "- **for each** file  L" in looping
    assert "  - **if** a blank entry → ↷ skips it  L" in looping
    assert "- **while** the rows run out before the files do  L" in looping
    attempts = decisions.render(fns["attempts"], 9)
    assert "- **on error** the call failed → ✋ exits 1  L" in attempts
    assert "- **if** that succeeded  L" in attempts
    nesting = decisions.render(fns["nesting"], 9)
    assert "- **helper** `helper(item)`  L" in nesting
    assert "- **for each** item  L" in nesting


def test_a_decision_without_a_phrase_shows_its_code_and_marks_the_function():
    fns = sample()
    branches = decisions.render(fns["branches"], 9)
    assert branches.startswith("🟡 **`decisions_sample.branches`** — 3 of 9 decisions, 1 without a phrase")
    assert '- **or if** `kind == "b"`  L' in branches
    assert "\n- otherwise  L" in branches
    assert "\n  - **if** anything else that is set  L" in branches
    matching = decisions.render(fns["matching"], 9)
    assert matching.startswith("⚪ **`decisions_sample.matching`** — 1 of 9 decisions")
    assert "- **case** a run was asked for → ↩ returns `1`  L" in matching
    assert "- otherwise → ↩ returns `0`  L" in matching
    method = decisions.render(fns["Holder.method"], 9)
    assert method.startswith("🟡 **`decisions_sample.Holder.method`** — 1 of 9 decisions, 1 without a phrase")


def test_links_and_the_limit_marks():
    fn = sample()["guarded"]
    text = decisions.render(fn, 3, "https://example.test/blob/abc/")
    assert "🟡 **`decisions_sample.guarded`** — 3 of 3 decisions" in text
    assert "[L8](https://example.test/blob/abc/tests/fixtures/decisions_sample.py#L8)" in text
    assert decisions.render(fn, 2).startswith("🔴")


def test_the_header_says_when_the_count_moved_and_the_outline_is_the_current_state(tmp_path):
    path = tmp_path / "m.py"
    old = decisions.functions(path, "def f(x):\n    if x < 0:  # negative\n        return 0\n    return x\n")[
        0
    ]
    new_text = "def f(x):\n    if x < 0:  # negative\n        return 0\n    if x > 9:  # too big\n        return 9\n    return x\n"
    new = decisions.functions(path, new_text)[0]
    text = decisions.render(new, 9, before=old)
    assert text == (
        "⚪ **`m.f`** — 2 of 9 decisions (was 1)\n- **if** negative → ↩ returns `0`  L2\n- **if** too big → ↩ returns `9`  L4"
    )
    assert decisions.render(old, 9, before=new).endswith("(was 2)\n- **if** negative → ↩ returns `0`  L2")
    assert decisions.render(new, 9, new=True).startswith("⚪ **`m.f`** — 2 of 9 decisions, new")
    straight = decisions.functions(path, "def g():\n    return 1\n")[0]
    assert decisions.render(straight, 9, new=True) == "⚪ **`m.g`** — no decisions, new"


def test_limits_come_from_pyproject(tmp_path):
    assert decisions.limits(tmp_path / "none.toml")["decisions"] == 9
    (tmp_path / "pyproject.toml").write_text("[tool.ruff.lint.mccabe]\nmax-complexity = 7\n")
    assert decisions.limits(tmp_path / "pyproject.toml") == {
        "complexity": 7,
        "decisions": 6,
        "branches": 12,
        "statements": 50,
    }


def test_cli_lists_a_file(capsys):
    assert decisions.main([str(SAMPLE), "--limit", "9"]) == 0
    out = capsys.readouterr().out
    assert "**`decisions_sample.guarded`**" in out
    assert "**`decisions_sample.Holder.method`**" in out


APP = ROOT / "tests" / "fixtures" / "app"


def app_graph() -> decisions.CallGraph:
    return decisions.CallGraph(decisions.python_files([str(APP)]))


def test_the_call_graph_follows_the_entry_point_in_evaluation_order():
    graph = app_graph()
    assert graph.handlers("cli.main") == [("cli.fetch", "fetch"), ("cli.run", "run")]
    assert list(graph.reach("cli.main")) == [
        "cli.main",
        "cli.fetch",
        "work.download",
        "cli.run",
        "work.load",
        "work.parse",
        "work.finish",
    ]
    assert graph.reach("cli.main")["work.parse"] == ["cli.main", "cli.run", "work.load"]
    assert graph.edges("cli.run") == ["work.load", "work.finish"]


def test_a_package_is_listed_on_the_apps_path_grouped_by_subcommand():
    entries = [
        decisions.Entry(fn)
        for path in decisions.python_files([str(APP)])
        for fn in decisions.functions(path, path.read_text())
    ]
    text = decisions.Layout(app_graph(), "app", "cli.main").render(entries, 9)
    headings = [line for line in text.splitlines() if line.startswith("**")]
    assert headings == ["**`app`**", "**`app fetch`**", "**`app run`**", "**Not reached from `app`**"]
    labels = [line.split("`")[1] for line in text.splitlines() if line.startswith(("⚪", "🟡", "🔴"))]
    assert labels == [
        "cli.main",
        "cli.fetch",
        "work.download",
        "cli.run",
        "work.load",
        "work.parse",
        "work.finish",
        "work.unused",
    ]
    assert "⚪ **`work.download`** — 1 of 9 decisions · also under `app run`" in text
    assert "⚪ **`work.parse`** — 1 of 9 decisions · via `work.load`" in text
    assert "⚪ **`work.load`** — 2 of 9 decisions\n" in text


def test_one_file_is_listed_top_to_bottom_and_a_package_on_the_path(monkeypatch, capsys):
    monkeypatch.chdir(APP)
    monkeypatch.setattr(decisions, "entry_point", lambda pyproject=None: ("app", ["app", "cli"], "main"))
    assert decisions.main(["work.py", "--limit", "9"]) == 0
    file_order = [
        line.split("`")[1] for line in capsys.readouterr().out.splitlines() if line.startswith(("⚪", "🟡"))
    ]
    assert file_order == ["work.download", "work.parse", "work.load", "work.finish", "work.unused"]
    assert decisions.main([".", "--limit", "9"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("**`app`**\n\n⚪ **`cli.main`** — no decisions")
    assert "**Not reached from any entry point in `app`**\n\n⚪ **`work.unused`**" in out


def test_touched_functions_keep_their_whole_path_and_straight_line_code_is_left_out(tmp_path):
    path = tmp_path / "m.py"
    function = "def {name}({arg}):\n    if {arg}:  # set\n        return {value}\n    return 0\n"
    old = (
        function.format(name="same", arg="x", value=1)
        + "\n\n"
        + function.format(name="edited", arg="y", value=1)
    )
    new = (
        function.format(name="same", arg="x", value=1)
        + "\n\n"
        + function.format(name="edited", arg="y", value=2)
    )
    new += "\n\ndef added(z):\n    return z\n"
    before = {fn.name: fn for fn in decisions.functions(path, old)}
    entries = decisions.touched(before, decisions.functions(path, new))
    assert [(e.fn.name, e.new, e.before is not None) for e in entries] == [("edited", False, True)]
    assert decisions.render(entries[0].fn, 9, before=entries[0].before).endswith(
        "- **if** set → ↩ returns `2`  L8"
    )
    emptied = decisions.touched(
        before, decisions.functions(path, old.replace("    if y:  # set\n        return 1\n", ""))
    )
    assert [decisions.render(e.fn, 9, before=e.before) for e in emptied] == [
        "⚪ **`m.edited`** — no decisions (was 1)"
    ]


def test_scripts_are_grouped_by_their_main_and_the_rest_comes_last(monkeypatch):
    monkeypatch.setattr(decisions, "entry_point", lambda pyproject=None: None)
    tools = ROOT / "tests" / "fixtures" / "tools"
    entries = [
        decisions.Entry(fn) for path in decisions.python_files([str(tools)]) for fn in functions_of(path)
    ]
    text = decisions.arrange(entries, 9)
    assert text.split("\n\n")[:3] == [
        "**`python -m tests.fixtures.tools.one`**",
        "⚪ **`one.main`** — 1 of 9 decisions\n- **if** arguments were given → ↩ returns `two.count(argv)`  L7",
        "⚪ **`two.count`** — 2 of 9 decisions\n- **for each** item  L6\n  - **if** a real item  L7",
    ]
    assert "**Not reached from any entry point in `tests.fixtures.tools`**\n\n⚪ **`one.spare`**" in text


def functions_of(path):
    return decisions.functions(path, path.read_text())
