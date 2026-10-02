"""Every decision a function makes, in plain words.

A decision is what ruff's C901 counts: an `if` or `elif`, a loop, an `except`, a `match` case and a
nested function. The comment on its line (or on the line above) reads as the condition, and a guard's
`raise` message is its outcome. `python -m scripts.decisions src/s1cr/github.py` lists one file;
`--changed main` lists only the functions the branch touched. A package is listed in the order the
console script reaches its functions, grouped by subcommand; one file is listed top to bottom.
"""

import argparse
import ast
import io
import re
import subprocess
import tokenize
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

NEEDS_PHRASE = {"if", "or if", "while", "on error", "case"}
NOT_PHRASES = ("noqa", "type:", "pragma", "fmt:", "ruff:")
LEGEND = (
    "**if** a condition · **for each** a loop · **on error** an except · **helper** a nested function · "
    "✋ stops with that error · ↩ returns early · 🟡 near the limit, or a decision without a phrase"
)


@dataclass
class Decision:
    line: int
    depth: int
    kind: str
    code: str
    phrase: str | None = None
    outcome: str | None = None
    counted: bool = True

    @property
    def missing(self) -> bool:
        return self.counted and self.kind in NEEDS_PHRASE and not self.phrase

    def text(self) -> str:
        if self.kind == "otherwise":  # an else branch: not a decision, shown for the nesting
            return "otherwise" + (f" → {self.outcome}" if self.outcome else "")
        if self.kind == "helper":  # a nested function: its name says it
            return f"**helper** `{self.code}`"
        what = self.phrase or (self.code if self.kind == "for each" else f"`{self.code}`")
        return f"**{self.kind}** {what}" + (f" → {self.outcome}" if self.outcome else "")


@dataclass
class Function:
    path: Path
    name: str
    line: int
    doc: str
    decisions: list[Decision] = field(default_factory=list)
    text: str = ""

    @property
    def count(self) -> int:
        return sum(d.counted for d in self.decisions)

    @property
    def complexity(self) -> int:
        """What ruff's C901 reports: the decisions plus one for the function itself."""
        return self.count + 1

    @property
    def missing(self) -> int:
        return sum(d.missing for d in self.decisions)

    @property
    def label(self) -> str:
        return f"{self.path.stem}.{self.name}"


class Source:
    """One file's text, with its comments by line and source segments on demand."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.comments: dict[int, tuple[str, bool]] = {}
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            comment = token.string[1:].strip()
            if token.type == tokenize.COMMENT and not comment.startswith(NOT_PHRASES):  # written for people
                self.comments[token.start[0]] = (comment, token.line.strip().startswith("#"))

    def segment(self, node: ast.AST, limit: int = 70) -> str:
        text = " ".join((ast.get_source_segment(self.text, node) or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def phrase(self, first: int, last: int) -> str | None:
        """The trailing comment on a statement's header lines, else the comment line right above it."""
        for line in range(first, max(first, last) + 1):
            found = self.comments.get(line)
            if found and not found[1]:  # a trailing comment
                return found[0]
        above = self.comments.get(first - 1)
        return above[0] if above and above[1] else None


def message(src: Source, node: ast.expr) -> str | None:
    """The string an error is raised with: the author's own words for what went wrong."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):  # a plain string
        text = node.value
    elif isinstance(node, ast.JoinedStr):  # an f-string: keep the words, mark the values
        text = "".join(
            part.value if isinstance(part, ast.Constant) else "{" + src.segment(part.value, 25) + "}"
            for part in node.values
        )
    else:
        return None
    text = " ".join(text.split())
    return text if len(text) <= 95 else text[:94] + "…"


def raised(src: Source, node: ast.Raise) -> str:
    if node.exc is None:  # a bare raise
        return "re-raises"
    call = node.exc
    if isinstance(call, ast.Call) and call.args:  # an error built with arguments
        text = message(src, call.args[0])
        if text:  # the first argument is the message
            return text
        if src.segment(call.func) == "SystemExit":  # an exit status instead of a message
            return f"exits {src.segment(call.args[0], 20)}"
    return src.segment(call, 60)


def outcome(src: Source, body: list[ast.stmt]) -> str | None:
    """What a branch does, from its last statement."""
    last = body[-1] if body else None
    if isinstance(last, ast.Raise):  # the branch stops the function
        return "✋ " + raised(src, last)
    if isinstance(last, ast.Return):  # the branch leaves the function early
        return "↩ returns" + (f" `{src.segment(last.value, 45)}`" if last.value else "")
    if isinstance(last, ast.Continue):  # the branch skips the rest of the loop body
        return "↷ skips it"
    if isinstance(last, ast.Break):  # the branch ends the loop
        return "⏹ stops the loop"
    return None


def catches_all(pattern: ast.pattern) -> bool:
    """A pattern nothing can fail to match: `_`, a bare name, or an alternative that is one."""
    if isinstance(pattern, ast.MatchAs):  # `_` or `case name`, unless it narrows a sub-pattern
        return pattern.pattern is None
    if isinstance(pattern, ast.MatchOr):  # one catch-all alternative is enough
        return any(catches_all(alternative) for alternative in pattern.patterns)
    return False


class Walker:
    """Collects the decisions of one function body, in order, the way ruff's C901 counts them."""

    def __init__(self, src: Source) -> None:
        self.src = src
        self.out: list[Decision] = []
        self.visit: dict[type, Callable[[ast.AST, int], None]] = {
            ast.If: self.branch,
            ast.For: self.loop,
            ast.AsyncFor: self.loop,
            ast.While: self.loop,
            ast.Try: self.attempt,
            ast.TryStar: self.attempt,
            ast.Match: self.match,
            ast.With: self.block,
            ast.AsyncWith: self.block,
            ast.ClassDef: self.block,
            ast.FunctionDef: self.helper,
            ast.AsyncFunctionDef: self.helper,
        }

    def walk(self, body: list[ast.stmt], depth: int) -> None:
        for statement in body:
            visit = self.visit.get(type(statement))
            if visit:  # a statement that can hold decisions
                visit(statement, depth)

    def decision(
        self, line: int, header_end: int, depth: int, kind: str, code: str, what: str | None = None
    ) -> Decision:
        phrase = self.src.phrase(line, header_end)
        if phrase and re.search(r"->|→", phrase):  # the comment names the outcome too
            phrase, named = re.split(r"\s*(?:->|→)\s*", phrase, maxsplit=1)
            what = f"{what[0]} {named}" if what else named
        found = Decision(line, depth, kind, code, phrase or None, what)
        self.out.append(found)
        return found

    def branch(self, node: ast.If, depth: int, kind: str = "if") -> None:
        what = outcome(self.src, node.body)
        self.decision(node.lineno, node.body[0].lineno - 1, depth, kind, self.src.segment(node.test), what)
        self.walk(node.body[:-1] if what else node.body, depth + 1)
        orelse = node.orelse
        if len(orelse) == 1 and isinstance(orelse[0], ast.If) and orelse[0].col_offset == node.col_offset:
            self.branch(orelse[0], depth, "or if")  # an elif
        elif orelse:  # an else
            what = outcome(self.src, orelse)
            self.out.append(Decision(orelse[0].lineno, depth, "otherwise", "", outcome=what, counted=False))
            self.walk(orelse[:-1] if what else orelse, depth + 1)

    def loop(self, node: ast.For | ast.AsyncFor | ast.While, depth: int) -> None:
        header_end = node.body[0].lineno - 1
        if isinstance(node, ast.While):  # a while reads by its condition
            self.decision(node.lineno, header_end, depth, "while", self.src.segment(node.test))
        else:
            self.decision(node.lineno, header_end, depth, "for each", self.src.segment(node.target, 30))
        self.walk(node.body, depth + 1)
        self.walk(node.orelse, depth + 1)

    def attempt(self, node: ast.Try | ast.TryStar, depth: int) -> None:
        self.walk(node.body, depth)
        if node.orelse:  # the try's else runs when nothing was raised
            self.out.append(Decision(node.orelse[0].lineno, depth, "if", "that succeeded", "that succeeded"))
            self.walk(node.orelse, depth + 1)
        self.walk(node.finalbody, depth)
        for handler in node.handlers:
            kinds = self.src.segment(handler.type, 50).strip("()") if handler.type else "any error"
            what = outcome(self.src, handler.body)
            self.decision(handler.lineno, handler.body[0].lineno - 1, depth, "on error", kinds, what)
            self.walk(handler.body[:-1] if what else handler.body, depth + 1)

    def match(self, node: ast.Match, depth: int) -> None:
        for case in node.cases:
            what = outcome(self.src, case.body)
            line = case.pattern.lineno
            if (
                case is node.cases[-1] and case.guard is None and catches_all(case.pattern)
            ):  # an else, in effect
                self.out.append(Decision(line, depth, "otherwise", "", outcome=what, counted=False))
            else:
                self.decision(
                    line, case.body[0].lineno - 1, depth, "case", self.src.segment(case.pattern), what
                )
            self.walk(case.body[:-1] if what else case.body, depth + 1)

    def block(self, node: ast.With | ast.AsyncWith | ast.ClassDef, depth: int) -> None:
        self.walk(node.body, depth)

    def helper(self, node: ast.FunctionDef | ast.AsyncFunctionDef, depth: int) -> None:
        arguments = ", ".join(argument.arg for argument in node.args.args)
        self.out.append(Decision(node.lineno, depth, "helper", f"{node.name}({arguments})"))
        self.walk(node.body, depth + 1)


def named_functions(body: list[ast.stmt], prefix: str = "") -> list[tuple[ast.AST, str]]:
    """Module-level functions and methods, with methods named after their class."""
    found = []
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):  # a function or method
            found.append((node, prefix + node.name))
        elif isinstance(node, ast.ClassDef):  # a class: its methods are listed
            found.extend(named_functions(node.body, prefix + node.name + "."))
    return found


def functions(path: Path, text: str, *, nested: bool = False) -> list[Function]:
    """The functions of one file with their decisions; `nested` also lists inner functions on their own."""
    src = Source(text)
    tree = ast.parse(text)
    if nested:  # every function, inner ones included, the way ruff reports them
        nodes = [(n, n.name) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    else:
        nodes = named_functions(tree.body)
    found = []
    for node, name in nodes:
        walker = Walker(src)
        walker.walk(node.body, 0)
        doc = (ast.get_docstring(node) or "").split("\n")[0]
        found.append(
            Function(path, name, node.lineno, doc, walker.out, ast.get_source_segment(text, node) or "")
        )
    return sorted(found, key=lambda fn: fn.line)


def mark(fn: Function, limit: int) -> str:
    if fn.count > limit:  # ruff fails this function
        return "🔴"
    if fn.count >= limit - 2 or fn.missing:  # close to the limit, or not fully phrased
        return "🟡"
    return "⚪"


def link(path: Path, line: int, link_base: str) -> str:
    if not link_base:  # a bare line number
        return f"L{line}"
    shown = path.resolve()
    if shown.is_relative_to(Path.cwd()):  # a path under the working directory links by its relative path
        shown = shown.relative_to(Path.cwd())
    return f"[L{line}]({link_base}{shown.as_posix()}#L{line})"


def render(
    fn: Function,
    limit: int,
    link_base: str = "",
    *,
    before: Function | None = None,
    new: bool = False,
    note: str | None = None,
) -> str:
    """One function as a markdown outline: a header line, then one bullet per decision, as it is now."""
    counts = f"{fn.count} of {limit} decisions" if fn.count else "no decisions"
    if before is not None and before.count != fn.count:  # the count moved since the base revision
        counts += f" (was {before.count})"
    header = f"{mark(fn, limit)} **`{fn.label}`** — {counts}"
    if new:  # the function did not exist at the base revision
        header += ", new"
    if fn.missing:  # some conditions still read as code
        header += f", {fn.missing} without a phrase"
    if note:  # where the function sits on the app's path
        header += f" · {note}"
    if fn.doc:  # the docstring's first line says what the function is for
        header += f" · *{fn.doc}*"
    lines = [header]
    lines.extend(f"{'  ' * d.depth}- {d.text()}  {link(fn.path, d.line, link_base)}" for d in fn.decisions)
    return "\n".join(lines)


def python_files(paths: list[str]) -> list[Path]:
    found = []
    for path in map(Path, paths):
        found.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return found


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else ""


@dataclass
class Entry:
    fn: Function
    before: Function | None = None
    new: bool = False


def entry_point(pyproject: Path = Path("pyproject.toml")) -> tuple[str, str] | None:
    """The console script and the function it starts, from `[project.scripts]`: (`s1cr`, `cli.main`)."""
    if not pyproject.exists():  # no project file
        return None
    scripts = tomllib.loads(pyproject.read_text()).get("project", {}).get("scripts", {})
    for script, target in scripts.items():  # the first script is the app
        module, _, function = target.partition(":")
        return script, f"{module.rsplit('.', 1)[-1]}.{function}"
    return None


def import_map(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Local names from relative imports: `from . import github` and `from .report import write_report`."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:  # a relative import, anywhere in the module
            for alias in node.names:
                kind, target = (
                    ("module", alias.name) if node.module is None else ("name", f"{node.module}.{alias.name}")
                )
                names[alias.asname or alias.name] = (kind, target)
    return names


def subcommand_parsers(node: ast.AST) -> dict[str, str]:
    """`x = commands.add_parser("name", ...)` assignments: the variable and the subcommand it parses."""
    parsers = {}
    for sub in ast.walk(node):
        call = sub.value if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call) else None
        if (
            not call
            or not isinstance(call.func, ast.Attribute)
            or call.func.attr != "add_parser"
            or not call.args
        ):
            continue  # not a parser being made
        if (
            isinstance(call.args[0], ast.Constant)
            and len(sub.targets) == 1
            and isinstance(sub.targets[0], ast.Name)
        ):
            parsers[sub.targets[0].id] = call.args[0].value
    return parsers


def set_defaults_calls(node: ast.AST) -> list[ast.Call]:
    """Every `x.set_defaults(...)` call under `node`, in source order."""
    calls = [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "set_defaults"
    ]
    return sorted(calls, key=lambda call: (call.lineno, call.col_offset))


class CallGraph:
    """Which functions refer to which, resolved through relative imports, in evaluation order."""

    def __init__(self, paths: list[Path]) -> None:
        self.nodes: dict[str, ast.AST] = {}
        self.targets: dict[str, list[str]] = {}
        self.imports: dict[str, dict[str, tuple[str, str]]] = {}
        for path in paths:
            if path.stem == "__init__":  # a package marker, not a module
                continue
            tree = ast.parse(path.read_text())
            self.imports[path.stem] = import_map(tree)
            for node in tree.body:
                self.index(path.stem, node)

    def index(self, module: str, node: ast.stmt, prefix: str = "") -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):  # a function or method
            label = f"{module}.{prefix}{node.name}"
            self.nodes[label] = node
            self.targets[label] = [label]
        elif isinstance(node, ast.ClassDef):  # a class stands for its methods, in order
            methods = [sub for sub in node.body if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef)]
            self.targets[f"{module}.{node.name}"] = [f"{module}.{node.name}.{sub.name}" for sub in methods]
            for method in methods:
                self.index(module, method, f"{node.name}.")

    def resolve(self, module: str, node: ast.AST) -> list[str]:
        """The functions a name or `module.name` stands for, if any."""
        local = self.imports[module]
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):  # `module.name`
            found = local.get(node.value.id)
            return self.targets.get(f"{found[1]}.{node.attr}", []) if found and found[0] == "module" else []
        if isinstance(node, ast.Name):  # a bare name: imported, or from this module
            found = local.get(node.id)
            if found and found[0] == "name":  # imported with `from .module import name`
                return self.targets.get(found[1], [])
            return self.targets.get(f"{module}.{node.id}", [])
        return []

    def references(self, module: str, node: ast.AST) -> Iterator[str]:
        """Functions a node refers to, in evaluation order: a call's arguments before the call itself."""
        if isinstance(node, ast.Call):  # arguments run first
            for child in [*node.args, *node.keywords]:
                yield from self.references(module, child)
            yield from self.references(module, node.func)
            return
        if isinstance(node, ast.AnnAssign):  # an annotation is not run
            if node.value:  # the assigned value is
                yield from self.references(module, node.value)
            return
        yield from self.resolve(module, node)
        for child in ast.iter_child_nodes(node):
            yield from self.references(module, child)

    def edges(self, label: str) -> list[str]:
        """The functions one function refers to, first reference first, each once."""
        module = label.split(".")[0]
        ordered: list[str] = []
        for statement in self.nodes[label].body:
            for target in self.references(module, statement):
                if target != label and target not in ordered:  # a new target
                    ordered.append(target)
        return ordered

    def reach(self, start: str) -> dict[str, list[str]]:
        """Every function reached from `start`, depth first, with the chain that reached it."""
        reached: dict[str, list[str]] = {}

        def visit(label: str, chain: list[str]) -> None:
            if label in reached or label not in self.nodes:  # seen already, or not a function here
                return
            reached[label] = chain
            for target in self.edges(label):
                visit(target, [*chain, label])

        visit(start, [])
        return reached

    def handlers(self, entry: str) -> list[tuple[str, str]]:
        """The functions the entry hands subcommands to, in registration order, with each subcommand's name."""
        module, node = entry.split(".")[0], self.nodes.get(entry)
        if node is None:  # the entry is not in these files
            return []
        parsers = subcommand_parsers(node)
        found = []
        for call in set_defaults_calls(node):
            receiver = call.func.value.id if isinstance(call.func.value, ast.Name) else ""
            for keyword in call.keywords:
                if keyword.arg == "func":  # the handler
                    found.extend(
                        (t, parsers.get(receiver, t.split(".")[-1]))
                        for t in self.resolve(module, keyword.value)
                    )
        return found or [(target, target.split(".")[-1]) for target in self.edges(entry)]


class Layout:
    """Entries in the order the app reaches them, grouped by subcommand, the unreached last."""

    def __init__(self, graph: CallGraph, script: str, entry: str) -> None:
        self.script, self.entry = script, entry
        self.reached = graph.reach(entry)
        self.handlers = graph.handlers(entry)
        self.names = dict(self.handlers)
        self.under = {label: graph.reach(label) for label, _ in self.handlers}
        self.position = {label: index for index, label in enumerate(self.reached)}
        self.groups: list[str | None] = ["", *self.names.values(), None]

    def group(self, label: str) -> str | None:
        chain = self.reached.get(label)
        if chain is None:  # never reached from the entry
            return None
        if label in self.names:  # a handler heads its own group
            return self.names[label]
        handler = chain[1] if len(chain) > 1 else self.entry
        return self.names.get(handler, "")

    def note(self, label: str, group: str | None) -> str | None:
        chain = self.reached.get(label) or []
        heads = {self.entry, *(h for h, name in self.handlers if name == group)}
        parts = []
        if chain and chain[-1] not in heads:  # the caller is not the group's own handler
            parts.append(f"via `{chain[-1]}`")
        also = [name for h, name in self.handlers if name != group and label in self.under[h]]
        if also:  # other subcommands reach it too
            parts.append("also under " + ", ".join(f"`{self.script} {name}`" for name in also))
        return " · ".join(parts) or None

    def heading(self, group: str | None) -> str:
        if group is None:  # the leftovers
            return f"**Not reached from `{self.script}`**"
        return f"**`{self.script}`**" if group == "" else f"**`{self.script} {group}`**"

    def key(self, item: Entry) -> tuple:
        label = item.fn.label
        return (
            self.groups.index(self.group(label)),
            self.position.get(label, 0),
            str(item.fn.path),
            item.fn.line,
        )

    def render(self, entries: list[Entry], limit: int, link_base: str = "") -> str:
        sections: dict[str | None, list[str]] = {}
        for item in sorted(entries, key=self.key):
            group = self.group(item.fn.label)
            note = self.note(item.fn.label, group)
            block = render(item.fn, limit, link_base, before=item.before, new=item.new, note=note)
            sections.setdefault(group, []).append(block)
        return "\n\n".join(
            self.heading(g) + "\n\n" + "\n\n".join(sections[g]) for g in self.groups if g in sections
        )


def arrange(
    entries: list[Entry], paths: list[str], limit: int, link_base: str, app: tuple[str, str] | None
) -> str:
    """Entries on the app's path when there is an app, else in file order."""
    if app is None:  # no console script to follow
        return "\n\n".join(render(e.fn, limit, link_base, before=e.before, new=e.new) for e in entries)
    roots = sorted({str(path.parent) for path in python_files(paths)})
    return Layout(CallGraph(python_files(roots)), *app).render(entries, limit, link_base)


def report(paths: list[str], limit: int, link_base: str = "", *, order: str = "path") -> str:
    entries = [Entry(fn) for path in python_files(paths) for fn in functions(path, path.read_text())]
    if order == "file":  # top to bottom, as in the editor
        return "\n\n".join(render(e.fn, limit, link_base) for e in entries)
    return arrange(entries, paths, limit, link_base, entry_point())


def touched(old: dict[str, Function], new: list[Function]) -> list[Entry]:
    """The functions whose source differs, leaving out straight-line code that stayed straight."""
    found = []
    for fn in new:
        before = old.get(fn.name)
        if not fn.count and not (before and before.count):  # straight-line code: nothing to glance at
            continue
        if before is None:  # the function is new
            found.append(Entry(fn, new=True))
        elif before.text != fn.text:  # the function was edited
            found.append(Entry(fn, before))
    return found


def changed_report(base: str, paths: list[str], limit: int, link_base: str = "") -> str:
    """The functions edited since `base`, each with its whole decision path, on the app's path."""
    entries, removed = [], []
    for path in map(Path, git("diff", "--name-only", base, "--", *paths).splitlines()):
        if path.suffix != ".py":  # not Python
            continue
        old = {fn.name: fn for fn in functions(path, git("show", f"{base}:{path.as_posix()}"))}
        new = functions(path, path.read_text()) if path.exists() else []
        entries.extend(touched(old, new))
        names = {fn.name for fn in new}
        removed.extend(
            f"− **`{fn.label}`** removed ({fn.count} decisions)" for n, fn in old.items() if n not in names
        )
    if not entries and not removed:  # nothing to show
        return "No function changed."
    body = arrange(entries, paths, limit, link_base, entry_point()) if entries else ""
    return "\n\n".join(part for part in (body, "\n".join(removed)) if part)


def limits(pyproject: Path = Path("pyproject.toml")) -> dict[str, int]:
    """The limits ruff enforces, from pyproject.toml; decisions are mccabe's complexity minus one."""
    lint: dict = {}
    if pyproject.exists():  # the project configures ruff
        lint = tomllib.loads(pyproject.read_text()).get("tool", {}).get("ruff", {}).get("lint", {})
    complexity = lint.get("mccabe", {}).get("max-complexity", 10)
    return {
        "complexity": complexity,
        "decisions": complexity - 1,
        "branches": lint.get("pylint", {}).get("max-branches", 12),
        "statements": lint.get("pylint", {}).get("max-statements", 50),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["src"], help="files or directories (default: src)")
    parser.add_argument(
        "--changed", metavar="BASE", help="only functions whose decisions differ from this revision"
    )
    parser.add_argument(
        "--limit", type=int, default=limits()["decisions"], help="most decisions a function may have"
    )
    parser.add_argument("--link-base", default="", help="URL prefix that turns line numbers into links")
    parser.add_argument(
        "--order", choices=("path", "file"), help="the app's path (default for a package) or file order"
    )
    args = parser.parse_args(argv)
    if args.changed:  # compare with a Git revision
        print(changed_report(args.changed, args.paths, args.limit, args.link_base))
    else:
        single = len(args.paths) == 1 and Path(args.paths[0]).is_file()
        print(
            report(args.paths, args.limit, args.link_base, order=args.order or ("file" if single else "path"))
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
