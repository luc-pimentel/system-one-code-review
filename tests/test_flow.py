import ast
import shutil
from pathlib import Path

from scripts import flow

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "tests" / "fixtures" / "flow_app"


def commands(app: flow.App | None) -> dict[str, flow.Command]:
    assert app is not None
    return {command.name: command for command in app.commands}


def touches(command: flow.Command) -> set[tuple[str, str, bool]]:
    return {(e.verb, e.target, e.named) for e in command.effects()}


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new))


def test_each_command_names_what_it_touches_through_the_catalog():
    found = commands(flow.build(DEMO))
    assert list(found) == ["demo score", "demo show"]
    assert found["demo score"].about == "score every item"
    assert touches(found["demo score"]) == {
        ("reads", "data/items.jsonl", True),  # through the parameter's default
        ("calls", "api", True),  # a request to a catalog address
        ("appends", "<run>/results.jsonl", True),  # through a catalog function
        ("calls", "tool", True),  # a program the catalog names
        ("env", "$DEMO_TOKEN", True),
    }
    assert touches(found["demo show"]) == {("reads", "path", False)}


def test_steps_carry_their_data_their_callers_and_their_rules():
    steps = {s.label: s for s in commands(flow.build(DEMO))["demo score"].steps}
    assert (steps["files.load"].caller, steps["files.load"].depth) == ("cli.score", 1)
    assert steps["files.load"].shape == "→ list[Item]"
    assert steps["files.ask"].shape == "(Item) → Score"
    assert steps["files.save"].shape == "(list[Score])"
    assert steps["files.load"].rules == ["✋ nothing was kept yet"]
    assert steps["cli.score"].rules == ["✋ nothing to score"]


def test_the_whole_app_reads_as_a_map_a_catalog_lineages_rules_and_data():
    text = flow.whole(DEMO)
    assert '  f_items[("data/items.jsonl<br/><i>Item</i>")]' in text  # a file with the type it holds
    assert "  f_items --> c_score\n  s_api -.- c_score\n  c_score --> f_results" in text
    assert '  u_path[("🟡 path")]\n  t_out(["terminal"])' in text  # unnamed, and printed
    assert "|  | `stores.results` | `<run>/results.jsonl` | file | One Score per line | `Score` |" in text
    assert "|  | `stores.DATA` | `data` | folder | everything demo keeps |  |" in text
    assert (
        '  f_items -->|"load"| t_Item\n  t_Item -->|"ask"| s_api\n'
        '  s_api -->|"ask"| t_Score\n  t_Score -->|"save"| f_results'
    ) in text
    assert "No named data moves through this command." in text
    assert "<b>Rules</b> — 2 refusals and early returns in 2 functions, each once" in text
    assert (
        "- `files.load` · `→ list[Item]` · reads `data/items.jsonl` · in `demo score`\n  - ✋ nothing was kept yet"
        in text
    )
    assert "|  | `size` | `int` | 🟡 no comment |" in text


def test_a_branch_shows_what_it_changed_and_what_it_leaves_unnamed(tmp_path):
    base, head = tmp_path / "base", tmp_path / "head"
    shutil.copytree(DEMO, base)
    shutil.copytree(DEMO, head)
    edit(
        head / "src" / "demo" / "files.py",
        "    output = stores.results(run)\n",
        '    if not scores:  # nothing to save\n        raise ValueError("no scores")\n'
        '    output = stores.results(run)\n    stores.LOG.write_text("saved")\n',
    )
    edit(
        head / "src" / "demo" / "files.py",
        '"""The tool\'s version."""',
        '"""The version the tool reports."""',
    )
    edit(
        head / "src" / "demo" / "stores.py",
        'TOOL = "tool"',
        'LOG = DATA / "log.txt"  # what demo did last\nTOOL = "tool"',
    )
    edit(
        head / "src" / "demo" / "models.py",
        "    value: float  # how good it is\n",
        "    value: float  # how good it is\n    note: str\n",
    )
    text, gaps = flow.changed(base, head, folders=["src"])
    assert "- **Map:** `demo score` now writes `data/log.txt`; new in the catalog: `stores.LOG`." in text
    assert "- **Data:** `models.Score` (+`note`)." in text
    assert "- **Lineage:** changed in `demo score`, drawn below." in text
    assert "- **Rules:** 1 new, listed below." in text
    # the docstring edit is counted apart from the code change
    assert "- **Code:** changed in 1 function (`files.save`); only docstrings or comments in 1." in text
    assert "  class f_log new\n  linkStyle 2 stroke:#2da44e" in text  # the map marks the new file
    assert "  class t_Score changed\n" in text  # the lineage marks the reshaped type
    assert "<details open><summary><b><code>demo score</code></b> — score every item</summary>" in text
    assert "<b><code>demo show</code></b>" not in text  # an untouched lineage stays out
    assert "| ➕ | `stores.LOG` | `data/log.txt` | file | what demo did last |  |" in text
    assert "<b>Rules</b> — 1 new</summary>" in text
    assert "  - ➕ ✋ nothing to save" in text
    assert "✏️ <b><code>models.Score</code></b> TypedDict" in text
    assert "| ➕ | `note` | `str` | 🟡 no comment |" in text
    assert "models.Item" not in text  # unchanged data stays out
    assert "- `models.Score.note` has no comment saying what it holds" in text
    assert gaps == 1


def test_a_branch_that_misses_the_app_says_so(tmp_path):
    text, gaps = flow.changed(DEMO, DEMO, folders=["src"])
    for line in ("Map", "Data", "Lineage", "Rules"):
        assert f"- **{line}:** unchanged." in text
    assert "- **Code:** unchanged." in text
    assert "classDef" not in text
    assert "Outlined" not in text  # no legend when nothing is marked
    assert gaps == 0


def test_a_service_no_longer_called_is_drawn_dashed_and_said(tmp_path):
    base, head = tmp_path / "base", tmp_path / "head"
    shutil.copytree(DEMO, base)
    shutil.copytree(DEMO, head)
    edit(head / "src" / "demo" / "cli.py", "    print(files.version(), files.token())\n", "")
    text, _ = flow.changed(base, head, folders=["src"])
    assert "`demo score` no longer calls `tool`" in text
    assert "  linkStyle 5 stroke:#cf222e,stroke-width:2px,stroke-dasharray:5 5" in text
    assert "class s_tool removed" in text


def test_new_or_edited_functions_say_what_they_do_and_name_their_data():
    before = "def kept(x: dict) -> None:\n    pass\n"
    after = before + "\n\ndef added(x: dict[str, Any]) -> list[dict]:\n    return []\n"
    assert flow.file_gaps(Path("m.py"), after, before) == [
        flow.Gap("m.added", "has no docstring"),
        flow.Gap("m.added", "passes `dict[str, Any]`, which names no fields"),
        flow.Gap("m.added", "passes `list[dict]`, which names no fields"),
    ]


def test_plain_containers_are_told_apart_from_named_data():
    def plain(text: str) -> bool:
        return flow.plain(ast.parse(text, mode="eval").body)

    assert all(plain(text) for text in ("dict", "list[dict]", "dict[str, Any]", "set[tuple]"))
    assert not any(plain(text) for text in ("dict[str, float]", "list[Row]", "Receipt | None", "Any"))


def test_the_app_names_every_file_and_service_it_touches():
    found = commands(flow.build(ROOT))
    unnamed = [
        (c.name, s.label, e) for c in found.values() for s in c.steps for e in s.effects if not e.named
    ]
    assert unnamed == []
    run = {(e.verb, e.target) for e in found["s1cr run"].effects()}
    assert {
        ("reads", "data/rows.jsonl"),
        ("env", "$TYPESAFE_API_KEY"),
        ("appends", "<run>/answers.jsonl"),
        ("writes", "<run>/run.json"),
        ("calls", "jev"),
        ("calls", "git"),
    } <= run
    build = {(e.verb, e.target) for e in found["s1cr build"].effects()}
    assert {("reads", "data/compare"), ("writes", "data/compare"), ("calls", "github api")} <= build
    labels = {s.label for s in found["s1cr run"].steps}
    assert "swrbench.Row.from_json" in labels
    assert "swrbench.Row.era" not in labels  # `Class.method` reaches that method alone
    assert "swrbench.Row.review_input" in labels  # `row.review_input()` on a name typed Row
    building = {s.label for s in found["s1cr build"].steps}
    assert "swrbench.Row.to_json" not in building  # building a Row runs none of its methods


def test_each_command_draws_its_lineage_type_by_type():
    app = flow.build(ROOT)
    assert app is not None
    run = flow.mermaid.render(flow.Lineage(app, commands(app)["s1cr run"]).chart())
    for line in (
        'f_rows -->|"load_rows"| t_Row',
        't_Row -->|"review_input"| t_ReviewInput',
        't_JevRequest -->|"call"| s_jev',
        's_jev -->|"call"| t_JevResponse',
        't_JevResponse -->|"review"| t_ReviewResult',
        't_ReviewResult -->|"ask"| t_Attempt',  # a function nested in jev.run
        't_Attempt -->|"run"| f_answers',
        's_git -->|"git_snapshot"| t_Snapshot',
        't_Snapshot -->|"prepare"| t_Receipt',
    ):
        assert f"  {line}\n" in run, line
    assert 't_ReviewInput -->|"request"| t_JevRequest' not in run  # drawn through State and Question
    review = flow.mermaid.render(flow.Lineage(app, commands(app)["s1cr review"]).chart())
    assert '  s_gh -->|"_api"| t_GitHubPull\n' in review  # an untyped call's result, named where it is kept
    assert '  t_PullRequest -.->|".input"| t_ReviewInput\n' in review  # data taken out of its holder


def test_the_readme_shows_the_current_map_and_lineages():
    text = (ROOT / "README.md").read_text()
    assert flow.with_readme_part(text, flow.readme_part(ROOT)) == text, (
        "README.md is out of date: run `uv run python -m scripts.flow --readme`"
    )


def test_a_change_is_told_by_what_it_touched():
    def code(source: str) -> dict[str, flow.Code]:
        return flow.codes(Path("m.py"), source)

    before = code('def f(x: int) -> int:\n    """Double it."""\n    return x * 2\n')
    words = code('def f(x: int) -> int:\n    """Twice x."""\n    return x * 2  # doubled\n')
    types = code('def f(x: float) -> float:\n    """Double it."""\n    return x * 2\n')
    logic = code('def f(x: int) -> int:\n    """Double it."""\n    return x * 3\n')
    assert flow.classify(before, words).words == ["m.f"]
    assert flow.classify(before, types).types == ["m.f"]
    assert flow.classify(before, logic).logic == ["m.f"]
    found = flow.classify(before, code("def g() -> None:\n    pass\n"))
    assert (found.new, found.removed) == (["m.g"], ["m.f"])


def test_the_base_revision_is_read_without_touching_the_working_tree(tmp_path):
    folder = flow.checkout("HEAD", tmp_path)
    assert (folder / "pyproject.toml").exists()
    assert (folder / "src" / "s1cr" / "cli.py").exists()
    assert flow.checkout("no-such-revision", tmp_path / "none") == tmp_path / "none"
