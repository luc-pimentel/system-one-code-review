"""Every decision a function makes, in plain words.

A decision is what ruff's C901 counts: an `if` or `elif`, a loop, an `except`, a `match` case and a
nested function. The comment on its line (or on the line above) reads as the condition, and a guard's
`raise` message is its outcome. `python -m scripts.decisions src/s1cr/github.py` lists one file;
`--changed main` lists only the functions whose decisions differ from that Git revision.
"""

import argparse
import ast
import io
import re
import subprocess
import tokenize
import tomllib
from collections import Counter
from collections.abc import Callable
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

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.kind, self.phrase or self.code, self.outcome)

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

    @property
    def count(self) -> int:
        return 1 + sum(d.counted for d in self.decisions)

    @property
    def missing(self) -> int:
        return sum(d.missing for d in self.decisions)

    @property
    def label(self) -> str:
        return f"{self.path.stem}.{self.name}"

    @property
    def keys(self) -> Counter:
        return Counter(d.key for d in self.decisions if d.counted)


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
        found.append(Function(path, name, node.lineno, doc, walker.out))
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
    fn: Function, limit: int, link_base: str = "", *, before: Function | None = None, new: bool = False
) -> str:
    """One function as a markdown outline: a header line, then one bullet per decision."""
    counts = f"{fn.count} of {limit} decisions"
    if before is not None and before.count != fn.count:  # the count moved
        counts = f"{before.count} → {fn.count} of {limit} decisions"
    header = f"{mark(fn, limit)} **`{fn.label}`** — {counts}"
    if new:  # the function did not exist at the base revision
        header += ", new"
    if fn.missing:  # some conditions still read as code
        header += f", {fn.missing} without a phrase"
    if fn.doc:  # the docstring's first line says what the function is for
        header += f" · *{fn.doc}*"
    added = fn.keys - before.keys if before else Counter()
    removed = before.keys - fn.keys if before else Counter()
    lines = [header]
    for d in fn.decisions:
        prefix = ""
        if added[d.key] > 0:  # this decision is new since the base revision
            added[d.key] -= 1
            prefix = "+ "
        lines.append(f"{'  ' * d.depth}- {prefix}{d.text()}  {link(fn.path, d.line, link_base)}")
    for d in before.decisions if before else []:
        if removed[d.key] > 0:  # this decision is gone since the base revision
            removed[d.key] -= 1
            lines.append(f"- − {d.text()}  (was L{d.line})")
    return "\n".join(lines)


def python_files(paths: list[str]) -> list[Path]:
    found = []
    for path in map(Path, paths):
        found.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return found


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else ""


def report(paths: list[str], limit: int, link_base: str = "") -> str:
    blocks = [
        render(fn, limit, link_base)
        for path in python_files(paths)
        for fn in functions(path, path.read_text())
    ]
    return "\n\n".join(blocks)


def changed_report(base: str, paths: list[str], limit: int, link_base: str = "") -> str:
    """Only the functions whose decisions differ from `base`, with what was added and removed."""
    blocks = []
    changed = [Path(line) for line in git("diff", "--name-only", base, "--", *paths).splitlines()]
    for path in changed:
        if path.suffix != ".py":  # not Python
            continue
        old = {fn.name: fn for fn in functions(path, git("show", f"{base}:{path.as_posix()}"))}
        new = functions(path, path.read_text()) if path.exists() else []
        for fn in new:
            before = old.get(fn.name)
            if before is None:  # the function is new
                blocks.append(render(fn, limit, link_base, new=True))
            elif before.keys != fn.keys or before.count != fn.count:  # its decisions moved
                blocks.append(render(fn, limit, link_base, before=before))
        names = {fn.name for fn in new}
        blocks.extend(
            f"− **`{fn.label}`** removed ({fn.count} decisions)"
            for name, fn in old.items()
            if name not in names
        )
    return "\n\n".join(blocks) or "No function changed its decisions."


def limits(pyproject: Path = Path("pyproject.toml")) -> dict[str, int]:
    """The limits ruff enforces, from pyproject.toml: mccabe decisions, pylint branches and statements."""
    lint: dict = {}
    if pyproject.exists():  # the project configures ruff
        lint = tomllib.loads(pyproject.read_text()).get("tool", {}).get("ruff", {}).get("lint", {})
    return {
        "decisions": lint.get("mccabe", {}).get("max-complexity", 10),
        "branches": lint.get("pylint", {}).get("max-branches", 12),
        "statements": lint.get("pylint", {}).get("max-statements", 50),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["src"], help="files or directories (default: src)")
    parser.add_argument(
        "--changed", metavar="BASE", help="only functions whose decisions differ from this revision"
    )
    parser.add_argument("--limit", type=int, default=limits()["decisions"], help="ruff's mccabe limit")
    parser.add_argument("--link-base", default="", help="URL prefix that turns line numbers into links")
    args = parser.parse_args(argv)
    if args.changed:  # compare with a Git revision
        print(changed_report(args.changed, args.paths, args.limit, args.link_base))
    else:
        print(report(args.paths, args.limit, args.link_base))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
