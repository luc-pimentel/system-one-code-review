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


def test_the_whole_app_reads_as_a_map_a_catalog_steps_and_data():
    text = flow.whole(DEMO)
    assert (
        "|  | `demo score` | `data/items.jsonl`, `$DEMO_TOKEN` | `api`, `tool` | `<run>/results.jsonl` |"
        in text
    )
    assert "|  | `demo show` | 🟡 `path` |  | stdout |" in text
    assert "|  | `stores.results` | `<run>/results.jsonl` | file | One Score per line |" in text
    assert "|  | `stores.DATA` | `data` | folder | everything demo keeps |" in text
    assert (
        "  - `files.load` · `→ list[Item]` · reads `data/items.jsonl`\n    - ✋ nothing was kept yet" in text
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
    assert (
        "| ✏️ | `demo score` | `data/items.jsonl`, `$DEMO_TOKEN` | `api`, `tool` | ➕ `data/log.txt`, `<run>/results.jsonl` |"
        in text
    )
    assert "|  | `demo show` |" in text
    assert "| ➕ | `stores.LOG` | `data/log.txt` | file | what demo did last |" in text
    assert "✏️ <b><code>models.Score</code></b> TypedDict" in text
    assert "| ➕ | `note` | `str` | 🟡 no comment |" in text
    assert "models.Item" not in text  # unchanged data stays out
    assert (
        "<details open><summary>✏️ <b><code>demo score</code></b> — score every item · steps 1 changed</summary>"
        in text
    )
    assert (
        "  - ✏️ `files.save` · `(list[Score])` · ➕ writes `data/log.txt`, appends `<run>/results.jsonl`"
        in text
    )
    assert "    - ➕ ✋ nothing to save" in text
    assert "- `models.Score.note` has no comment saying what it holds" in text
    assert gaps == 1


def test_a_branch_that_misses_the_app_says_so(tmp_path):
    text, gaps = flow.changed(DEMO, DEMO, folders=["src"])
    assert "This branch changes nothing on `demo`'s path." in text
    assert "✏️" not in text
    assert gaps == 0


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


def test_the_base_revision_is_read_without_touching_the_working_tree(tmp_path):
    folder = flow.checkout("HEAD", tmp_path)
    assert (folder / "pyproject.toml").exists()
    assert (folder / "src" / "s1cr" / "cli.py").exists()
    assert flow.checkout("no-such-revision", tmp_path / "none") == tmp_path / "none"
